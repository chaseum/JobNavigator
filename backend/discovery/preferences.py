"""Job Preferences: the one canonical statement of what the user is looking for.

There is exactly one authoritative copy, stored as the `job_preferences` Setting
row. It is not a new table because it is a singleton config document, which is
what the settings table already is, and it is not `Persona.preferences` because
that node is the autofill screening answers — it is fed verbatim into
application-form prompts, and search criteria have no business there.

Saved filters (`saved_job_filters`) are the same shape, named, and each one can
be switched on; every active one contributes to the Discovery Plan.
"""
import json
import logging

from backend.models.db import Setting
from backend.discovery import taxonomy as T

logger = logging.getLogger("jobnavigator.discovery.preferences")

SETTING_KEY = "job_preferences"
FILTERS_KEY = "saved_job_filters"

# The shape, and the defaults a fresh install starts from. A default that
# searched for nothing would leave the feed permanently empty, so the starting
# point is the intended workflow: US software roles at the start of a career.
DEFAULTS = {
    "countries": ["US"],
    "locations": [],                 # free-text places: "Seattle, WA", "California"
    "job_functions": ["software_engineering"],
    "levels": ["intern", "new_grad", "entry"],
    "job_types": ["fulltime", "internship"],
    "work_models": ["onsite", "hybrid", "remote"],
    "date_posted_days": 7,
    "max_years_experience": 2,
    "companies": [],
    "excluded_companies": [],
    "minimum_salary": None,
    "sponsorship": "any",            # any | required
    "industries": [],
    "title_query": "",               # a committed search box term, not a keystroke
}

_LIST_FIELDS = {"countries", "locations", "job_functions", "levels", "job_types",
                "work_models", "companies", "excluded_companies", "industries"}
_ENUMS = {
    "job_functions": set(T.FUNCTION_IDS),
    "levels": set(T.LEVEL_IDS),
    "job_types": set(T.JOB_TYPE_IDS),
    "work_models": set(T.WORK_MODEL_IDS),
}
SPONSORSHIP_VALUES = {"any", "required"}


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [v for v in value if v is not None] if isinstance(value, (list, tuple)) else []


def _as_int(value, floor=0):
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= floor else None


def normalize(raw: dict | None) -> dict:
    """A well-formed preferences document, whatever came in.

    Unknown ids are dropped rather than rejected: the taxonomy grows, and a
    stored value from a future/older build must not make the whole document
    unreadable.
    """
    src = dict(DEFAULTS)
    if isinstance(raw, dict):
        src.update({k: v for k, v in raw.items() if k in DEFAULTS})
    out = {}
    for key, default in DEFAULTS.items():
        value = src.get(key, default)
        if key in _LIST_FIELDS:
            items = [str(v).strip() for v in _as_list(value) if str(v).strip()]
            allowed = _ENUMS.get(key)
            if allowed:
                items = [v for v in items if v in allowed]
            elif key == "countries":
                items = [v.upper()[:2] for v in items]
            # order-preserving dedupe
            out[key] = list(dict.fromkeys(items))
        elif key == "date_posted_days":
            out[key] = _as_int(value, floor=1) or DEFAULTS[key]
        elif key == "max_years_experience":
            out[key] = _as_int(value, floor=0)          # None = no ceiling
        elif key == "minimum_salary":
            out[key] = _as_int(value, floor=0)
        elif key == "sponsorship":
            out[key] = value if value in SPONSORSHIP_VALUES else "any"
        else:
            out[key] = str(value or "").strip()
    return out


def load(db) -> dict:
    row = db.query(Setting).filter(Setting.key == SETTING_KEY).first()
    if not row or not row.value:
        return normalize(None)
    try:
        return normalize(json.loads(row.value))
    except (TypeError, ValueError):
        logger.warning("%s is not valid JSON; falling back to defaults", SETTING_KEY)
        return normalize(None)


def save(db, raw: dict) -> dict:
    """Persist a whole preferences document (already merged by the caller)."""
    prefs = normalize(raw)
    row = db.query(Setting).filter(Setting.key == SETTING_KEY).first()
    payload = json.dumps(prefs)
    if row:
        row.value = payload
    else:
        db.add(Setting(key=SETTING_KEY, value=payload,
                       description="Canonical job-search preferences (Jobs screen + Settings)"))
    db.commit()
    return prefs


def patch(db, changes: dict) -> dict:
    """Merge a partial update into the stored document and save it."""
    return save(db, {**load(db), **(changes or {})})


# ── Saved filters ────────────────────────────────────────────────────────────
# A saved filter holds USER CRITERIA only. There is deliberately no place in this
# shape for a source, a board, an interval, a result count or a scoring depth.

def load_filters(db) -> list[dict]:
    row = db.query(Setting).filter(Setting.key == FILTERS_KEY).first()
    try:
        raw = json.loads(row.value) if row and row.value else []
    except (TypeError, ValueError):
        logger.warning("%s is not valid JSON; treating it as empty", FILTERS_KEY)
        return []
    out = []
    for i, entry in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(entry, dict):
            continue
        out.append({
            "id": str(entry.get("id") or f"filter_{i}"),
            "name": str(entry.get("name") or "Saved filter").strip(),
            "active": bool(entry.get("active", True)),
            "criteria": normalize(entry.get("criteria")),
        })
    return out


def save_filters(db, filters) -> list[dict]:
    clean = []
    for i, entry in enumerate(filters or []):
        if not isinstance(entry, dict):
            continue
        clean.append({
            "id": str(entry.get("id") or f"filter_{i}"),
            "name": str(entry.get("name") or "Saved filter").strip()[:80],
            "active": bool(entry.get("active", True)),
            "criteria": normalize(entry.get("criteria")),
        })
    row = db.query(Setting).filter(Setting.key == FILTERS_KEY).first()
    payload = json.dumps(clean)
    if row:
        row.value = payload
    else:
        db.add(Setting(key=FILTERS_KEY, value=payload,
                       description="Named job-criteria filters; active ones add to discovery"))
    db.commit()
    return clean


def active_criteria(db) -> list[dict]:
    """Every criteria document discovery should cover: the main preferences plus
    each active saved filter. Overlap is the planner's problem, not this one's."""
    return [load(db)] + [f["criteria"] for f in load_filters(db) if f["active"]]


def taxonomy() -> dict:
    """What the UI needs to render every picker, so labels live in one place."""
    return {
        "job_functions": [{"id": f["id"], "label": f["label"]} for f in T.JOB_FUNCTIONS],
        "levels": [{"id": k, "label": v} for k, v in T.LEVELS],
        "job_types": [{"id": k, "label": v} for k, v in T.JOB_TYPES],
        "work_models": [{"id": k, "label": v} for k, v in T.WORK_MODELS],
        "date_posted_days": T.DATE_POSTED_DAYS,
        "sponsorship": sorted(SPONSORSHIP_VALUES),
    }
