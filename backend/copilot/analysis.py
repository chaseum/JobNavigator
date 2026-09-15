"""Job analysis -> evidence mapping -> Role Match, persisted on JobAnalysisRecord.

Two LLM steps (temperature 0, schema-constrained), then deterministic code:
extraction of requirements from the posting, and a proposal of which verified
facts support each. matching.py checks every citation and computes the score.
"""
import hashlib
import json
import logging
from types import SimpleNamespace

from backend.analyzer.llm_client import call_structured
from backend.copilot import facts as F
from backend.copilot import matching as M
from backend.copilot.schemas import EvidenceMapping, JobAnalysis
from backend.models.db import Job, JobAnalysisRecord, Persona, SessionLocal, Setting, utcnow

logger = logging.getLogger("jobnavigator.copilot.analysis")

EXTRACT_SYSTEM = ("You extract hiring requirements from job postings into a fixed structure. "
                  "Copy what the posting says. Never add requirements it does not state.")
EXTRACT_PROMPT = """Analyze this job posting. Fill every field from the posting text only; leave a field empty when the posting says nothing.

For `requirements`, list each distinct requirement once:
- text: the requirement as written, lightly normalized ("3+ years of Python")
- source_quote: REQUIRED. Copy an exact 12–500 character quote from the posting that supports this requirement; never paraphrase it. Omit the requirement if no exact supporting quote exists.
- category: qualification | technology | domain | responsibility | education | experience | work_authorization | clearance | soft_skill
- required: true for required / must / minimum; false for preferred / nice to have / bonus
- importance: 3 when repeated or emphasized, 2 normally, 1 for a passing mention
List the main responsibilities too, with category "responsibility".

JOB POSTING:
<<JD>>"""

MATCH_SYSTEM = ("You check job requirements against a candidate's verified facts. Each fact starts with its id in square "
                "brackets; the brackets are only delimiters. Never assume experience the facts do not state.")
MATCH_PROMPT = """For every requirement below, return one match.

status:
- MATCHED: a fact directly demonstrates it. Cite that fact.
- PARTIAL: related or weaker evidence. Cite it and say what is missing.
- MISSING: nothing in the facts supports it. Cite nothing.
- UNKNOWN: the facts are too thin to decide. Cite nothing.
A skill that is only listed is weaker than a role, project or research entry where it was used.
Do not treat similar-sounding tools as the same tool.

Citing facts:
- source_fact_ids lists the ids of the facts that support your status.
- Write each id WITHOUT brackets. Correct: "education_10". Incorrect: "[education_10]".
- MATCHED and PARTIAL require at least one id. MISSING and UNKNOWN cite nothing.

CANDIDATE FACTS:
<<FACTS>>

REQUIREMENTS:
<<REQS>>"""

_MATCH_CHUNK = 15


def _setting(db, key, default=""):
    row = db.query(Setting).filter(Setting.key == key).first()
    return row.value if row and row.value not in (None, "") else default


def weights(db) -> dict:
    try:
        w = json.loads(_setting(db, "role_match_weights", "{}"))
        return {k: float(v) for k, v in w.items() if k in M.DEFAULT_WEIGHTS} if isinstance(w, dict) else {}
    except (ValueError, TypeError):
        return {}


_QUOTE_STOPWORDS = {"a", "an", "and", "as", "at", "be", "by", "for", "from", "in", "into", "of", "on", "or", "the", "to", "using", "with"}


def _quote_normalize(value: str) -> str:
    import re
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").casefold()))


def _grounded_requirement(requirement: dict, jd: str) -> bool:
    quote = str(requirement.get("source_quote") or "").strip()
    quote_text = _quote_normalize(quote)
    jd_text = _quote_normalize(jd)
    if len(quote) < 12 or len(quote) > 500 or not quote_text or quote_text not in jd_text:
        return False
    required_tokens = {t for t in _quote_normalize(str(requirement.get("text") or "")).split()
                       if t not in _QUOTE_STOPWORDS and len(t) > 1}
    quote_tokens = set(quote_text.split())
    return bool(required_tokens and len(required_tokens & quote_tokens) / len(required_tokens) >= 0.5)


def normalize_requirements(analysis: dict, jd: str | None = None) -> dict:
    """Assign stable ids, drop duplicates, and optionally reject unsupported claims."""
    seen, reqs = set(), []
    for r in analysis.get("requirements") or []:
        if jd is not None and not _grounded_requirement(r, jd):
            continue
        key = " ".join(str(r.get("text") or "").lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        reqs.append({**r, "id": f"r{len(reqs) + 1}"})
    return {**analysis, "requirements": reqs}


def latest_record(db, job_id):
    return (db.query(JobAnalysisRecord).filter(JobAnalysisRecord.job_id == job_id)
            .order_by(JobAnalysisRecord.id.desc()).first())


def evidence_context(db) -> tuple[list, dict, dict]:
    """(verified facts, {ref: {kind, data, parent ref}}, identity answers)."""
    facts = F.load_verified(db)
    refs = {f.id: F.fact_ref(f) for f in facts}
    persona = db.query(Persona).filter(Persona.id == 1).first()
    return (facts, {F.fact_ref(f): {"kind": f.kind, "data": f.data or {}, "parent": refs.get(f.parent_id)} for f in facts},
            F.identity_facts(persona))


async def run_analysis(job_id: str) -> str:
    from backend.api.routes_resumes import _resolve_tailoring_jd
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            raise RuntimeError("job not found")
        ref = SimpleNamespace(
            id=job.id, description=job.description, description_source=job.description_source,
            description_quality=job.description_quality, description_fetched_at=job.description_fetched_at,
            source=job.source, source_url=job.source_url, canonical_url=job.canonical_url,
            apply_url=job.apply_url, company=job.company, title=job.title, url=job.url,
            location=job.location, employment_type=job.employment_type,
            salary_min=job.salary_min, salary_max=job.salary_max, salary_source=job.salary_source,
            salary_currency=job.salary_currency, salary_period=job.salary_period,
            cached_page_text=job.cached_page_text,
        )
        label = f"{job.company or '?'} — {job.title or '?'}"
    finally:
        db.close()

    jd = await _resolve_tailoring_jd(ref)
    if not (jd or "").strip():
        raise RuntimeError("Could not retrieve a complete job description")
    result, provider, model = await call_structured(
        JobAnalysis, EXTRACT_PROMPT.replace("<<JD>>", jd[:15000]), EXTRACT_SYSTEM,
        feature="copilot", max_tokens=6000, job_id=job_id)
    data = normalize_requirements(result.model_dump(), jd=jd)
    if not data["requirements"]:
        raise RuntimeError("Could not extract grounded job requirements from the posting")

    db = SessionLocal()
    try:
        db.add(JobAnalysisRecord(job_id=job_id, jd_hash=hashlib.sha256(jd.encode()).hexdigest(),
                                 provider=provider, model=model, analysis=data))
        db.commit()
    finally:
        db.close()
    summary = await run_match(job_id)
    return f"Analyzed {label}: {len(data['requirements'])} requirements, {summary}"


async def run_match(job_id: str) -> str:
    db = SessionLocal()
    try:
        rec = latest_record(db, job_id)
        if rec is None:
            raise RuntimeError("analyze the job first")
        rec_id, analysis = rec.id, rec.analysis or {}
        requirements = list(analysis.get("requirements") or [])
        facts, facts_by_ref, identity = evidence_context(db)
        facts_text = F.facts_prompt(facts)
        w = weights(db)
        version = F.profile_version(db)
    finally:
        db.close()

    matches, llm_error = [], None
    askable = [r for r in requirements if r.get("category") != "work_authorization"]
    if facts and askable:
        try:
            for i in range(0, len(askable), _MATCH_CHUNK):
                chunk = askable[i:i + _MATCH_CHUNK]
                reqs_text = "\n".join(f"{r['id']}: [{r['category']}{', required' if r.get('required') else ', preferred'}] {r['text']}" for r in chunk)
                out, _, _ = await call_structured(
                    EvidenceMapping, MATCH_PROMPT.replace("<<FACTS>>", facts_text).replace("<<REQS>>", reqs_text),
                    MATCH_SYSTEM, feature="copilot", max_tokens=4000, job_id=job_id)
                matches.extend(m.model_dump() for m in out.matches)
        except Exception as e:   # deterministic evidence still stands; the number does not
            logger.warning(f"evidence matching failed for {job_id}: {e}")
            llm_error = str(e)[:300]

    evidence = M.sanitize_evidence(requirements, matches, facts_by_ref, identity, analysis.get("technologies") or [])
    result = M.score(requirements, evidence, w)
    if not facts:
        result["unavailable"] = "your Profile has no verified facts"
    elif llm_error:
        result["unavailable"] = f"evidence matching failed: {llm_error}"
    if result.get("unavailable"):
        # a score computed without the evidence step would read as a real low fit
        result["partial_score"], result["score"] = result["score"], None

    db = SessionLocal()
    try:
        rec = db.get(JobAnalysisRecord, rec_id)
        rec.evidence, rec.match, rec.profile_version, rec.matched_at = evidence, result, version, utcnow()
        db.commit()
    finally:
        db.close()
    from backend.copilot.resume_audits import audit_job
    audit_job(job_id)
    return f"Candidate Fit {result['score'] if result['score'] is not None else 'unavailable'}"
