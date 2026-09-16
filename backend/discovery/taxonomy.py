"""The vocabulary the user actually picks from, and the deterministic parsers that
map a raw posting onto it.

Everything here is a pure function of a title/description string plus these
tables — no LLM, no network, no database. That is the point: the Preference Gate
runs on every discovered posting, so it has to be cheap, repeatable and testable,
and an LLM is none of the three. The structured Copilot analysis stays where it
was; it is richer, it costs money, and it only runs once a posting has already
earned it.

Stable ids are the canonical values (``software_engineering``, ``new_grad``).
Labels are presentation and never persisted.
"""
import functools
import re


@functools.lru_cache(maxsize=2048)
def _matcher(term: str):
    """A term matched as WHOLE words, both ends.

    Deliberately not `role_families.matches`, which treats a term as a prefix so
    that "prioriti" catches "prioritised". That is right for ranking résumé
    evidence and wrong here: "lead" then matched "leading", and every job
    description whose opening paragraph said "a leading AI company" came back
    classified as a Lead role. Seniority has to be an exact word or it is noise.
    """
    # Boundaries are letters and digits only. `role_families` also guards against
    # `+` and `#` so that "c" cannot match inside "c++" — but no term in THIS
    # vocabulary is a language name, and that guard made "staff" fail to match
    # "Staff+ Software Engineer", which let a whole run of staff-level postings
    # through a new-grad filter.
    parts = [re.escape(p) for p in str(term).split()]
    return re.compile(r"(?<![a-z0-9])" + r"\s+".join(parts) + r"(?![a-z0-9])")


def matches(term: str, text: str) -> bool:
    return bool(term) and _matcher(term).search(text) is not None

# ── Job functions ────────────────────────────────────────────────────────────
# `search_terms` is what the Discovery Planner sends to a job board; `title`
# is what classifies a posting that came back. They overlap but are not the
# same list: "developer" is a useful classifier and a terrible search term.
JOB_FUNCTIONS = [
    {
        "id": "software_engineering",
        "label": "Software Engineering",
        "search_terms": ["software engineer", "backend engineer", "frontend engineer",
                         "full stack engineer", "platform engineer"],
        "title": ["software engineer", "software developer", "backend", "back end", "frontend",
                  "front end", "full stack", "fullstack", "developer", "programmer",
                  "platform engineer", "infrastructure engineer", "systems engineer",
                  "mobile engineer", "ios engineer", "android engineer", "embedded engineer",
                  "swe", "software engineering"],
    },
    {
        "id": "data_ml",
        "label": "Data / ML",
        "search_terms": ["machine learning engineer", "data scientist", "data engineer",
                         "ai engineer"],
        "title": ["machine learning", "ml engineer", "mle", "ai engineer", "deep learning",
                  "data scientist", "data science", "data engineer", "applied scientist",
                  "research scientist", "nlp engineer", "computer vision"],
    },
    {
        "id": "data_analytics",
        "label": "Data & Analytics",
        "search_terms": ["data analyst", "business intelligence analyst", "analytics engineer"],
        "title": ["data analyst", "business intelligence", "bi analyst", "analytics engineer",
                  "reporting analyst", "business analyst"],
    },
    {
        "id": "product",
        "label": "Product",
        "search_terms": ["product manager", "technical product manager", "associate product manager"],
        "title": ["product manager", "product management", "technical product", "tpm",
                  "product owner", "program manager", "product analyst", "product lead"],
    },
    {
        "id": "design",
        "label": "Design",
        "search_terms": ["product designer", "ux designer"],
        "title": ["product designer", "ux designer", "ui designer", "ux researcher",
                  "user experience", "user research", "visual designer",
                  "interaction designer", "design engineer"],
    },
    {
        "id": "security",
        "label": "Security",
        "search_terms": ["security engineer", "application security engineer"],
        "title": ["security engineer", "security analyst", "appsec", "application security",
                  "infosec", "information security", "penetration tester", "red team",
                  "security researcher", "detection engineer"],
    },
    {
        "id": "devops_cloud",
        "label": "DevOps / Cloud",
        "search_terms": ["devops engineer", "site reliability engineer", "cloud engineer"],
        "title": ["devops", "site reliability", "sre", "cloud engineer", "cloud architect",
                  "platform reliability", "infrastructure engineer", "systems administrator",
                  "build engineer", "release engineer"],
    },
    {
        "id": "qa",
        "label": "QA / Test",
        "search_terms": ["qa engineer", "software test engineer"],
        "title": ["qa engineer", "quality assurance", "test engineer", "sdet",
                  "automation engineer", "test automation"],
    },
    {
        "id": "other",
        "label": "Other",
        "search_terms": [],
        "title": [],
    },
]

FUNCTION_IDS = [f["id"] for f in JOB_FUNCTIONS]
FUNCTION_LABEL = {f["id"]: f["label"] for f in JOB_FUNCTIONS}


def function_search_terms(function_ids) -> list[str]:
    """Every board search term the given functions expand to, deduplicated, order kept."""
    seen, out = set(), []
    for fid in function_ids or []:
        for term in next((f["search_terms"] for f in JOB_FUNCTIONS if f["id"] == fid), []):
            if term not in seen:
                seen.add(term)
                out.append(term)
    return out


def classify_function(title: str, description: str | None = None) -> str:
    """The one function id this posting belongs to; ``other`` when nothing matches.

    The title carries the decision — it is the only part of a posting that names
    the role. The description is a tie-breaker, not a vote of its own, because
    every backend JD mentions "data" somewhere.
    """
    t = " ".join(str(title or "").casefold().split())
    body = " ".join(str(description or "").casefold().split())[:4000]
    best, best_score = "other", 0
    for f in JOB_FUNCTIONS:
        if not f["title"]:
            continue
        # A longer matched term is a more specific claim: "data engineer" must beat
        # "engineer" inside another family's list.
        title_hits = [term for term in f["title"] if matches(term, t)]
        score = 3 * sum(len(term) for term in title_hits)
        if body:
            score += sum(1 for term in f["title"] if matches(term, body))
        if score > best_score:
            best, best_score = f["id"], score
    return best


# ── Employment levels ────────────────────────────────────────────────────────
# A posting may legitimately carry more than one ("New Grad / Entry Level").
LEVELS = [
    ("intern", "Internship"),
    ("new_grad", "New Grad"),
    ("entry", "Entry"),
    ("mid", "Mid"),
    ("senior", "Senior"),
    ("staff", "Staff"),
    ("lead", "Lead"),
    ("manager", "Manager"),
    ("director", "Director"),
]
LEVEL_IDS = [k for k, _ in LEVELS]
LEVEL_LABEL = dict(LEVELS)

# Ordered most specific first: "senior staff engineer" must resolve to staff+senior,
# and "intern" must never be read out of "internal tools engineer" (word boundary).
_LEVEL_TITLE_TERMS = {
    "intern": ["intern", "internship", "co-op", "coop", "summer analyst", "apprentice"],
    "new_grad": ["new grad", "new graduate", "recent graduate", "university graduate",
                 "campus hire", "graduate program", "grad program", "early career",
                 "entry level", "entry-level"],
    "entry": ["entry level", "entry-level", "junior", "jr", "associate", "i", "level 1", "l1"],
    "mid": ["mid level", "mid-level", "ii", "iii", "level 2", "level 3", "l2", "l3"],
    "senior": ["senior", "sr", "snr", "iv", "level 4", "l4"],
    "staff": ["staff", "member of technical staff", "mts", "l5", "l6"],
    "lead": ["lead", "tech lead", "technical lead", "principal", "architect"],
    "manager": ["manager", "head of", "supervisor"],
    "director": ["director", "vp", "vice president", "chief", "cto", "head of engineering"],
}

# The ONLY terms a job description may contribute. Everything else is title-only.
#
# Prose is a bad witness for seniority. "you will lead the team", "reporting to a
# manager", "our staff enjoy", "a leading AI company" — every one of those read
# as a senior posting, and a new-grad search then rejected the job for a word
# that was never its level. These phrases are different: they are multi-word,
# they are stated deliberately, and they can only ever ADD an early-career read,
# which is the safe direction for a gate that is meant to reject on evidence.
_DESCRIPTION_TERMS = {
    "internship", "co-op", "new grad", "new graduate", "recent graduate",
    "university graduate", "campus hire", "graduate program", "early career",
    "entry level", "entry-level",
}

# Seniority a "0-2 years" candidate cannot hold, however the posting words it.
SENIOR_LEVELS = {"senior", "staff", "lead", "manager", "director"}


def classify_levels(title: str, description: str | None = None) -> list[str]:
    """Every level this posting plausibly is, in taxonomy order; empty when unknown.

    Empty is a real answer and means UNKNOWN — the gate must not read it as
    "matches nothing".
    """
    t = " ".join(str(title or "").casefold().split())
    # Only the opening of a description: "you will report to a Director" at the
    # foot of a JD is not this posting's level.
    body = " ".join(str(description or "").casefold().split())[:600]
    found = []
    for level in LEVEL_IDS:
        for term in _LEVEL_TITLE_TERMS[level]:
            if matches(term, t) or (term in _DESCRIPTION_TERMS and body and matches(term, body)):
                found.append(level)
                break
    # "Senior Manager" is both; "Manager, Engineering" is only manager. But a
    # title naming a senior rank was never entry work, so drop the junior reads.
    if any(lv in SENIOR_LEVELS for lv in found):
        found = [lv for lv in found if lv not in ("intern", "new_grad", "entry")]
    return found


def pack_levels(levels) -> str | None:
    """["new_grad","entry"] -> ",new_grad,entry," — the storage form (see Job.experience_levels).

    The sentinel commas are what make a substring match exact: without them
    `LIKE '%entry%'` would also answer for a level named `entry_manager`.
    """
    clean = [lv for lv in (levels or []) if lv in LEVEL_IDS]
    return "," + ",".join(clean) + "," if clean else None


def unpack_levels(packed) -> list[str]:
    """The storage form back to a list; tolerant of an unwrapped legacy value."""
    if isinstance(packed, (list, tuple)):
        return [lv for lv in packed if lv in LEVEL_IDS]
    return [lv for lv in str(packed or "").split(",") if lv in LEVEL_IDS]


def level_like(level: str) -> str:
    """The SQL LIKE pattern that matches one packed level exactly."""
    return f"%,{level},%"


# ── Years of experience ──────────────────────────────────────────────────────
# Only counted inside a sentence that is actually talking about experience —
# "founded in 5 years ago" and "3 years of runway" are not requirements.
_YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:[-–—]|\bto\b)?\s*(\d{1,2})?\s*\+?\s*(?:\+\s*)?(?:years?|yrs?)\b",
    re.I)
_EXPERIENCE_RE = re.compile(r"experien|background|track record", re.I)


def parse_years(text: str | None) -> tuple[int | None, int | None]:
    """(minimum, maximum) years of experience the posting states, or (None, None).

    The minimum returned is the SMALLEST floor any requirement states. A posting
    asking "2+ years backend, 5+ years distributed systems preferred" has an
    entry bar of 2, and rejecting it for the 5 would throw away a job the
    candidate can hold. Conservative by design — the gate rejects only on
    evidence, never on a guess.
    """
    body = str(text or "")
    if not body:
        return (None, None)
    floors, ceilings = [], []
    # Sentence-ish chunks, so "experience" has to be near the number.
    for chunk in re.split(r"[.\n;•·|]", body[:20000]):
        if not _EXPERIENCE_RE.search(chunk):
            continue
        for m in _YEARS_RE.finditer(chunk):
            low = int(m.group(1))
            high = int(m.group(2)) if m.group(2) else None
            if low > 40 or (high is not None and (high > 40 or high < low)):
                continue        # "401k years" and other nonsense
            floors.append(low)
            if high is not None:
                ceilings.append(high)
    if not floors:
        return (None, None)
    return (min(floors), max(ceilings) if ceilings else None)


# ── Employment type ──────────────────────────────────────────────────────────
JOB_TYPES = [
    ("fulltime", "Full-time"),
    ("internship", "Internship"),
    ("contract", "Contract"),
    ("parttime", "Part-time"),
]
JOB_TYPE_IDS = [k for k, _ in JOB_TYPES]
JOB_TYPE_LABEL = dict(JOB_TYPES)

_TYPE_TERMS = {
    "internship": ["intern", "internship", "co-op", "coop"],
    "contract": ["contract", "contractor", "c2c", "freelance", "temporary", "temp",
                 "consultant", "1099"],
    "parttime": ["part time", "part-time", "parttime"],
    "fulltime": ["full time", "full-time", "fulltime", "permanent", "regular"],
}


def normalize_job_type(raw: str | None, title: str | None = None) -> str | None:
    """A board's employment_type string folded onto our four ids; None = unknown."""
    text = " ".join(str(raw or "").casefold().replace("_", " ").split())
    for key in ("internship", "contract", "parttime", "fulltime"):
        if text and any(matches(term, text) for term in _TYPE_TERMS[key]):
            return key
    # The title still settles the one case boards routinely leave blank.
    t = " ".join(str(title or "").casefold().split())
    if t and any(matches(term, t) for term in _TYPE_TERMS["internship"]):
        return "internship"
    return None


# ── Work models ──────────────────────────────────────────────────────────────
# These are the three `Job.arr_*` booleans, named for the user.
WORK_MODELS = [("onsite", "On-site"), ("hybrid", "Hybrid"), ("remote", "Remote")]
WORK_MODEL_IDS = [k for k, _ in WORK_MODELS]
WORK_MODEL_LABEL = dict(WORK_MODELS)

# ── Date-posted presets ──────────────────────────────────────────────────────
DATE_POSTED_DAYS = [1, 3, 7, 30]

# ── Years-of-experience presets (max_years_experience) ───────────────────────
YEARS_PRESETS = [0, 1, 2, 3, 5, None]
