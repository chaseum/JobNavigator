"""Application-question autofill: generate an answer from persona + qa_bank."""
import json as _json
import logging
import re as _re
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from backend.models.db import SessionLocal, Setting, Persona
from backend.analyzer.llm_client import call_autofill_llm, call_autofill_llm_stream
from backend.analyzer.llm_logger import track_llm_call
from backend.autofill_schema import ANSWER_SCHEMA, PROTECTED_KEYS, project_answers

logger = logging.getLogger("jobnavigator.autofill")
router = APIRouter(prefix="/autofill", tags=["autofill"])


def _flatten_persona(p: Persona) -> str:
    parts = []
    for label, node in (("Contact", p.contact), ("Work authorization", p.work_auth),
                        ("Preferences", p.preferences), ("Resume content", p.resume_content)):
        if node:
            parts.append(f"{label}:\n{_json.dumps(node, indent=2)}")
    return "\n\n".join(parts) if parts else "(empty)"


def _trim_to_chars(text: str, max_chars: int):
    """Cut text to at most max_chars on a sentence or word boundary, enforced here since the model treats a character limit as a suggestion; returns (text, trimmed)."""
    if not text or max_chars <= 0 or len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    # A sentence end is only a good cut if it isn't throwing most of the answer
    # away — otherwise a single early "e.g." would strand the reader mid-thought.
    end = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
    if end >= int(max_chars * 0.6):
        return cut[:end + 1].rstrip(), True
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip().rstrip(",;:-—–").rstrip(), True


_FENCE_OPEN = _re.compile(r"^```[A-Za-z0-9_+-]*[^\S\r\n]*\r?\n?")


def _strip_code_fences(text: str) -> str:
    """Drop a ```json … ``` wrapper the model put around its JSON."""
    t = (text or "").strip()
    if not t.startswith("```"):
        return t
    t = _FENCE_OPEN.sub("", t, count=1).strip()
    if t.endswith("```"):
        t = t[:-3].rstrip()
    return t


def _first_json_object(text: str):
    r"""Return the first balanced {...} in text (string/escape aware), or None; a naive brace regex would span to the last "}" in the reply and break on trailing prose."""
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text or ""):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    return text[start:i + 1]
    return None


def _last_unescaped_quote(body: str) -> int:
    """Index of the last `"` that is not itself escaped, or -1; a plain rfind would land on an escaped quote and cut a truncated answer short."""
    i = len(body) - 1
    while i >= 0:
        if body[i] == '"':
            back = 0
            j = i - 1
            while j >= 0 and body[j] == "\\":
                back += 1
                j -= 1
            if back % 2 == 0:
                return i
        i -= 1
    return -1


def _unescape_json_fragment(body: str) -> str:
    """Decode a JSON string body that has lost its closing quote."""
    try:
        return _json.loads('"' + body + '"')
    except ValueError:
        pass
    # A truncated fragment can end mid-escape ("…\") — drop that and retry.
    trimmed = body[:-1] if body.endswith("\\") else body
    try:
        return _json.loads('"' + trimmed + '"')
    except ValueError:
        return (trimmed.replace("\n", "\n").replace("\t", "\t")
                       .replace('\\"', '"').replace("\\\\", "\\"))


def _extract_answer(raw: str) -> str:
    """Pull the answer text out of the model's reply, trying a fenced JSON envelope, the whole reply, the first balanced object, a hand-salvaged truncated envelope, then plain prose, so a malformed wrapper is never pasted into a real application form; returns "" when nothing usable is left."""
    text = _strip_code_fences(raw)
    if not text:
        return ""
    for candidate in (text, _first_json_object(text)):
        if not candidate:
            continue
        try:
            parsed = _json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            value = parsed.get("answer")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                value = str(value)
            if isinstance(value, str) and value.strip():
                return value.strip()
        elif isinstance(parsed, str) and parsed.strip():
            return parsed.strip()
    # Truncated envelope: the opening `{"answer": "` is there, the closing quote
    # and brace are not (the model hit its token budget mid-sentence).
    m = _re.search(r'["\']answer["\']\s*:\s*"', text)
    if m:
        body = text[m.end():]
        end = _last_unescaped_quote(body)
        if end > 0:
            body = body[:end]
        body = _unescape_json_fragment(body.rstrip()).strip()
        if body and not body.startswith("{"):
            return body
    # Plain prose is fine; a leftover JSON envelope never is.
    text = text.strip().strip('"').strip()
    return "" if text.startswith("{") else text


_JSON_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t",
                 "r": "\r", "b": "\b", "f": "\f"}
_ANSWER_PREFIX_RE = _re.compile(r'^\{\s*["\']answer["\']\s*:\s*"')


def _decode_json_string_body(body: str) -> tuple:
    """Decode a JSON string body that may be incomplete (more of it is still
    streaming in). Returns (text, closed): closed is True once an unescaped
    closing quote is reached, at which point `text` is the final answer and
    everything after the quote (the rest of the envelope) is not part of it.
    A trailing, not-yet-complete escape sequence (e.g. a lone "\\" or a
    partial "\\uXX") is left off `text` until more characters arrive."""
    out = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "\\":
            if i + 1 >= n:
                break
            nxt = body[i + 1]
            if nxt == "u":
                hex_part = body[i + 2:i + 6]
                if len(hex_part) < 4:
                    break
                out.append(chr(int(hex_part, 16)))
                i += 6
                continue
            out.append(_JSON_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        if c == '"':
            return "".join(out), True
        out.append(c)
        i += 1
    return "".join(out), False


async def _clean_stream(chunks):
    """Wrap the raw model stream so a delta can never carry a `{"answer": …}`
    envelope, even for a moment. /answer/stream tells the model to output plain
    prose, but a model that ignores that (or types the envelope a character at
    a time) would otherwise flash raw JSON in the extension popover mid-stream
    before the client's own safety net cleaned it up on completion — the R4 fix
    ("robust extraction, never returns text starting with {") only covered the
    non-stream /answer path. This buffers just long enough to tell plain prose
    from an (optionally ```-fenced) JSON envelope, then for the envelope case
    decodes the `"answer"` string body progressively — handling an escape
    sequence split across two chunks — and drops everything once its closing
    quote is seen, discarding the rest of the envelope. Plain prose is passed
    through unchanged, incrementally, same as before this wrapper existed."""
    raw = ""
    mode = None          # None (undecided) | "prose" | "json"
    fence_len = 0
    body_start = -1
    closed = False
    emitted = 0          # chars of raw[fence_len:] already yielded, in prose mode
    decoded_len = 0       # chars of the decoded answer already yielded, in json mode
    async for chunk in chunks:
        if closed or not chunk:
            continue
        raw += chunk
        if mode is None:
            stripped = raw.lstrip()
            if not stripped:
                continue  # only whitespace so far — hold
            if stripped[0] == "`":
                if len(stripped) < 3:
                    continue  # could still become a ``` fence — hold
                if not stripped.startswith("```"):
                    mode = "prose"
                    fence_len = len(raw) - len(stripped)
                else:
                    m = _FENCE_OPEN.match(stripped)
                    fence_len = (len(raw) - len(stripped)) + len(m.group(0))
                    rest = raw[fence_len:]
                    if not rest:
                        continue  # fence consumed everything received so far — hold
                    mode = "json" if rest[0] == "{" else "prose"
            else:
                fence_len = len(raw) - len(stripped)
                mode = "json" if stripped[0] == "{" else "prose"
        body = raw[fence_len:]
        if mode == "prose":
            if len(body) > emitted:
                new_text = body[emitted:]
                emitted = len(body)
                yield new_text
            continue
        # mode == "json"
        if body_start < 0:
            m = _ANSWER_PREFIX_RE.match(body)
            if not m:
                continue  # envelope shape not confirmed yet — hold
            body_start = m.end()
        text, done = _decode_json_string_body(body[body_start:])
        if len(text) > decoded_len:
            yield text[decoded_len:]
            decoded_len = len(text)
        if done:
            closed = True


def _qa_pair(entry) -> tuple:
    """Normalise one qa_bank entry to (question, answer); accepts both the canonical {"question","answer"} shape written by POST /persona/qa-bank and a legacy single-key {"<question>": "<answer>"} map."""
    if not isinstance(entry, dict):
        return "", ""
    if "question" in entry or "answer" in entry:
        return str(entry.get("question") or ""), str(entry.get("answer") or "")
    for k, v in entry.items():
        return str(k or ""), str(v or "")
    return "", ""


def _flatten_qa_bank(bank) -> str:
    if not bank:
        return "(empty)"
    pairs = [_qa_pair(e) for e in bank]
    pairs = [(q, a) for q, a in pairs if q or a]
    if not pairs:
        return "(empty)"
    return "\n\n".join(f"Q: {q}\nA: {a}" for q, a in pairs)


def _setting(db, key, default=""):
    row = db.query(Setting).filter(Setting.key == key).first()
    return row.value if row and row.value is not None else default


def _json_setting(db, key, default):
    try:
        return _json.loads(_setting(db, key, "") or "")
    except ValueError:
        return default


@router.get("/config")
def autofill_config():
    """Serve everything the extension needs to fill structured fields: the projected fixed answers, matching dictionaries, and the schema."""
    db = SessionLocal()
    try:
        p = db.query(Persona).filter(Persona.id == 1).first()
        persona = {
            "contact": (p.contact if p else None) or {},
            "work_auth": (p.work_auth if p else None) or {},
            "demographics": (p.demographics if p else None) or {},
            "compensation": (p.compensation if p else None) or {},
            "preferences": (p.preferences if p else None) or {},
        }
        return {
            "answers": project_answers(persona),
            "field_patterns": _json_setting(db, "autofill_field_patterns", {}),
            "option_synonyms": _json_setting(db, "autofill_option_synonyms", {}),
            "schema": ANSWER_SCHEMA,
            # Mirrors the Persona's "prefer not to answer" checkbox so the extension
            # can decline questions the Persona has no field for at all.
            "decline_self_id": bool((persona.get("demographics") or {}).get("decline_demographics")),
            "protected_keys": sorted(PROTECTED_KEYS),
        }
    finally:
        db.close()


def _build_autofill_prompt(body: dict, *, want_provider: bool = False) -> dict:
    """Shared prompt construction for both /answer and /answer/stream.

    Both variants must honour the editable `autofill_prompt` setting — the
    streaming path used to build its own hardcoded prefix, so editing the prompt
    in Settings had no effect on the answers the extension actually renders
    (R4-T1-22). Returns the cacheable prefix, the per-question suffix and the
    resolved character budget.
    """
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "question is required")
    company = (body.get("company") or "").strip() or "(unknown company)"
    position = (body.get("position") or "").strip() or "(unknown role)"

    provider = model = None
    db = SessionLocal()
    try:
        persona = db.query(Persona).filter(Persona.id == 1).first()
        len_row = db.query(Setting).filter(Setting.key == "autofill_default_length").first()
        default_len = int(len_row.value) if len_row and (len_row.value or "").isdigit() else 120
        tmpl_row = db.query(Setting).filter(Setting.key == "autofill_prompt").first()
        if not tmpl_row or not (tmpl_row.value or "").strip():
            raise HTTPException(500, "autofill_prompt setting is empty")
        template = tmpl_row.value
        persona_txt = _flatten_persona(persona) if persona else "(no persona)"
        qa_txt = _flatten_qa_bank(persona.qa_bank if persona else [])

        if want_provider:
            # Resolve provider/model for logging with the same resolver
            # call_autofill_llm dispatches through, so both stay in sync.
            from backend.analyzer.llm_client import resolve_llm_config
            _cfg = resolve_llm_config("autofill", db=db)
            provider, model = _cfg["provider"], _cfg["model"]
    finally:
        db.close()

    max_chars = body.get("max_chars")
    max_chars = int(max_chars) if isinstance(max_chars, (int, str)) and str(max_chars).isdigit() else default_len

    # Stable prefix (persona+bank+instructions) is cacheable; the per-question suffix
    # isn't. Split at the first {company} placeholder so the model sees each part once.
    if "{company}" in template:
        before, after = template.split("{company}", 1)
        suffix_template = "{company}" + after
    else:
        before, suffix_template = "", template

    # Every placeholder is substituted in BOTH halves, independently of where the
    # split fell. Filling only some of them per half meant a template written
    # without {company} put the whole thing in the suffix and never expanded
    # {persona} / {qa_bank} — the model was handed the literal braces and answered
    # with no profile at all (R4-E2E-04). Sequential .replace, not str.format_map:
    # the shipped template contains a literal JSON envelope ({"answer": …}) that
    # any format() call would choke on.
    values = {
        "{persona}": persona_txt,
        "{qa_bank}": qa_txt,
        "{max_chars}": str(max_chars),
        "{company}": company,
        "{position}": position,
        "{question}": question,
    }

    def _fill(chunk: str) -> str:
        for token, value in values.items():
            chunk = chunk.replace(token, value)
        return chunk

    cached_prefix = _fill(before) or None
    suffix = _fill(suffix_template)
    return {"cached_prefix": cached_prefix, "suffix": suffix,
            "max_chars": max_chars, "provider": provider, "model": model}


def _protected_guard(body: dict):
    """A protected question (authorization, sponsorship, EEO, salary, attestations) never reaches a model.

    Returns the user's own verified Answer Bank answer when there is one; otherwise 422.
    """
    from backend.copilot import answer_bank as AB
    question = (body.get("question") or "").strip() if isinstance(body, dict) else ""
    intent = AB.classify(question)
    if not AB.is_protected(intent):
        return None
    db = SessionLocal()
    try:
        p = db.query(Persona).filter(Persona.id == 1).first()
        entry, _ = AB.find_answer((p.qa_bank if p else None) or [], question)
    finally:
        db.close()
    if entry:
        return {"answer": entry["answer"], "trimmed": False, "max_chars": None, "from_bank": entry["id"], "intent": intent}
    raise HTTPException(422, f"protected: a {intent.replace('_', ' ')} question is never drafted by AI. "
                             "Answer it yourself; the extension offers to save it to your Answer Bank.")


@router.post("/answer")
async def autofill_answer(body: dict):
    guarded = _protected_guard(body)
    if guarded is not None:
        return guarded
    built = _build_autofill_prompt(body, want_provider=True)
    cached_prefix, suffix = built["cached_prefix"], built["suffix"]
    max_chars = built["max_chars"]
    provider, model = built["provider"], built["model"]
    system = "You write concise, truthful first-person job-application answers grounded only in the provided profile."

    # token budget: char budget (~4 chars/token) + headroom for the JSON wrapper
    # and any discarded preamble. Kept tight so the answer respects max_chars.
    max_tokens = max(96, min(900, max_chars // 4 + 96))
    try:
        async with track_llm_call("autofill", provider, model) as tracker:
            resp = await call_autofill_llm(suffix, system, max_tokens=max_tokens,
                                           cached_prefix=cached_prefix)
            tracker.record(resp)
        raw = (resp.get("text") or "").strip()
        # Extract the answer from the model's {"answer": ...} wrapper, discarding any
        # leaked preamble; an unsalvageable envelope is a failed generation (502)
        # rather than raw JSON pasted into the user's application form.
        answer = _extract_answer(raw)
        if not answer:
            raise ValueError(f"unusable model output: {raw[:160]!r}")
    except Exception as e:
        logger.error(f"autofill generation failed: {e}")
        raise HTTPException(502, "autofill generation failed") from e
    # The length the user picked is a contract, not a hint.
    answer, trimmed = _trim_to_chars(answer, max_chars)
    return {"answer": answer, "trimmed": trimmed, "max_chars": max_chars}


@router.post("/answer/stream")
async def autofill_answer_stream(body: dict):
    """SSE variant of /answer that streams the drafted answer as plain-text chunks (no JSON wrapper) so the extension can render it into the field live, sharing the persona/qa_bank cache prefix with /answer."""
    sse_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    try:
        guarded = _protected_guard(body)
    except HTTPException as e:
        refusal = e.detail

        async def _refuse():
            yield f"data: {_json.dumps({'error': refusal, 'protected': True})}\n\n"
        return StreamingResponse(_refuse(), media_type="text/event-stream", headers=sse_headers)
    if guarded is not None:
        async def _from_bank():
            yield f"data: {_json.dumps({'delta': guarded['answer']})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(_from_bank(), media_type="text/event-stream", headers=sse_headers)

    # Same editable `autofill_prompt` template as /answer, so what the user edits
    # in Settings governs the variant the extension actually streams (R4-T1-22).
    built = _build_autofill_prompt(body)
    cached_prefix = built["cached_prefix"]
    max_chars = built["max_chars"]

    # Refinements: ordered change requests already applied ("shorter", "mention
    # fintech work"), appended to the suffix so the cache prefix stays untouched.
    refinements = body.get("refinements") or []
    refine_block = ""
    if isinstance(refinements, list):
        lines = "\n".join(f"- {str(r).strip()}" for r in refinements if str(r).strip())
        if lines:
            refine_block = f"\n\nApply these changes the candidate requested, in order:\n{lines}"

    # The template steers toward the {"answer": …} envelope /answer parses; this
    # path renders straight into a form field, so the envelope is suppressed here
    # rather than by forking the whole prompt.
    suffix = (
        f"{built['suffix']}{refine_block}\n\n"
        f"Write a first-person answer in AT MOST {max_chars} characters — this is a "
        f"hard limit, not a target. Be concise and finish a sentence before reaching it. "
        f"Output only the answer text — no JSON, no quotes, no preamble, no labels."
    )
    system = ("You write concise, truthful first-person job-application answers grounded "
              "only in the provided profile. Respect the character limit strictly. "
              "Output only the answer as plain prose.")
    # ~4 chars/token, so cap tokens near the char budget plus a small buffer.
    max_tokens = max(48, min(800, max_chars // 4 + 24))

    async def _events():
        try:
            raw_chunks = call_autofill_llm_stream(suffix, system, max_tokens=max_tokens,
                                                  cached_prefix=cached_prefix)
            async for chunk in _clean_stream(raw_chunks):
                if chunk:
                    yield f"data: {_json.dumps({'delta': chunk})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error(f"autofill stream failed: {e}")
            yield f"data: {_json.dumps({'error': 'autofill generation failed'})}\n\n"

    return StreamingResponse(_events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Application copilot: job context, accepted résumé, Answer Bank, tracking ──

import base64 as _b64
import re as _re2
from urllib.parse import parse_qsl, urlsplit

_ID_PARAMS = {"gh_jid", "token", "jobid", "job_id", "jid", "req", "reqid", "requisitionid", "id"}


def normalize_job_url(url: str) -> str:
    """host + path without the apply suffix, keeping only query params that identify the posting."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return ""
    host = parts.netloc.lower().removeprefix("www.")
    path = _re2.split(r"/(?:apply|application|applymanually|applywithautofill)(?:/|$)", parts.path, maxsplit=1, flags=_re2.I)[0]
    keep = sorted((k.lower(), v) for k, v in parse_qsl(parts.query) if k.lower() in _ID_PARAMS and v)
    return (host + path.rstrip("/")).lower() + ("?" + "&".join(f"{k}={v}" for k, v in keep) if keep else "")


def match_job(db, *urls):
    """The saved job a page belongs to: same normalized URL, the page under the posting, or a shared posting id."""
    from backend.models.db import Job
    for url in [u for u in urls if u]:
        want = normalize_job_url(url)
        host = want.split("/", 1)[0].split("?", 1)[0]
        if not host:
            continue
        candidates = db.query(Job).filter(Job.url.ilike(f"%{host}%")).order_by(Job.discovered_at.desc()).limit(500).all()
        best = None
        for job in candidates:
            have = normalize_job_url(job.url)
            if have == want:
                return job
            base_want, base_have = want.split("?", 1)[0], have.split("?", 1)[0]
            if base_want.startswith(base_have + "/") or base_have.startswith(base_want + "/"):
                best = best or job
        if best:
            return best
    for url in [u for u in urls if u]:
        try:
            ids = [v for k, v in parse_qsl(urlsplit(url).query) if k.lower() in ("gh_jid", "token") and v.isdigit()]
        except ValueError:
            ids = []
        for jid in ids:
            job = db.query(Job).filter(Job.url.ilike(f"%{jid}%")).first()
            if job:
                return job
    return None


def _accepted_resume_for(db, job):
    from backend.copilot.versions import latest_accepted
    v = latest_accepted(db, job_id=job.id) if job is not None else None
    if v is None and _setting(db, "autofill_resume_fallback", "none") == "latest":
        v = latest_accepted(db)
        return v, v is not None
    return v, False


@router.post("/job-context")
def job_context(body: dict):
    """What the extension needs on an application page: the saved job, its accepted résumé, the match, the application."""
    from backend.copilot.analysis import latest_record
    from backend.models.db import Application
    db = SessionLocal()
    try:
        job = match_job(db, body.get("url"), body.get("tab_url"))
        if job is None:
            return {"job": None, "resume_version": None}
        version, fallback = _accepted_resume_for(db, job)
        rec = latest_record(db, job.id)
        app = db.query(Application).filter(Application.job_id == job.id).first()
        return {
            "job": {"id": str(job.id), "title": job.title, "company": job.company, "url": job.url},
            "resume_version": {"id": str(version.id), "kind": version.kind, "fallback": fallback,
                               "accepted_at": version.accepted_at.isoformat() if version.accepted_at else None} if version else None,
            "match_score": ((rec.match or {}).get("score") if rec else None),
            "application": {"id": str(app.id), "status": app.status} if app else None,
        }
    finally:
        db.close()


@router.get("/resume/{version_id}")
def accepted_resume_pdf(version_id: str):
    """Only an accepted version is ever handed to autofill for upload."""
    import uuid as _uuid
    from sqlalchemy.orm import undefer
    from backend.models.db import ResumeVersion
    try:
        _uuid.UUID(version_id)
    except ValueError:
        raise HTTPException(404, "résumé not found")
    db = SessionLocal()
    try:
        v = db.query(ResumeVersion).options(undefer(ResumeVersion.pdf)).filter(ResumeVersion.id == version_id).first()
        if v is None or v.status != "accepted" or not v.pdf:
            raise HTTPException(404, "only an accepted résumé with a compiled PDF can be uploaded")
        p = db.query(Persona).filter(Persona.id == 1).first()
        c = (p.contact if p else None) or {}
        name = "_".join(x for x in (c.get("first_name"), c.get("last_name")) if x) or "Resume"
        return {"filename": f"{_re2.sub(r'[^A-Za-z0-9_-]', '', name)}_Resume.pdf", "base64": _b64.b64encode(v.pdf).decode()}
    finally:
        db.close()


@router.post("/lookup")
def lookup_answers(body: dict):
    """Answer Bank matches for the open questions on a form: {questions: [...]} -> one result per question."""
    from backend.copilot import answer_bank as AB
    questions = [str(q)[:500] for q in (body.get("questions") or []) if str(q).strip()][:60]
    db = SessionLocal()
    try:
        p = db.query(Persona).filter(Persona.id == 1).first()
        bank = (p.qa_bank if p else None) or []
    finally:
        db.close()
    out = []
    for q in questions:
        intent = AB.classify(q)
        entry, how = AB.find_answer(bank, q)
        out.append({"question": q, "intent": intent, "protected": AB.is_protected(intent), "how": how,
                    "match": {"id": entry["id"], "answer": entry["answer"], "answer_type": entry["answer_type"]} if entry else None})
    return {"results": out}


@router.post("/filled")
def record_fill(body: dict):
    """Autofill finished filling a form: create or update the application at ready_to_apply. Submission stays with the user."""
    import uuid as _uuid
    from sqlalchemy.orm.attributes import flag_modified
    from backend.api.routes_applications import PRE_APPLY_STAGES
    from backend.copilot import answer_bank as AB
    from backend.copilot.analysis import latest_record
    from backend.models.db import Application, Job, ResumeVersion, record_transition, utcnow
    db = SessionLocal()
    try:
        job = None
        if body.get("job_id"):
            try:
                job = db.query(Job).filter(Job.id == _uuid.UUID(str(body["job_id"]))).first()
            except ValueError:
                job = None
        job = job or match_job(db, body.get("url"), body.get("tab_url"))
        if job is None:
            raise HTTPException(404, "save this job first to track the application")
        version = None
        if body.get("resume_version_id"):
            try:
                version = db.get(ResumeVersion, _uuid.UUID(str(body["resume_version_id"])))
            except ValueError:
                version = None
            if version is None or version.status != "accepted":
                raise HTTPException(400, "only an accepted résumé can be recorded on an application")
        answers = [
            {k: str(a.get(k) or "")[:500] for k in ("label", "key", "source", "bank_id", "value")}
            for a in (body.get("answers_used") or []) if isinstance(a, dict)
        ][:200]
        rec = latest_record(db, job.id)
        score = (rec.match or {}).get("score") if rec else None

        app = db.query(Application).filter(Application.job_id == job.id).first()
        if app is None:
            app = Application(job_id=job.id, status="ready_to_apply",
                              status_transitions=[{"from": None, "to": "ready_to_apply", "at": utcnow().isoformat(), "source": "extension"}])
            db.add(app)
        elif app.status in PRE_APPLY_STAGES and app.status != "ready_to_apply":
            record_transition(app, "ready_to_apply", "extension")
        if app.status in PRE_APPLY_STAGES:
            if version is not None:
                app.resume_version_id = version.id
            app.match_score_at_apply = score
            app.answers_used = answers
            if body.get("ats") and not (app.notes or "").startswith("Filled by extension"):
                note = f"Filled by extension on {str(body['ats'])[:40]}"
                app.notes = f"{note}\n{app.notes}" if app.notes else note
        used = [a["bank_id"] for a in answers if a.get("bank_id")]
        if used:
            p = db.query(Persona).filter(Persona.id == 1).first()
            if p is not None:
                p.qa_bank = AB.mark_used(p.qa_bank, used)
                flag_modified(p, "qa_bank")
        db.commit()
        return {"application_id": str(app.id), "job_id": str(job.id), "status": app.status,
                "resume_version_id": str(app.resume_version_id) if app.resume_version_id else None,
                "match_score_at_apply": app.match_score_at_apply}
    finally:
        db.close()


_FIELD_KEY_HELP = {k: k.replace("_", " ") for k in ANSWER_SCHEMA} | {"full_name": "full name", "phone_national": "phone without country code"}


@router.post("/map-fields")
async def map_fields(body: dict):
    """Fallback for labels the deterministic ATS patterns missed. Only normalized labels go to the model, and only keys come back."""
    from backend.analyzer import llm_client
    from backend.copilot.schemas import FieldMapping
    fields = []
    for f in (body.get("fields") or [])[:40]:
        if not isinstance(f, dict) or not str(f.get("label") or "").strip():
            continue
        fields.append({"id": str(f.get("id"))[:40], "label": " ".join(str(f["label"]).split())[:200],
                       "kind": str(f.get("kind") or "")[:20],
                       "options": [str(o)[:80] for o in (f.get("options") or [])[:30]]})
    if not fields:
        return {"mappings": []}
    prompt = ("Map each application form field to one profile key, or null when none fits exactly. "
              "Do not guess: a field about something not in the key list maps to null.\n\nKEYS:\n"
              + "\n".join(f"- {k}: {v}" for k, v in _FIELD_KEY_HELP.items())
              + "\n\nFIELDS:\n" + "\n".join(
                  f"- id {f['id']} [{f['kind']}] {f['label']}" + (f" (options: {', '.join(f['options'])})" if f["options"] else "")
                  for f in fields))
    try:
        out, _, _ = await llm_client.call_structured(FieldMapping, prompt, "You map form labels to a fixed list of keys.",
                                                     feature="autofill", max_tokens=1500)
    except Exception as e:
        logger.warning(f"map-fields failed: {e}")
        raise HTTPException(502, "field mapping failed")
    ids = {f["id"] for f in fields}
    return {"mappings": [{"field_id": m.field_id, "key": m.key} for m in out.mappings
                         if m.field_id in ids and m.key in _FIELD_KEY_HELP]}
