"""Candidate fact kinds, validation, provenance ids and the text the LLM sees.

A fact's provenance id is "<kind>_<id>" (experience_7, achievement_12). Identity
answers that live on Persona are cited as "identity.<key>". Nothing else is a
valid source for a résumé claim.
"""
import hashlib
import json
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_DATE_RE = re.compile(r"^(\d{4}(-(0[1-9]|1[0-2]))?|present)?$")


class _Fact(BaseModel):
    # Unknown keys are a typo or a stale client, never data worth keeping silently.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _dates(*names):
    @field_validator(*names)
    @classmethod
    def check(cls, v):
        v = (v or "").strip().lower()
        if not _DATE_RE.match(v):
            raise ValueError("use YYYY, YYYY-MM or 'present'")
        return v
    return check


class Employment(_Fact):
    employer: str = Field(min_length=1)
    title: str = Field(min_length=1)
    location: str = ""
    start_date: str = ""
    end_date: str = ""
    employment_type: Literal["", "full_time", "part_time", "internship", "contract", "co_op", "volunteer"] = ""
    description: str = ""
    technologies: list[str] = []
    responsibilities: list[str] = []
    metrics: list[str] = []
    domain: str = ""
    notes: str = ""
    check_dates = _dates("start_date", "end_date")


class Education(_Fact):
    institution: str = Field(min_length=1)
    degree: str = ""
    major: str = ""
    minor: str = ""
    gpa: str = ""
    location: str = ""
    start_date: str = ""
    graduation_date: str = ""
    coursework: list[str] = []
    honors: list[str] = []
    check_dates = _dates("start_date", "graduation_date")


class Project(_Fact):
    name: str = Field(min_length=1)
    role: str = ""
    repository_url: str = ""
    project_url: str = ""
    start_date: str = ""
    end_date: str = ""
    description: str = ""
    technologies: list[str] = []
    metrics: list[str] = []
    technical_details: str = ""
    check_dates = _dates("start_date", "end_date")


class Research(_Fact):
    organization: str = Field(min_length=1)
    title: str = ""
    location: str = ""
    start_date: str = ""
    end_date: str = ""
    research_area: str = ""
    methods: list[str] = []
    technologies: list[str] = []
    responsibilities: list[str] = []
    publications: list[str] = []
    check_dates = _dates("start_date", "end_date")


class Skill(_Fact):
    name: str = Field(min_length=1)
    category: str = ""
    proficiency: str = ""          # free text: "daily for 2 years", "coursework only"
    evidence_ids: list[str] = []   # provenance ids of the facts where it was actually used


class Certification(_Fact):
    name: str = Field(min_length=1)
    issuer: str = ""
    date: str = ""
    expires: str = ""
    credential_url: str = ""
    check_dates = _dates("date", "expires")


class Achievement(_Fact):
    text: str = Field(min_length=1)
    metric: str = ""


class Publication(_Fact):
    title: str = Field(min_length=1)
    venue: str = ""
    date: str = ""
    authors: str = ""
    url: str = ""
    check_dates = _dates("date")


class Link(_Fact):
    label: str = ""
    url: str = Field(min_length=1)


KINDS: dict[str, type[_Fact]] = {
    "experience": Employment,
    "internship": Employment,
    "education": Education,
    "project": Project,
    "research": Research,
    "skill": Skill,
    "certification": Certification,
    "achievement": Achievement,
    "publication": Publication,
    "link": Link,
}
# kinds an achievement may hang under
PARENT_KINDS = {"experience", "internship", "project", "research", "education"}


def validate_data(kind: str, data: dict) -> dict:
    """Validated, normalised data for `kind`; raises ValueError with a readable message."""
    model = KINDS.get(kind)
    if model is None:
        raise ValueError(f"unknown fact kind {kind!r}")
    try:
        return model.model_validate(data or {}).model_dump()
    except ValidationError as e:
        parts = [f"{'.'.join(str(x) for x in err['loc']) or kind}: {err['msg']}" for err in e.errors()]
        raise ValueError("; ".join(parts)) from None


def fact_ref(fact) -> str:
    return f"{fact.kind}_{fact.id}"


_REF_RE = re.compile(r"^([a-z]+)_(\d+)$")


def parse_ref(ref: str) -> Optional[tuple[str, int]]:
    m = _REF_RE.match(str(ref or ""))
    return (m.group(1), int(m.group(2))) if m and m.group(1) in KINDS else None


_IDENTITY_REF_RE = re.compile(r"^identity\.[a-z_]+$")


def strip_ref(raw) -> str:
    """A model citation with surrounding whitespace and exactly one pair of square brackets removed.

    The prompt shows facts as "[education_10] ..."; the brackets are delimiters,
    never part of the id. Nothing else is repaired.
    """
    s = raw.strip() if isinstance(raw, str) else ""
    return s[1:-1].strip() if len(s) > 1 and s[0] == "[" and s[-1] == "]" else s


def normalize_ref(raw) -> Optional[str]:
    """The canonical provenance id ("education_10", "identity.authorized_us") a citation names, or None when it names none.

    Shape only: whether the id is a verified fact is the caller's check.
    """
    s = strip_ref(raw)
    return s if parse_ref(s) or _IDENTITY_REF_RE.match(s) else None


def schema_for_ui() -> dict:
    """Field list per kind so the Profile screen renders forms without duplicating this file."""
    out = {}
    for kind, model in KINDS.items():
        fields = []
        for name, f in model.model_fields.items():
            ann = f.annotation
            if ann == list[str]:
                ftype = "list"
            elif getattr(ann, "__origin__", None) is Literal:
                ftype = "enum"
            elif name.endswith("date") or name == "expires" or name == "date":
                ftype = "date"
            elif name.endswith("url"):
                ftype = "url"
            elif name in ("description", "technical_details", "notes", "text"):
                ftype = "textarea"
            else:
                ftype = "text"
            spec = {"name": name, "type": ftype, "required": f.is_required()}
            if ftype == "enum":
                spec["options"] = list(ann.__args__)
            if name == "evidence_ids":
                spec["type"] = "refs"
            fields.append(spec)
        out[kind] = fields
    return out


# ── what the model sees ─────────────────────────────────────────────────────

def _span(d: dict, start="start_date", end="end_date") -> str:
    s, e = d.get(start) or "", d.get(end) or ""
    return f" ({s or '?'} – {e or '?'})" if s or e else ""


def fact_headline(kind: str, d: dict) -> str:
    """One line naming the fact; the fields the LLM may never alter live here."""
    if kind in ("experience", "internship"):
        return f"{d.get('title')} at {d.get('employer')}{_span(d)}" + (f", {d['location']}" if d.get("location") else "")
    if kind == "education":
        deg = " ".join(x for x in (d.get("degree"), d.get("major")) if x)
        return f"{deg or 'Studies'} at {d.get('institution')}{_span(d, 'start_date', 'graduation_date')}"
    if kind == "project":
        return f"Project: {d.get('name')}" + (f" ({d['role']})" if d.get("role") else "") + _span(d)
    if kind == "research":
        return f"Research: {d.get('title') or 'Research entry'} at {d.get('organization')}{_span(d)}"
    if kind == "skill":
        return f"Skill: {d.get('name')}" + (f" [{d['category']}]" if d.get("category") else "") + (f" — {d['proficiency']}" if d.get("proficiency") else "")
    if kind == "certification":
        return f"Certification: {d.get('name')}" + (f" ({d['issuer']})" if d.get("issuer") else "")
    if kind == "achievement":
        return d.get("text", "") + (f" [metric: {d['metric']}]" if d.get("metric") else "")
    if kind == "publication":
        return f"Publication: {d.get('title')}" + (f", {d['venue']}" if d.get("venue") else "")
    if kind == "link":
        return f"Link: {d.get('label') or ''} {d.get('url')}".strip()
    return json.dumps(d)


_DETAIL_SKIP = {"employer", "title", "name", "institution", "organization", "text", "start_date", "end_date",
                "graduation_date", "location", "role", "category", "proficiency", "metric", "evidence_ids",
                "label", "url", "venue", "issuer"}


def fact_text(kind: str, d: dict) -> str:
    """Headline plus every other non-empty field; the evidence corpus for one fact."""
    lines = [fact_headline(kind, d)]
    for k, v in d.items():
        if k in _DETAIL_SKIP or not v:
            continue
        lines.append(f"  {k}: {', '.join(v) if isinstance(v, list) else v}")
    return "\n".join(lines)


def facts_prompt(facts) -> str:
    """Verified facts as "[ref] text" blocks, children indented under parents."""
    by_parent: dict = {}
    for f in facts:
        by_parent.setdefault(f.parent_id, []).append(f)
    ids = {f.id for f in facts}
    out = []

    def emit(f, depth):
        body = fact_text(f.kind, f.data or {}).replace("\n", "\n" + "  " * depth)
        out.append(f"{'  ' * depth}[{fact_ref(f)}] {body}")
        for c in by_parent.get(f.id, []):
            emit(c, depth + 1)

    for f in facts:
        if f.parent_id is None or f.parent_id not in ids:
            emit(f, 0)
    return "\n".join(out)


IDENTITY_KEYS = {
    "work_auth": ("authorized_us", "requires_sponsorship_now", "requires_sponsorship_future", "work_auth_type", "over_18"),
    "preferences": ("willing_to_relocate", "remote_preference", "desired_locations", "earliest_start"),
}


def identity_facts(persona) -> dict:
    """{"identity.<key>": value} for identity answers the user actually gave; never inferred."""
    out = {}
    for node, keys in IDENTITY_KEYS.items():
        src = getattr(persona, node, None) or {} if persona is not None else {}
        for k in keys:
            v = src.get(k)
            if v is not None and v != "" and v != []:
                out[f"identity.{k}"] = v
    return out


_IDENTITY_LABELS = {
    "authorized_us": "authorized to work in the US", "requires_sponsorship_now": "requires sponsorship now",
    "requires_sponsorship_future": "requires sponsorship in the future", "work_auth_type": "work authorization type",
    "over_18": "over 18", "willing_to_relocate": "willing to relocate", "remote_preference": "work arrangement",
    "desired_locations": "desired locations", "earliest_start": "earliest start",
}


def identity_headline(ref: str, value) -> str:
    key = ref.split(".", 1)[-1]
    shown = "yes" if value is True else "no" if value is False else str(value)
    return f"Your answer: {_IDENTITY_LABELS.get(key, key.replace('_', ' '))} — {shown}"


def load_verified(db):
    from backend.models.db import CandidateFact
    return (db.query(CandidateFact).filter(CandidateFact.verified == True)  # noqa: E712
            .order_by(CandidateFact.sort_order, CandidateFact.id).all())


def profile_version(db) -> str:
    """Short content hash of every verified fact plus identity answers; changes whenever what the pipeline may cite changes."""
    from backend.models.db import Persona
    persona = db.query(Persona).filter(Persona.id == 1).first()
    payload = [[f.id, f.kind, f.parent_id, f.data] for f in load_verified(db)]
    blob = json.dumps([payload, identity_facts(persona)], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]
