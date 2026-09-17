"""Career Evidence: historical résumés -> extracted claims -> reconciled canonical facts.

Uploaded résumés are *evidence*, not truth. Each one is a `ResumeSource`; every
claim it supports is attached to the canonical `CandidateFact` through
`CandidateFactSource`, so a fact can always say which documents back it.

Reconciling one extracted item against what is already stored has exactly four
outcomes, and none of them asks a model to decide:

A. compatible duplicate   -> attach the source, fill only fields that were empty
B. more evidence          -> same entity, union of the distinct achievements/lists
C. conflict               -> a scalar both sides state differently (a graduation
                             date, a GPA): record a `FactConflict` and leave the
                             stored value alone until the user chooses
D. novel                  -> create it, unverified, until the user confirms it

Identity is decided by normalised stable dimensions (employer, title, school,
degree, project name, skill name), not by raw text equality, and never by dates:
two résumés disagreeing about when something ended describe one entity, which is
case C. Achievements are matched only against their own parent's children, so the
same bullet under two different jobs stays two facts.
"""
import hashlib
import json
import logging
import re
import unicodedata

from backend.copilot import facts as F
from backend.models.db import CandidateFact, CandidateFactSource, FactConflict, ResumeSource, utcnow

logger = logging.getLogger("jobnavigator.copilot.evidence")


# ── normalisation ────────────────────────────────────────────────────────────

def _fold(value) -> str:
    """Case, accents and punctuation removed; one space between words."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9+#]+", " ", text.casefold()).split())


# Legal suffixes only. "Acme Technologies" is deliberately NOT folded into "Acme":
# dropping a descriptive word merges two employers that may really be different.
_ORG_SUFFIXES = {"inc", "llc", "llp", "lp", "ltd", "limited", "corp", "corporation", "co",
                 "company", "plc", "gmbh", "ag", "bv", "nv", "sa", "pvt", "pte", "srl"}
_ORG_FILLER = {"the", "and", "of"}

_TITLE_ALIASES = {"sr": "senior", "snr": "senior", "jr": "junior", "jnr": "junior",
                  "engineering": "engineer", "internship": "intern", "coop": "intern",
                  "mgr": "manager", "swe": "software engineer", "pm": "product manager",
                  "tpm": "technical product manager", "ml": "machine learning"}
_TITLE_FILLER = {"the", "of", "and", "a", "an"}

_DEGREE_ALIASES = {
    "bs": "bachelor of science", "bsc": "bachelor of science", "bse": "bachelor of science",
    "ba": "bachelor of arts", "bachelors": "bachelor", "ab": "bachelor of arts",
    "ms": "master of science", "msc": "master of science", "ma": "master of arts",
    "masters": "master", "meng": "master of engineering", "beng": "bachelor of engineering",
    "mba": "master of business administration", "phd": "doctor of philosophy",
}


def norm_org(value) -> str:
    words = [w for w in _fold(value).split() if w not in _ORG_FILLER]
    while words and words[-1] in _ORG_SUFFIXES:
        words.pop()
    return " ".join(words)


def norm_title(value) -> str:
    out = []
    for word in _fold(value).split():
        if word in _TITLE_FILLER:
            continue
        out.extend(_TITLE_ALIASES.get(word, word).split())
    return " ".join(out)


def norm_degree(value) -> str:
    # "B.S." must reach the alias table as "bs", not as the two words "b s"
    return " ".join(_DEGREE_ALIASES.get(w, w) for w in _fold(str(value or "").replace(".", "")).split())


def norm_skill(value) -> str:
    """"Node.js", "node js" and "NodeJS" are one skill; C++ and C# stay distinct."""
    folded = _fold(value)
    return folded.replace(" ", "") if len(folded.split()) <= 2 else folded


def norm_url(value) -> str:
    return re.sub(r"^(https?://)?(www\.)?", "", str(value or "").strip().casefold()).rstrip("/")


def norm_text(value) -> str:
    return _fold(value)


# ── identity ─────────────────────────────────────────────────────────────────
# An experience is identified by employer + role, never by the raw strings and
# never by its dates. Every kind names its stable dimensions here.

GROUPS = {"experience": "employment", "internship": "employment"}
KEY_FIELDS = {
    "employment": (("employer", norm_org), ("title", norm_title)),
    "education": (("institution", norm_org), ("degree", norm_degree), ("major", norm_text)),
    "project": (("name", norm_text),),
    "research": (("organization", norm_org), ("title", norm_title)),
    "skill": (("name", norm_skill),),
    "certification": (("name", norm_text), ("issuer", norm_org)),
    "publication": (("title", norm_text),),
    "achievement": (("text", norm_text),),
    "link": (("url", norm_url),),
}

# Prose is not a dimension to reconcile: whoever wrote it first keeps it, and a
# different retelling is neither a merge nor a conflict the user should arbitrate.
FREE_TEXT = {"description", "technical_details", "notes", "text", "explanation"}


def group_of(kind: str) -> str:
    return GROUPS.get(kind, kind)


def identity(kind: str, data: dict) -> tuple:
    """The normalised dimensions that decide whether two entries are the same thing."""
    return tuple(fn(data.get(name)) for name, fn in KEY_FIELDS.get(group_of(kind), ()))


def same_entity(kind: str, a: dict, b: dict) -> bool:
    """True when both describe one entity: primary dimension equal and non-empty, the rest equal or absent.

    Tolerating an absent secondary dimension is what lets an older résumé that
    omitted a major, an issuer or a job title still land on the canonical fact
    instead of forking a near-duplicate.
    """
    ka, kb = identity(kind, a), identity(kind, b)
    if not ka or not ka[0] or ka[0] != kb[0]:
        return False
    return all(x == y or not x or not y for x, y in zip(ka[1:], kb[1:]))


# ── merging ──────────────────────────────────────────────────────────────────

class Outcome:
    DUPLICATE = "duplicate"      # A
    ENRICHED = "enriched"        # B
    CONFLICT = "conflict"        # C
    NOVEL = "novel"              # D
    REVIEW = "review"            # extracted source data needs user resolution


def normalizer_for(kind: str, field: str):
    """The comparison a field deserves: its own if it helps decide identity, plain folding otherwise.

    Without this, the very spellings normalisation exists to reconcile came back
    as conflicts — "CED Engineering" vs "CED Engineering, Inc." is one employer
    written two ways, not a disagreement to put in front of the user. A date or a
    GPA has no such normaliser, so it still conflicts, which is the point.
    """
    for name, fn in KEY_FIELDS.get(group_of(kind), ()):
        if name == field:
            return fn
    return norm_text


def merge(stored: dict, incoming: dict, kind: str = "") -> tuple[dict, list[str], list[tuple[str, str, str]]]:
    """(merged data, fields gained, [(field, stored, incoming)] conflicts).

    Lists are unioned; an empty scalar is filled; a scalar both sides genuinely
    state differently is reported, never overwritten.
    """
    merged, gained, conflicts = dict(stored), [], []
    for field, value in (incoming or {}).items():
        if value in (None, "", [], {}):
            continue
        current = merged.get(field)
        if isinstance(value, list):
            have = [str(x) for x in current or []]
            seen = {norm_text(x) for x in have}
            added = []
            for x in value:
                key = norm_text(x)
                if str(x).strip() and key not in seen:
                    seen.add(key)
                    added.append(x)
            if added:
                merged[field] = have + added
                gained.append(field)
        elif not current:
            merged[field] = value
            gained.append(field)
        elif field in FREE_TEXT:
            continue
        elif normalizer_for(kind, field)(current) == normalizer_for(kind, field)(value):
            continue
        else:
            conflicts.append((field, str(current), str(value)))
    return merged, gained, conflicts


# ── reconciliation against the database ──────────────────────────────────────

def _link(db, fact: CandidateFact, source: ResumeSource, locator: str = "", raw: str = "") -> None:
    """Record that `source` supports `fact`. One link per (fact, document)."""
    if source is None:
        return
    row = (db.query(CandidateFactSource)
           .filter(CandidateFactSource.candidate_fact_id == fact.id,
                   CandidateFactSource.resume_source_id == source.id).first())
    if row is None:
        db.add(CandidateFactSource(candidate_fact_id=fact.id, resume_source_id=source.id,
                                   locator=(locator or "")[:200], raw_text=(raw or "")[:4000] or None))
        db.flush()


def _raise_conflict(db, fact: CandidateFact, source, field, stored_value, proposed) -> bool:
    """Open a review conflict, unless the user has already been shown this exact disagreement.

    Keyed on the value being proposed: re-importing the same résumé, or a third one
    that repeats what the second said, must not pile up duplicate review items —
    whether the first is still waiting or the user already settled it.
    """
    seen = (db.query(FactConflict)
            .filter(FactConflict.candidate_fact_id == fact.id, FactConflict.field == field,
                    FactConflict.proposed_value == proposed).first())
    if seen is not None:
        return False
    db.add(FactConflict(candidate_fact_id=fact.id, field=field, current_value=stored_value,
                        proposed_value=proposed, resume_source_id=getattr(source, "id", None)))
    db.flush()
    return True


def kinds_in_group(group: str) -> list[str]:
    """"employment" -> ["experience", "internship"]: one résumé's "intern" is another's "experience"."""
    return [k for k in F.KINDS if group_of(k) == group] or [group]


def _match(db, kind: str, data: dict, parent_id):
    """The canonical fact this item describes, or None. An exact match wins over a looser one.

    Achievements are only ever compared with their own parent's children, so the
    same bullet under two different jobs stays two facts with two parents.
    """
    if kind == "achievement":
        return _match_achievement(db, data, parent_id)
    q = db.query(CandidateFact).filter(CandidateFact.kind.in_(kinds_in_group(group_of(kind))))
    if group_of(kind) == "achievement":
        q = q.filter(CandidateFact.parent_id == parent_id)
    loose = None
    for row in q.order_by(CandidateFact.id).all():
        if (group_of(kind) == "employment" and
                (row.data or {}).get("title") == "Role" and data.get("title") and
                norm_org((row.data or {}).get("employer")) == norm_org(data.get("employer"))):
            return row
        if not same_entity(kind, data, row.data or {}):
            # A real user-entered title at the same employer is a conflict,
            # not permission to fork a second canonical employment row.
            if (group_of(kind) == "employment" and
                    norm_org((row.data or {}).get("employer")) == norm_org(data.get("employer"))):
                loose = loose or row
            continue
        if identity(kind, data) == identity(row.kind, row.data or {}):
            return row
        loose = loose or row
    return loose


_BULLET_NUMBER_RE = re.compile(r"\$?\d[\d,.]*(?:\s*[%KMBkmb+])*")
_BULLET_WORD_RE = re.compile(r"[a-zA-Z]+")
_BULLET_STOPWORDS = {"a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "by", "for", "with", "from", "as", "is", "was", "were", "be", "via"}


def bullet_numeric_anchors(text: str) -> set[str]:
    return {x.replace(" ", "") for x in _BULLET_NUMBER_RE.findall(text or "")}


def bullet_similarity(left: str, right: str) -> float:
    """Deterministic similarity shared by evidence and résumé merging."""
    a = {w for w in _BULLET_WORD_RE.findall((left or "").casefold()) if w not in _BULLET_STOPWORDS}
    b = {w for w in _BULLET_WORD_RE.findall((right or "").casefold()) if w not in _BULLET_STOPWORDS}
    jaccard = len(a & b) / len(a | b) if a and b else 0.0
    return jaccard


def bullet_match(left: str, right: str) -> tuple[bool, bool]:
    """Return (duplicate, numeric_conflict). Exact normalized text always matches."""
    if norm_text(left) == norm_text(right):
        return True, False
    def anchors(text):
        values = {x.casefold() for x in bullet_numeric_anchors(text)}
        for number in re.findall(r"\d[\d,.]*", text or ""):
            if re.search(rf"{re.escape(number)}\s*percent", text or "", re.I):
                values.discard(number.casefold())
                values.add(number.replace(",", "") + "%")
        return values
    a, b = anchors(left), anchors(right)
    if a and b and a != b:
        return False, True
    return bullet_similarity(left, right) >= (0.40 if a else 0.50), False


def _match_achievement(db, data: dict, parent_id):
    """Compare bullets only among children of the same canonical parent."""
    text = str(data.get("text") or "")
    for row in (db.query(CandidateFact)
                .filter(CandidateFact.kind == "achievement", CandidateFact.parent_id == parent_id)
                .order_by(CandidateFact.id).all()):
        duplicate, _ = bullet_match(text, (row.data or {}).get("text", ""))
        if duplicate:
            return row
    return None


def reconcile_item(db, item: dict, source, parent: CandidateFact | None = None) -> tuple[CandidateFact | None, str]:
    """(canonical fact, outcome) for one extracted item. Nothing here overwrites a stored value."""
    kind = item.get("kind") or ""
    try:
        data = F.validate_data(kind, item.get("data") or {})
    except ValueError as e:
        logger.info(f"evidence import requires review for {kind}: {e}")
        return None, "dropped"

    parent_id = parent.id if parent is not None else None
    row = _match(db, kind, data, parent_id)
    if row is None:
        row = CandidateFact(kind=kind, parent_id=parent_id, data=data, verified=False,
                            source=f"import:{getattr(source, 'filename', None) or 'manual'}"[:200])
        db.add(row)
        db.flush()
        _link(db, row, source, item.get("locator", ""), item.get("raw", ""))
        if kind == "achievement":
            for old in (db.query(CandidateFact)
                        .filter(CandidateFact.kind == "achievement", CandidateFact.parent_id == parent_id,
                                CandidateFact.id != row.id).all()):
                similar, numeric_conflict = bullet_match(data.get("text", ""), (old.data or {}).get("text", ""))
                if numeric_conflict:
                    _raise_conflict(db, row, source, "text", (old.data or {}).get("text", ""), data.get("text", ""))
                    return row, Outcome.CONFLICT
        return row, Outcome.NOVEL

    stored = dict(row.data or {})
    # Repair only the legacy import sentinels. Arbitrary user-entered values
    # remain real values and therefore still produce conflicts.
    if kind in ("experience", "internship"):
        if stored.get("title") == "Role" and data.get("title"):
            stored["title"] = ""
        if stored.get("employer") == "Unknown" and data.get("employer"):
            stored["employer"] = ""
    merged, gained, conflicts = merge(stored, data, kind)
    if gained:
        row.data = merged
        row.updated_at = utcnow()
    opened = sum(_raise_conflict(db, row, source, field, stored, proposed) for field, stored, proposed in conflicts)
    _link(db, row, source, item.get("locator", ""), item.get("raw", ""))
    return row, Outcome.CONFLICT if opened else (Outcome.ENRICHED if gained else Outcome.DUPLICATE)


def reconcile(db, items: list, source=None) -> dict:
    """Fold one résumé's extracted items into the canonical facts; returns a per-outcome tally."""
    tally = {Outcome.NOVEL: 0, Outcome.ENRICHED: 0, Outcome.DUPLICATE: 0,
             Outcome.CONFLICT: 0, Outcome.REVIEW: 0, "dropped": 0, "report": []}
    def report(item, fact, outcome, parent=None):
        data = item.get("data") or {}
        headline = (data.get("title") or data.get("name") or data.get("text") or
                    data.get("institution") or data.get("organization") or data.get("employer") or "")
        tally["report"].append({"kind": item.get("kind"), "fact_id": fact.id if fact else None,
                                 "parent_id": parent.id if parent else None, "headline": headline,
                                 "outcome": outcome, "source": getattr(source, "filename", None),
                                 "locator": item.get("locator", "")})
    for item in items or []:
        fact, outcome = reconcile_item(db, item, source)
        tally[outcome] = tally.get(outcome, 0) + 1
        if outcome == "dropped":
            tally[Outcome.REVIEW] += 1
        report(item, fact, outcome)
        if fact is None:
            continue
        for child in item.get("children") or []:
            child_fact, child_outcome = reconcile_item(db, child, source, parent=fact)
            tally[child_outcome] = tally.get(child_outcome, 0) + 1
            if child_outcome == "dropped":
                tally[Outcome.REVIEW] += 1
            report(child, child_fact, child_outcome, fact)
    return tally


def resolve_conflict(db, conflict: FactConflict, value: str) -> CandidateFact:
    """Write the value the user chose onto the canonical fact and close the review item."""
    fact = db.get(CandidateFact, conflict.candidate_fact_id)
    if fact is None:
        raise LookupError("the fact this conflict belongs to no longer exists")
    fact.data = {**(fact.data or {}), conflict.field: value}
    try:
        fact.data = F.validate_data(fact.kind, fact.data)
    except ValueError as e:
        raise ValueError(f"{conflict.field}: {e}") from None
    fact.updated_at = utcnow()
    conflict.status, conflict.resolved_value, conflict.resolved_at = "resolved", value, utcnow()
    return fact


# ── extraction: résumé JSON -> items ─────────────────────────────────────────
# Kept pure so it is tested without a database. `locator`/`raw` travel with each
# item so a fact can point back at the line of the document that supports it.

_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_PART_RE = re.compile(r"(?:([a-z]{3})[a-z]*\.?\s+)?((?:19|20)\d{2})|(present|current|now)", re.I)
_RANGE_SPLIT_RE = re.compile(r"\s*(?:[–—−]|\s-\s|-(?=\s*[A-Za-z]{3}|\s*(?:19|20)\d{2})|\bto\b)\s*")


def _one_date(text: str) -> str:
    m = _PART_RE.search(text or "")
    if not m:
        return ""
    if m.group(3):
        return "present"
    mon = _MONTHS.get((m.group(1) or "").lower()[:3])
    return f"{m.group(2)}-{mon:02d}" if mon else m.group(2)


def parse_date_range(text: str) -> tuple[str, str]:
    """'May 2021 – Present' -> ('2021-05', 'present'); anything unparseable -> ''."""
    parts = [p for p in _RANGE_SPLIT_RE.split(str(text or "").strip()) if p]
    if not parts:
        return "", ""
    start = _one_date(parts[0])
    end = _one_date(parts[-1]) if len(parts) > 1 else ""
    return start, end


def _split_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in re.split(r"[,;•|]", str(v or "")) if x.strip()]


def _bullets(items, locator: str) -> list[dict]:
    return [{"kind": "achievement", "data": {"text": str(b).strip()},
             "locator": f"{locator}.bullets[{j}]", "raw": str(b).strip()}
            for j, b in enumerate(items or []) if str(b).strip()]


def facts_from_resume_json(json_data: dict) -> list[dict]:
    """Résumé json_data -> [{kind, data, locator, raw, children:[...]}]; pure, so it is tested without a DB."""
    out = []
    for i, e in enumerate((json_data or {}).get("experience") or []):
        if not isinstance(e, dict) or not (e.get("company") or e.get("title")):
            continue
        loc = f"experience[{i}]"
        start = str(e.get("start_date") or "").strip()
        end = str(e.get("end_date") or "").strip()
        if not (start or end):
            start, end = parse_date_range(e.get("date") or e.get("dates") or "")
        title = str(e.get("title") or "").strip()
        employer = str(e.get("company") or "").strip()
        out.append({
            "kind": "internship" if "intern" in title.lower() else "experience",
            "data": {"employer": employer, "title": title,
                     "location": str(e.get("location") or ""), "start_date": start, "end_date": end,
                     "employment_type": str(e.get("employment_type") or ""),
                     "technologies": _split_list(e.get("technologies")),
                     "description": str(e.get("description") or ""),
                     "notes": "" if start else f"Imported date: {e.get('date') or ''}".strip(": ")},
            "locator": loc, "raw": f"{title} — {e.get('company') or ''} ({e.get('date') or ''})",
            "children": _bullets(e.get("bullets"), loc),
        })
    for i, ed in enumerate((json_data or {}).get("education") or []):
        if not isinstance(ed, dict) or not ed.get("school"):
            continue
        start, grad = str(ed.get("start_date") or "").strip(), str(ed.get("graduation_date") or "").strip()
        if not (start or grad):
            start, grad = parse_date_range(ed.get("years") or ed.get("year") or "")
            # A lone year/date is a graduation date, not an education start.
            if start and not grad:
                grad, start = start, ""
        if not grad:
            grad = _one_date(str(ed.get("years") or ed.get("year") or ""))
        out.append({"kind": "education", "locator": f"education[{i}]",
                    "raw": f"{ed.get('degree') or ''} — {ed['school']} ({ed.get('years') or ed.get('year') or ''})",
                    "data": {"institution": str(ed["school"]).strip(), "degree": str(ed.get("degree") or ""),
                             "major": str(ed.get("major") or ""), "minor": str(ed.get("minor") or ""),
                             "gpa": str(ed.get("gpa") or ""), "start_date": start,
                             "location": str(ed.get("location") or ""),
                             "graduation_date": grad if grad != "present" else "",
                             "coursework": _split_list(ed.get("coursework")), "honors": _split_list(ed.get("honors"))}})
    for i, pr in enumerate((json_data or {}).get("projects") or []):
        if not isinstance(pr, dict) or not pr.get("name"):
            continue
        loc = f"projects[{i}]"
        out.append({"kind": "project", "locator": loc, "raw": str(pr["name"]).strip(),
                    "data": {"name": str(pr["name"]).strip(), "role": str(pr.get("role") or ""),
                             "description": str(pr.get("description") or ""),
                             "technologies": _split_list(pr.get("technologies")),
                             "repository_url": str(pr.get("repository_url") or ""),
                             "project_url": str(pr.get("project_url") or ""),
                             "start_date": str(pr.get("start_date") or ""),
                             "end_date": str(pr.get("end_date") or "")},
                    "children": _bullets(pr.get("bullets"), loc)})
    skills = (json_data or {}).get("skills") or {}
    groups = skills.items() if isinstance(skills, dict) else [("", skills)]
    for category, items in groups:
        for k, name in enumerate(_split_list(items)):
            out.append({"kind": "skill", "data": {"name": name, "category": str(category or "")},
                        "locator": f"skills.{category or 'all'}[{k}]", "raw": name})
    for i, pub in enumerate((json_data or {}).get("publications") or []):
        if isinstance(pub, dict) and pub.get("title"):
            out.append({"kind": "publication", "locator": f"publications[{i}]", "raw": str(pub["title"]).strip(),
                        "data": {"title": str(pub["title"]).strip(),
                                 "venue": str(pub.get("description") or pub.get("venue") or ""),
                                 "date": str(pub.get("date") or ""), "authors": str(pub.get("authors") or ""),
                                 "url": str(pub.get("url") or "")}})
    for i, research in enumerate((json_data or {}).get("research") or []):
        if not isinstance(research, dict) or not research.get("organization"):
            continue
        loc = f"research[{i}]"
        start = str(research.get("start_date") or "").strip()
        end = str(research.get("end_date") or "").strip()
        if not (start or end):
            start, end = parse_date_range(research.get("date") or "")
        out.append({"kind": "research", "locator": loc,
                    "raw": f"{research.get('title') or ''} — {research.get('organization')}",
                    "data": {"organization": str(research.get("organization") or "").strip(),
                             "title": str(research.get("title") or "").strip(),
                             "location": str(research.get("location") or ""), "start_date": start, "end_date": end,
                             "research_area": str(research.get("research_area") or ""),
                             "methods": _split_list(research.get("methods")),
                             "technologies": _split_list(research.get("technologies")),
                             "responsibilities": [], "publications": _split_list(research.get("publications"))},
                    "children": _bullets(research.get("bullets"), loc)})
    for i, cert in enumerate((json_data or {}).get("certifications") or []):
        if isinstance(cert, dict) and cert.get("name"):
            out.append({"kind": "certification", "locator": f"certifications[{i}]", "raw": str(cert["name"]),
                        "data": {k: str(cert.get(k) or "") for k in ("name", "issuer", "date", "expires", "credential_url")}})
    for i, link in enumerate((json_data or {}).get("links") or []):
        if isinstance(link, dict) and link.get("url"):
            out.append({"kind": "link", "locator": f"links[{i}]", "raw": str(link["url"]),
                        "data": {"label": str(link.get("label") or ""), "url": str(link["url"]).strip()}})
    return out


def digest(pdf_bytes: bytes) -> str:
    return hashlib.sha256(pdf_bytes).hexdigest()


# ── the import job ───────────────────────────────────────────────────────────
# Upload extracts text (fast, offline) and stores a pending ResumeSource; this
# runs afterwards in the background because structuring N résumés is N LLM calls.
# One document failing never stops the rest of the batch.

IMPORT_JOB = "resume_import"


async def import_pending(source_ids: list[int] | None = None) -> str:
    """Structure exactly ``source_ids``; an omitted list is a recovery scan."""
    from backend.copilot.knowledge import extract_claims, index_document, index_facts
    from backend.models.db import SessionLocal

    db = SessionLocal()
    try:
        q = db.query(ResumeSource).filter(ResumeSource.status == "pending")
        pending = [s.id for s in q.order_by(ResumeSource.id).all()] if source_ids is None else list(source_ids)
    finally:
        db.close()

    done, failed, totals = 0, 0, {}
    for source_id in pending:
        db = SessionLocal()
        try:
            source = db.get(ResumeSource, source_id)
            if source is None or source.status != "pending":
                continue
            source.status = "parsing"
            text = source.parsed_text or json.dumps(source.parsed_json or {}, sort_keys=True)
            db.commit()
        finally:
            db.close()

        try:
            from backend.job_monitor import get_limiter
            async with get_limiter("resume_parsing"):
                parsed, provider, model = await extract_claims(text)
        except Exception as e:
            db = SessionLocal()
            try:
                source = db.get(ResumeSource, source_id)
                source.status, source.error = "waiting_for_ai", str(getattr(e, "detail", e))[:500]
                db.commit()
            finally:
                db.close()
            failed += 1
            logger.warning(f"résumé import failed for source {source_id}: {e}")
            continue

        db = SessionLocal()
        try:
            source = db.get(ResumeSource, source_id)
            tally = reconcile(db, parsed, source)
            source.parsed_json, source.result, source.report = {"claims": parsed}, tally, tally.get("report", [])
            source.status, source.imported_at, source.processed_at, source.error = "imported", utcnow(), utcnow(), None
            db.commit()
            # Embeddings are useful but must not discard a source when Ollama is
            # offline. The document and canonical claims remain durable.
            try:
                await index_document(db, source_id)
                await index_facts(db)
                db.commit()
            except Exception as embedding_error:
                source = db.get(ResumeSource, source_id)
                source.status = "waiting_for_ai"
                source.processing_error = f"Embedding unavailable: {str(embedding_error)[:400]}"
                db.commit()
            for key, count in tally.items():
                if isinstance(count, int):
                    totals[key] = totals.get(key, 0) + count
            done += 1
        except Exception as e:
            db.rollback()
            source = db.get(ResumeSource, source_id)
            if source is not None:
                source.status, source.error = "failed", str(e)[:500]
                db.commit()
            failed += 1
            logger.exception(f"reconciling source {source_id} failed: {e}")
        finally:
            db.close()

    parts = [f"{done} résumé{'' if done == 1 else 's'} imported"]
    if failed:
        parts.append(f"{failed} failed")
    parts.append(f"{totals.get(Outcome.NOVEL, 0)} new facts, {totals.get(Outcome.ENRICHED, 0)} enriched, "
                 f"{totals.get(Outcome.DUPLICATE, 0)} already known, {totals.get(Outcome.CONFLICT, 0)} conflicts to review, "
                 f"{totals.get(Outcome.REVIEW, 0)} needing review")
    return "; ".join(parts)


def recover_resume_imports() -> list[int]:
    """Restore sources interrupted by a prior process and queue their exact IDs."""
    from backend.models.db import SessionLocal
    db = SessionLocal()
    try:
        rows = db.query(ResumeSource).filter(ResumeSource.status.in_(["pending", "parsing"])).all()
        for row in rows:
            row.status = "pending"
        ids = [row.id for row in rows]
        db.commit()
        return ids
    finally:
        db.close()


def _fill_contact(db, header: dict) -> int:
    """Contact answers the résumé header gives, only where the Persona has none yet."""
    from backend.api.routes_persona import _contact_from_header
    from backend.models.db import Persona

    persona = db.query(Persona).filter(Persona.id == 1).first()
    if persona is None:
        return 0
    contact, added = dict(persona.contact or {}), 0
    for key, value in _contact_from_header(header or {}).items():
        if value and not contact.get(key):
            contact[key] = value
            added += 1
    if added:
        persona.contact = contact
    return added
