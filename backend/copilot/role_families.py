"""Role families: which slice of the verified Career Evidence a résumé starts from.

One universal résumé is the wrong starting point for every application. A role
family is a *view* of the same verified facts — a selection and an ordering, never
a second copy of the truth and never new wording. Everything a family résumé says
still comes from `CandidateFact` rows and is still cited by provenance id.

Three families ship (`software_engineering`, `product`, `data_ml`); anything the
user adds to the `role_families` setting joins them, so the pipeline is not built
around exactly three ids. Classification of a job into a family is deterministic
keyword scoring over the structured job analysis — it produces a label and a
reason for the user to override, and it is deliberately **not** an input to
Candidate Fit.
"""
import functools
import json
import logging
import re

logger = logging.getLogger("jobnavigator.copilot.role_families")

# `title` terms classify a posting; `evidence` terms rank the candidate's own
# facts when selecting what a family résumé leads with. Both are plain vocabulary:
# a term that appears in neither the posting nor the facts changes nothing, and a
# term matching a fact never licenses writing that term into a bullet.
BUILTIN = [
    {
        "id": "software_engineering",
        "label": "Software Engineering",
        "title": ["software engineer", "software engineering", "swe", "backend", "back end", "frontend",
                  "front end", "full stack", "fullstack", "developer", "programmer", "platform engineer",
                  "infrastructure engineer", "systems engineer", "devops", "site reliability", "sre",
                  "mobile engineer", "ios", "android", "embedded"],
        "evidence": ["software", "engineer", "backend", "frontend", "full stack", "api", "rest", "service",
                     "microservice", "database", "sql", "postgres", "cloud", "aws", "gcp", "azure", "docker",
                     "kubernetes", "ci", "cd", "pipeline", "python", "java", "javascript", "typescript", "c++",
                     "go", "rust", "react", "node", "django", "flask", "fastapi", "spring", "git", "test",
                     "latency", "throughput", "refactor", "deploy", "system", "architecture"],
    },
    {
        "id": "product",
        "label": "Product / Technical Product Management",
        "title": ["product manager", "product management", "technical product", "tpm", "product owner",
                  "program manager", "associate product", "product analyst", "product lead"],
        "evidence": ["product", "roadmap", "requirement", "stakeholder", "cross functional", "cross-functional",
                     "prioriti", "backlog", "user research", "customer", "launch", "go to market", "spec",
                     "specification", "coordinat", "facilitat", "presented", "communicat", "documentation",
                     "process", "workflow", "metric", "adoption", "retention", "kpi", "a/b", "scrum", "agile",
                     "jira", "owned", "ownership", "led"],
    },
    {
        "id": "data_ml",
        "label": "Data / Machine Learning",
        "title": ["data scientist", "data science", "machine learning", "ml engineer", "mle", "ai engineer",
                  "deep learning", "research scientist", "data engineer", "data analyst", "analytics engineer",
                  "applied scientist", "nlp", "computer vision"],
        "evidence": ["machine learning", "ml", "model", "modeling", "modelling", "training", "inference",
                     "neural", "deep learning", "nlp", "computer vision", "regression", "classification",
                     "clustering", "experiment", "a/b", "statistic", "hypothesis", "dataset", "data pipeline",
                     "etl", "feature", "pandas", "numpy", "scikit", "pytorch", "tensorflow", "sql", "python",
                     "r", "spark", "airflow", "accuracy", "precision", "recall", "f1", "auc", "research",
                     "analysis", "visualization", "notebook"],
    },
]

SETTING_KEY = "role_families"


def _valid(entry) -> bool:
    return (isinstance(entry, dict) and isinstance(entry.get("id"), str) and re.fullmatch(r"[a-z0-9_]{2,40}", entry["id"])
            and isinstance(entry.get("label"), str) and entry["label"].strip())


def _normalized(entry: dict) -> dict:
    return {"id": entry["id"], "label": entry["label"].strip(),
            "title": [str(t).casefold() for t in entry.get("title") or [] if str(t).strip()],
            "evidence": [str(t).casefold() for t in entry.get("evidence") or [] if str(t).strip()],
            "builtin": bool(entry.get("builtin"))}


def families(db=None) -> list[dict]:
    """The built-in families plus any the user defined; a custom entry may also override a built-in id."""
    out = {f["id"]: _normalized({**f, "builtin": True}) for f in BUILTIN}
    if db is None:
        return list(out.values())
    from backend.models.db import Setting
    row = db.query(Setting).filter(Setting.key == SETTING_KEY).first()
    try:
        extra = json.loads(row.value) if row and row.value else []
    except ValueError:
        logger.warning(f"setting {SETTING_KEY} is not valid JSON; ignoring it")
        extra = []
    for entry in extra if isinstance(extra, list) else []:
        if _valid(entry):
            out[entry["id"]] = _normalized(entry)
    return list(out.values())


def get(family_id: str, db=None) -> dict | None:
    return next((f for f in families(db) if f["id"] == family_id), None)


def default_id(db=None) -> str:
    return (families(db) or [{"id": "software_engineering"}])[0]["id"]


# ── classification ───────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=1024)
def _matcher(term: str):
    """A term matches at a word start; one- and two-letter terms must be the whole word.

    Plain substring matching made "r" (the language) match "Answered" and every
    other word with an r in it, which scored unrelated evidence into Data/ML.
    Longer entries stay prefixes on purpose — "prioriti" is meant to catch
    "prioritised" and "prioritization".
    """
    tail = r"(?![a-z0-9+#])" if len(term) <= 2 else ""
    return re.compile(r"(?<![a-z0-9+#])" + re.escape(term) + tail)


def matches(term: str, text: str) -> bool:
    return bool(term) and _matcher(term).search(text) is not None


def _hits(terms, text: str) -> list[str]:
    return sorted({t for t in terms if matches(t, text)})


def classify(analysis: dict, db=None) -> dict:
    """{"family", "label", "confidence", "reason", "scores"} for one structured job analysis.

    Deterministic, and separate from scoring: which family a posting belongs to
    says nothing about how well the candidate fits it, so this never reaches
    Candidate Fit. The title carries most of the weight because it is the one part
    of a posting that names the role; technologies and keywords break ties.
    """
    a = analysis or {}
    title = " ".join(str(a.get("title") or "").casefold().split())
    body = " ".join(str(x).casefold() for x in
                    (a.get("technologies") or []) + (a.get("keywords") or []) + (a.get("domain_terms") or [])
                    + [r.get("text", "") for r in a.get("requirements") or []])

    scores, reasons = {}, {}
    for f in families(db):
        title_hits = _hits(f["title"], title)
        body_hits = _hits(f["evidence"], body)
        scores[f["id"]] = 3 * len(title_hits) + len(body_hits)
        reasons[f["id"]] = (title_hits, body_hits)

    if not scores:
        return {"family": "", "label": "", "confidence": 0.0, "reason": "no role families configured", "scores": {}}

    best = max(scores, key=lambda k: (scores[k], k))
    total = sum(scores.values())
    top = scores[best]
    runner_up = max([v for k, v in scores.items() if k != best], default=0)
    confidence = 0.0 if not top else round(min(1.0, (top - runner_up) / top if total else 0) * 0.6
                                           + min(1.0, top / 12) * 0.4, 2)
    title_hits, body_hits = reasons[best]
    if title_hits:
        reason = f"the title matches {', '.join(title_hits[:3])}"
    elif body_hits:
        reason = f"the posting emphasises {', '.join(body_hits[:4])}"
    else:
        reason = "nothing in the posting matched a role family; this is the default"
    return {"family": best, "label": get(best, db)["label"], "confidence": confidence,
            "reason": reason, "scores": scores}


# ── selection ────────────────────────────────────────────────────────────────

def relevance(family: dict, text: str) -> int:
    """How many of a family's evidence terms this text actually contains. Selection only: it never adds a word."""
    lowered = " ".join(str(text or "").casefold().split())
    return sum(1 for t in family.get("evidence") or [] if matches(t, lowered))
