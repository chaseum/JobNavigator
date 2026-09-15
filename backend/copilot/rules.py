"""Deterministic evidence rules: what verified facts prove without asking a model.

Each rule reads one requirement and a list of evidence units and returns a verdict,
or None when the requirement is not its kind. A verdict has a mode:
  floor  the facts prove at least this much; a lower model verdict is raised
  cap    the facts cannot prove more than this; a higher model verdict is lowered
  exact  the facts decide it; the model verdict is replaced
The same rules run over the whole verified profile (Candidate Fit) and over the
text a résumé actually shows (Resume Applicability), so both measure one thing.

A unit is one piece of evidence: a fact (profile) or a heading, bullet or skill
line (résumé). `source_text` is the verified fact text behind it; a résumé line
only counts for a term its own sources contain.
"""
import re
from datetime import date

from backend.copilot import facts as F

RANK = {"MISSING": 0, "UNKNOWN": 1, "PARTIAL": 2, "MATCHED": 3}


def verdict(status, refs, note, mode, rule) -> dict:
    return {"status": status, "refs": list(dict.fromkeys(refs)), "note": note, "mode": mode, "rule": rule}


# ── evidence units ───────────────────────────────────────────────────────────

def _span(kind: str, d: dict) -> tuple[str, str]:
    if kind == "education":
        return d.get("start_date") or "", d.get("graduation_date") or ""
    if kind == "certification":
        return d.get("date") or "", ""
    return d.get("start_date") or "", d.get("end_date") or ""


def unit(refs, kind, text, data=None, span=("", ""), strong=True, source_text=None, heading=False) -> dict:
    return {"refs": list(refs), "kind": kind, "text": text or "", "data": data or {}, "start": span[0], "end": span[1],
            "strong": strong, "source_text": text or "" if source_text is None else source_text, "heading": heading}


def profile_units(facts_by_ref: dict) -> list[dict]:
    """One unit per verified fact; an achievement takes its parent's kind and dates."""
    out = []
    for ref, f in facts_by_ref.items():
        kind, d = f["kind"], f.get("data") or {}
        if kind == "link":
            continue
        if kind == "skill":
            out.append(unit([ref], "skill", d.get("name", ""), d, strong=bool(d.get("evidence_ids"))))
            continue
        parent = facts_by_ref.get(f.get("parent") or "")
        if kind == "achievement" and parent:
            pd = parent.get("data") or {}
            out.append(unit([ref], parent["kind"], F.fact_text(kind, d), d, _span(parent["kind"], pd)))
        else:
            out.append(unit([ref], kind, F.fact_text(kind, d), d, _span(kind, d)))
    return out


def resume_units(resume: dict, facts_by_ref: dict, claims: dict | None = None) -> list[dict]:
    """What a résumé visibly shows, as units. Bullets that failed the claim audit, or still await review, show nothing."""
    claims = claims or {}
    out = []
    for sec in resume.get("sections") or []:
        for e in sec.get("entries") or []:
            f = facts_by_ref.get(e.get("fact_id"))
            if f is None:
                continue
            kind, d = f["kind"], f.get("data") or {}
            head = " ".join(x for x in (e.get("heading"), e.get("subheading"), e.get("date")) if x)
            out.append(unit([e["fact_id"]], kind, head, d if kind == "education" else {}, _span(kind, d), heading=True))
            for b in e.get("bullets") or []:
                c = claims.get(b.get("id")) or {}
                if c.get("status") == "UNSUPPORTED" or (c.get("status") == "AMBIGUOUS" and not c.get("reviewed")):
                    continue
                src = [s for s in b.get("source_fact_ids") or [] if s in facts_by_ref]
                if not src:
                    continue
                src_text = "\n".join(F.fact_text(facts_by_ref[s]["kind"], facts_by_ref[s].get("data") or {}) for s in src)
                out.append(unit(src, kind, b.get("text", ""), {}, _span(kind, d), source_text=src_text))
        for line in sec.get("lines") or []:
            for name, ref in zip(line.get("items") or [], line.get("source_fact_ids") or []):
                f = facts_by_ref.get(ref)
                if f is not None:
                    out.append(unit([ref], "skill", name, f.get("data"), strong=bool((f.get("data") or {}).get("evidence_ids"))))
    return out


# ── technology terms ─────────────────────────────────────────────────────────
# Only aliases that are the same thing. Java/JavaScript, C/C++, React/React Native
# and AWS/Azure stay apart: word boundaries keep "Java" out of "JavaScript", and a
# term never matches when a longer known term ("React Native") starts with it.

# surface form as written (two-letter aliases match in exactly this case) -> canonical term
ALIASES = {"Postgres": "postgresql", "JS": "javascript", "TS": "typescript", "C Sharp": "c#", "dotnet": ".net", "NodeJS": "node.js"}
_ALIAS_KEYS = {k.lower(): v for k, v in ALIASES.items()}


def canonical(term: str) -> str:
    t = " ".join((term or "").lower().split())
    return _ALIAS_KEYS.get(t, t)


def _split_name(name: str) -> list[str]:
    """"AWS (SQS, EventBridge)" -> AWS, SQS, EventBridge; "HTML/CSS" -> HTML/CSS, HTML, CSS."""
    m = re.match(r"^(.*?)\s*\((.*)\)\s*$", name or "")
    parts = [m.group(1), *m.group(2).split(",")] if m else [name or ""]
    out = []
    for p in (x.strip() for x in parts):
        if len(p) >= 2:
            out.append(p)
            if "/" in p:
                out += [x.strip() for x in p.split("/") if len(x.strip()) >= 3]
    return out


def _pattern(surface: str, longer: list[str]) -> re.Pattern | None:
    s = surface.strip()
    if len(s) < 2:
        return None      # ponytail: one-letter languages (C, R) are left to the model; too ambiguous to match safely
    flags = 0 if (len(s) <= 2 and s.isalpha()) else re.I   # "Go", "JS": exact case, never the verb or a stray token
    tails = "".join(rf"(?!\s+{re.escape(x[len(s):].strip())})" for x in longer)
    return re.compile(rf"(?<![A-Za-z0-9+#.]){re.escape(s)}(?![A-Za-z0-9+#]){tails}", flags)


def vocabulary(facts_by_ref: dict, extra=()) -> dict:
    """{canonical: {"name", "patterns"}} for every technology the profile or the posting names."""
    names: dict = {}
    for f in facts_by_ref.values():
        d = f.get("data") or {}
        raw = ([d.get("name", "")] if f["kind"] == "skill" else []) + list(d.get("technologies") or [])
        for n in raw:
            for part in _split_name(n):
                names.setdefault(canonical(part), part)
    for n in extra or ():
        for part in _split_name(str(n)):
            names.setdefault(canonical(part), part)
    lowered = [k for k in names]
    vocab = {}
    for canon, shown in names.items():
        surfaces = {shown, *[a for a, c in ALIASES.items() if c == canon]}
        if not (len(canon) <= 2 and canon.isalpha()):
            surfaces.add(canon)
        longer = [x for x in lowered if x.startswith(canon + " ")]
        pats = [p for p in (_pattern(s, longer) for s in surfaces) if p]
        if pats:
            vocab[canon] = {"name": shown, "patterns": pats}
    return vocab


def _hits(entry: dict, text: str) -> bool:
    return any(p.search(text or "") for p in entry["patterns"])


def mentioned_terms(vocab: dict, text: str) -> list[str]:
    found = [c for c, e in vocab.items() if _hits(e, text)]
    # "HTML/CSS" and its parts both match: keep the parts, which are what evidence names
    return [c for c in found if not ("/" in c and any(o != c and o in c.split("/") for o in found))]


ANY_RE = re.compile(r"\bat least one\b|\bone or more\b|\bany\b|\be\.g\.|\bsuch as\b|\bfor example\b|\blike\b|\bor\b|\bincluding\b", re.I)


def tech_rule(req: dict, units: list, vocab: dict):
    if req.get("category") in ("work_authorization", "clearance", "education", "soft_skill"):
        return None
    text = req.get("text") or ""
    terms = mentioned_terms(vocab, text)
    if not terms:
        return None
    strong, weak = {}, {}
    for c in terms:
        for u in units:
            if _hits(vocab[c], u["text"]) and _hits(vocab[c], u["source_text"]):
                (strong if u["strong"] else weak).setdefault(c, []).extend(u["refs"])
    if not strong and not weak:
        return None
    name = lambda cs: ", ".join(vocab[c]["name"] for c in cs)   # noqa: E731
    only_listed = [c for c in weak if c not in strong]
    absent = [c for c in terms if c not in strong and c not in weak]
    refs = [r for c in strong for r in strong[c]]
    if strong and (ANY_RE.search(text) or not absent and not only_listed):
        return verdict("MATCHED", refs, f"{name(strong)} used in verified experience, projects or research", "floor", "technology")
    bits = ([f"used: {name(strong)}"] if strong else []) + ([f"only listed as skills: {name(only_listed)}"] if only_listed else []) \
        + ([f"no evidence: {name(absent)}"] if absent else [])
    return verdict("PARTIAL", refs + [r for c in only_listed for r in weak[c]], "; ".join(bits), "floor", "technology")


# ── ways of having built software ────────────────────────────────────────────

AVENUES = [("internship", r"\binternships?\b"), ("project", r"\bprojects?\b"), ("open source", r"\bopen[- ]source\b"),
           ("hackathon", r"\bhackathons?\b"), ("coursework", r"\bcourse ?work\b|\bcourses\b|\bclasses\b"),
           ("research", r"\bresearch\b"), ("work experience", r"\bwork experience\b|\bprofessional experience\b"),
           ("publication", r"\bpublications?\b")]
_QUALIFIED = re.compile(r"\bincluding\b|\bwith (?:experience|exposure)\b|\bat an? [a-z ]*compan", re.I)


def _avenue_hit(avenue: str, u: dict) -> bool:
    k, t = u["kind"], u["text"]
    return {
        "internship": k == "internship" or (k == "experience" and re.search(r"\bintern(ship)?\b", t, re.I) is not None),
        "project": k == "project",
        "research": k == "research",
        "publication": k == "publication",
        "work experience": k in ("experience", "internship"),
        "coursework": k == "education" and re.search(r"\bcourse ?work\b", t, re.I) is not None,
        "hackathon": re.search(r"\bhackathons?\b", t, re.I) is not None,
        "open source": re.search(r"\bopen[- ]source\b", t, re.I) is not None,
    }[avenue]


def avenue_rule(req: dict, units: list):
    """"through internships, personal projects, hackathons or coursework": any one listed avenue is direct evidence."""
    if req.get("category") not in ("experience", "qualification"):
        return None
    text = req.get("text") or ""
    wanted = [a for a, rx in AVENUES if re.search(rx, text, re.I)]
    if len(wanted) < 2:
        return None    # a single avenue ("internship at a technology company") says more than a kind of fact can prove
    hits, seen = [], set()
    for u in units:
        if u["kind"] != "skill":
            for a in wanted:
                if _avenue_hit(a, u):
                    hits += u["refs"]
                    seen.add(a)
    if not hits:
        return None
    if _QUALIFIED.search(text):
        return verdict("PARTIAL", hits[:8], f"has {', '.join(sorted(seen))}; the rest of the requirement is not proven by fact type alone", "floor", "experience_type")
    return verdict("MATCHED", hits[:8], f"verified {', '.join(sorted(seen))} facts", "floor", "experience_type")


# ── degrees ──────────────────────────────────────────────────────────────────

LEVELS = [("doctorate", 4, r"\bph\.?\s?d\b|\bdoctor(?:ate|al)\b|\bd\.?phil\b"),
          ("master", 3, r"\bmaster(?:'s|’s|s)?\b|\bm\.?\s?sc?\b|\bm\.?\s?eng\b|\bmba\b"),
          ("bachelor", 2, r"\bbachelor(?:'s|’s|s)?\b|\bb\.?\s?sc?\b|\bb\.?\s?a\b|\bb\.?\s?eng\b|\bundergraduate degree\b"),
          ("associate", 1, r"\bassociate(?:'s|’s)?\s+(?:degree|of)\b")]
FIELDS = [("computer science", r"\bcomputer science\b|\bcomp\.? ?sci\b|(?-i:\bCS\b)"),
          ("computer engineering", r"\bcomputer engineering\b"),
          ("software engineering", r"\bsoftware engineering\b"),
          ("electrical engineering", r"\belectrical (?:and computer )?engineering\b"),
          ("information systems", r"\binformation systems\b|(?-i:\bMIS\b)"),
          ("information technology", r"\binformation technology\b|(?-i:\bIT\b)"),
          ("data science", r"\bdata science\b"),
          ("mathematics", r"\bmathematics\b|\bapplied math\b"),
          ("statistics", r"\bstatistics\b"),
          ("physics", r"\bphysics\b")]
TECHNICAL_FIELDS = {"computer science", "computer engineering", "software engineering", "electrical engineering",
                    "information systems", "information technology", "data science"}
_DEGREE_WORDS = re.compile(r"\bdegree\b|\bbachelor|\bmaster(?:'s|’s|s)?\b|\bph\.?\s?d\b|\bdoctorate\b|\bb\.s\.|\bm\.s\.|\bbs/ms\b", re.I)
_GRAD_WORDS = re.compile(r"\bgraduat|\benrolled\b", re.I)
_RELATED = re.compile(r"\brelated (?:technical |quantitative )?(?:field|discipline|area|major|degree)|\btechnical (?:field|discipline)\b", re.I)
_ENROLLED = re.compile(r"\bcurrently (?:enrolled|pursuing)\b|\bcurrent(?:ly)? (?:a )?student\b|\benrolled in\b|\bpursuing an? (?:bachelor|master|degree|ph)", re.I)
_FULL_TIME = re.compile(r"\bfull[- ]time\b", re.I)
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_WHEN = re.compile(r"(?:\b([a-z]{3})[a-z]*\.?\s+)?\b((?:19|20)\d{2})\b", re.I)


def degree_level(text: str):
    return next(((name, rank) for name, rank, rx in LEVELS if re.search(rx, text or "", re.I)), (None, None))


def degree_fields(text: str) -> set:
    return {name for name, rx in FIELDS if re.search(rx, text or "", re.I)}


def _ym(value: str, end=False):
    """(year, month|None) from YYYY-MM / YYYY; None when empty or unparseable."""
    m = re.match(r"^(\d{4})(?:-(\d{2}))?$", (value or "").strip())
    return (int(m.group(1)), int(m.group(2)) if m.group(2) else None) if m else None


def graduation_window(text: str):
    """("by", (y, m)) | ("range", (y, m), (y, m)) | ("years", {y, ...}) from "graduating ..." wording; None when unstated."""
    i = text.lower().find("graduat")
    if i < 0:
        return None
    tail = text[i:i + 90]
    whens = [((int(y), _MONTHS.get(mon[:3].lower()) if mon else None)) for mon, y in _WHEN.findall(tail)]
    if not whens:
        return None
    if re.search(r"\b(?:by|before|no later than|prior to)\b", tail, re.I):
        y, m = whens[0]
        return ("by", (y, m or 12))
    if len(whens) >= 2 and whens[0][1] and whens[1][1] and re.search(r"\bbetween\b|\bfrom\b", tail, re.I):
        return ("range", whens[0], whens[1])
    return ("years", {y for y, _ in whens})


def _within(grad, window):
    """True / False / None (unknown) for a graduation (year, month|None) against a window."""
    y, m = grad
    kind = window[0]
    if kind == "years":
        return y in window[1]
    if kind == "by":
        dy, dm = window[1]
        if y != dy:
            return y < dy
        return None if m is None else m <= dm
    (sy, sm), (ey, em) = window[1], window[2]
    if m is None:
        return None if y in (sy, ey) else sy < y < ey
    return (sy, sm) <= (y, m) <= (ey, em)


def education_rule(req: dict, units: list, today: date):
    text = req.get("text") or ""
    if not (_DEGREE_WORDS.search(text) or (req.get("category") == "education" and _GRAD_WORDS.search(text))):
        return None
    edu = [u for u in units if u["kind"] == "education" and (u["data"] or {}).get("institution")]
    if not edu:
        return None
    need = [rank for _, rank, rx in LEVELS if re.search(rx, text, re.I)]
    stripped = text
    for _, rx in FIELDS:
        stripped = re.sub(rx, " ", stripped, flags=re.I)
    want_fields, any_engineering, related = degree_fields(text), re.search(r"\bengineering\b", stripped, re.I), _RELATED.search(text)
    window = graduation_window(text)
    best = None
    for u in edu:
        d = u["data"]
        level, rank = degree_level(f"{d.get('degree', '')} {d.get('major', '')}")
        fields = degree_fields(d.get("major") or d.get("degree") or "")
        if need and (rank is None or rank < min(need)):
            continue
        if (want_fields or any_engineering or related) and not (
                fields & want_fields or (any_engineering and any("engineering" in f for f in fields))
                or (related and fields & TECHNICAL_FIELDS)):
            continue
        shown = ", ".join(x for x in (d.get("degree"), d.get("major")) if x)
        proven, unproven = [f"{shown} ({level or 'degree'})"], []
        grad = _ym(d.get("graduation_date"))
        if window:
            ok = _within(grad, window) if grad else None
            if ok is False:
                unproven.append(f"graduation {d.get('graduation_date')} is outside the window the posting states")
            elif ok is None:
                unproven.append("graduation month is not precise enough to confirm the window")
            else:
                proven.append(f"graduation {d.get('graduation_date')} is within the window")
        if _ENROLLED.search(text):
            if grad and grad >= (today.year, today.month if grad[1] else 1):
                proven.append(f"a {d.get('graduation_date')} graduation implies current enrollment")
            else:
                unproven.append("current enrollment is not established by the graduation date")
            if _FULL_TIME.search(text) and not _FULL_TIME.search(u["source_text"]):
                unproven.append("full-time enrollment is not stated in your profile")
        status = "PARTIAL" if unproven else "MATCHED"
        note = "; ".join(proven) + (f". Not proven: {'; '.join(unproven)}" if unproven else "")
        v = verdict(status, u["refs"], note, "exact", "education")
        if best is None or RANK[status] > RANK[best["status"]]:
            best = v
    return best or verdict("PARTIAL", [], "no verified degree matches the level or field this asks for; equivalent experience may still count",
                           "cap", "education")


# ── years of experience ──────────────────────────────────────────────────────

_YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2}\s*)?\+?\s*(?:years?|yrs?)\b", re.I)


def _interval(u: dict, today: date):
    """Months [start, end) as month indices; conservative when only a year is known."""
    s, e = _ym(u["start"]), (today.year, today.month) if (u["end"] or "").lower() == "present" else _ym(u["end"])
    if not s or not e:
        return None
    a, b = s[0] * 12 + (s[1] or 12), e[0] * 12 + (e[1] or 1)
    return (a, b) if b > a else None


def union_months(intervals) -> int:
    total, cur = 0, None
    for a, b in sorted(intervals):
        if cur and a <= cur[1]:
            cur = (cur[0], max(cur[1], b))
        else:
            total += (cur[1] - cur[0]) if cur else 0
            cur = (a, b)
    return total + ((cur[1] - cur[0]) if cur else 0)


def years_rule(req: dict, units: list, vocab: dict, today: date):
    if req.get("category") in ("education", "work_authorization", "clearance"):
        return None
    text = req.get("text") or ""
    m = _YEARS_RE.search(text)
    if not m or int(m.group(1)) == 0:
        return None
    need = int(m.group(1))
    terms = mentioned_terms(vocab, text)
    if terms:
        rel = [u for u in units if u["kind"] != "skill" and any(_hits(vocab[c], u["text"]) and _hits(vocab[c], u["source_text"]) for c in terms)]
        what = ", ".join(vocab[c]["name"] for c in terms)
    else:
        rel = [u for u in units if u["kind"] in ("experience", "internship", "research")]
        what = "professional experience"
    if not rel:
        return None
    dated = [(u, iv) for u in rel if (iv := _interval(u, today))]
    months = union_months([iv for _, iv in dated])
    if months >= need * 12:
        return verdict("MATCHED", [r for u, _ in dated for r in u["refs"]],
                       f"{months // 12} yr {months % 12} mo of dated {what} (overlaps counted once)", "exact", "years")
    note = (f"asks for {need}+ years; dated verified {what} adds up to {months} months" if dated
            else f"asks for {need}+ years; the verified {what} facts have no dates, so duration cannot be proven")
    return verdict("PARTIAL", [r for u in rel for r in u["refs"]], note, "exact", "years")


# ── combining ────────────────────────────────────────────────────────────────

def evaluate(req: dict, units: list, vocab: dict, today: date) -> list[dict]:
    return [v for v in (education_rule(req, units, today), years_rule(req, units, vocab, today),
                        tech_rule(req, units, vocab), avenue_rule(req, units)) if v]


def combine(row: dict, verdicts: list[dict]) -> dict:
    """Apply verdicts to a checked row: floors, then caps, then exacts (the most conservative exact last)."""
    row = dict(row)
    ordered = ([v for v in verdicts if v["mode"] == "floor"] + [v for v in verdicts if v["mode"] == "cap"]
               + sorted((v for v in verdicts if v["mode"] == "exact"), key=lambda v: -RANK[v["status"]]))
    for v in ordered:
        cur = row["status"]
        if (v["mode"] == "floor" and RANK[v["status"]] <= RANK[cur]) or (v["mode"] == "cap" and RANK[v["status"]] >= RANK[cur]):
            continue
        row["rules"] = [*row.get("rules", []), v["rule"]]
        row["status"] = v["status"]
        if v["mode"] == "cap":
            row["explanation"] = v["note"] + (f" (model said {cur}: {row['explanation']})" if row.get("explanation") else "")
            if v["status"] not in ("MATCHED", "PARTIAL"):
                row["source_fact_ids"] = []
        else:
            row["explanation"] = v["note"]
            row["source_fact_ids"] = v["refs"] if v["status"] in ("MATCHED", "PARTIAL") else []
    return row
