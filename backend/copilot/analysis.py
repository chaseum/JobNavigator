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
- category: qualification | technology | domain | responsibility | education | experience | work_authorization | clearance | soft_skill
- required: true for required / must / minimum; false for preferred / nice to have / bonus
- importance: 3 when repeated or emphasized, 2 normally, 1 for a passing mention
List the main responsibilities too, with category "responsibility".

JOB POSTING:
<<JD>>"""

MATCH_SYSTEM = ("You check job requirements against a candidate's verified facts. Cite fact ids exactly as written "
                "in square brackets. Never assume experience the facts do not state.")
MATCH_PROMPT = """For every requirement below, return one match.

status:
- MATCHED: a fact directly demonstrates it. Cite that fact.
- PARTIAL: related or weaker evidence. Cite it and say what is missing.
- MISSING: nothing in the facts supports it. Cite nothing.
- UNKNOWN: the facts are too thin to decide. Cite nothing.
A skill that is only listed is weaker than a role, project or research entry where it was used.
Do not treat similar-sounding tools as the same tool.

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


def normalize_requirements(analysis: dict) -> dict:
    """Stable ids r1..rn in posting order, duplicates (same text, case-insensitive) dropped."""
    seen, reqs = set(), []
    for r in analysis.get("requirements") or []:
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
    """(verified facts, {ref: {kind, data}}, identity answers)."""
    facts = F.load_verified(db)
    persona = db.query(Persona).filter(Persona.id == 1).first()
    return facts, {F.fact_ref(f): {"kind": f.kind, "data": f.data or {}} for f in facts}, F.identity_facts(persona)


async def run_analysis(job_id: str) -> str:
    from backend.api.routes_resumes import _resolve_tailoring_jd
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            raise RuntimeError("job not found")
        ref = SimpleNamespace(id=job.id, description=job.description, url=job.url, cached_page_text=job.cached_page_text)
        label = f"{job.company or '?'} — {job.title or '?'}"
    finally:
        db.close()

    jd = await _resolve_tailoring_jd(ref)
    if not (jd or "").strip():
        raise RuntimeError("this job has no description to analyze")
    result, provider, model = await call_structured(
        JobAnalysis, EXTRACT_PROMPT.replace("<<JD>>", jd[:15000]), EXTRACT_SYSTEM,
        feature="copilot", max_tokens=6000, job_id=job_id)
    data = normalize_requirements(result.model_dump())

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
        rec_id, requirements = rec.id, list((rec.analysis or {}).get("requirements") or [])
        facts, facts_by_ref, identity = evidence_context(db)
        facts_text = F.facts_prompt(facts)
        w = weights(db)
        version = F.profile_version(db)
        from backend.copilot.versions import latest_parser_health
        health = latest_parser_health(db, job_id)
    finally:
        db.close()

    matches = []
    askable = [r for r in requirements if r.get("category") != "work_authorization"]
    if facts and askable:
        for i in range(0, len(askable), _MATCH_CHUNK):
            chunk = askable[i:i + _MATCH_CHUNK]
            reqs_text = "\n".join(f"{r['id']}: [{r['category']}{', required' if r.get('required') else ', preferred'}] {r['text']}" for r in chunk)
            out, _, _ = await call_structured(
                EvidenceMapping, MATCH_PROMPT.replace("<<FACTS>>", facts_text).replace("<<REQS>>", reqs_text),
                MATCH_SYSTEM, feature="copilot", max_tokens=4000, job_id=job_id)
            matches.extend(m.model_dump() for m in out.matches)

    evidence = M.sanitize_evidence(requirements, matches, facts_by_ref, identity)
    result = M.score(requirements, evidence, w, parser_health=health)

    db = SessionLocal()
    try:
        rec = db.get(JobAnalysisRecord, rec_id)
        rec.evidence, rec.match, rec.profile_version, rec.matched_at = evidence, result, version, utcnow()
        job = db.query(Job).filter(Job.id == job_id).first()
        if job is not None:
            # surfaces in the feed's score chip and sort, next to any legacy score
            scores = {**(job.cv_scores or {}), "Role Match": result["score"]}
            scores.pop("_skipped", None)
            job.cv_scores = scores
            numeric = {k: v for k, v in scores.items() if isinstance(v, (int, float))}
            job.best_cv_score = max(numeric.values()) if numeric else None
            job.best_cv = max(numeric, key=numeric.get) if numeric else None
        db.commit()
    finally:
        db.close()
    return f"Role Match {result['score']}"
