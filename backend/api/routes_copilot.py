"""Copilot workflow per job: analysis, Candidate Fit, Resume Applicability audits, adding context, privacy."""
import logging
import hashlib
import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from backend.copilot import facts as F
from backend.copilot.analysis import latest_record, run_analysis, run_match
from backend.job_monitor import JobAlreadyRunningError, is_running, launch_background
from backend.models.db import Application, CandidateFact, Job, get_db

logger = logging.getLogger("jobnavigator.copilot")
router = APIRouter(prefix="/copilot", tags=["copilot"])


def _job_or_404(db, job_id: str) -> Job:
    try:
        _uuid.UUID(str(job_id))
    except ValueError:
        raise HTTPException(404, "job not found")
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(404, "job not found")
    return job


def _launch(kind: str, func, job_id: str):
    # call only from an `async def` route: launch_background schedules on the running loop
    try:
        run_id = launch_background(kind, func, trigger="manual", scope_key=str(job_id),
                                   target_job_id=_uuid.UUID(str(job_id)), func_kwargs={"job_id": str(job_id)})
        return {"run_id": run_id, "status": "running"}
    except JobAlreadyRunningError:
        return JSONResponse(status_code=409, content={"detail": "already running for this job"})


@router.post("/jobs/{job_id}/analyze", status_code=202)
async def analyze_job(job_id: str, db: Session = Depends(get_db)):
    job = _job_or_404(db, job_id)
    if not ((job.description or "").strip() or (job.canonical_url or "").strip()
            or (job.url or "").strip() or (job.source_url or "").strip()
            or (job.cached_page_text or "").strip()):
        raise HTTPException(400, "job has no description or URL to analyze")
    return _launch("copilot_analyze", run_analysis, job_id)


@router.post("/jobs/{job_id}/match", status_code=202)
async def rematch_job(job_id: str, db: Session = Depends(get_db)):
    _job_or_404(db, job_id)
    if latest_record(db, job_id) is None:
        raise HTTPException(400, "analyze the job first")
    return _launch("copilot_match", run_match, job_id)


def match_is_outdated(match) -> bool:
    """A match computed by the old Role Match scorer (component weights, parser health mixed in); it must be re-run."""
    return bool(match) and "formula" not in match


@router.post("/jobs/{job_id}/resume-audit")
def audit_resumes(job_id: str, body: dict | None = None, db: Session = Depends(get_db)):
    """Run and store the Resume Applicability audit for the base and tailored résumés (or `version_ids`)."""
    from backend.copilot.resume_audits import audit_job
    _job_or_404(db, job_id)
    rec = latest_record(db, job_id)
    if rec is None or rec.evidence is None:
        raise HTTPException(400, "analyze and match the job first")
    ids = [str(x) for x in (body or {}).get("version_ids") or []]
    for x in ids:
        try:
            _uuid.UUID(x)
        except ValueError:
            raise HTTPException(404, "résumé version not found")
    out = audit_job(job_id, ids or None)
    return {vid: {"score": r["score"], "maximum": r["maximum"], "kind": r["kind"]} for vid, r in out.items()}


def _role_family_block(db, rec) -> dict:
    """Which role family this posting is, why, and the options to override it.

    Classification decides which base résumé a tailored draft derives from. It is
    deliberately no part of Candidate Fit: what kind of role a posting is says
    nothing about how well the candidate's evidence supports it.
    """
    from backend.copilot import role_families as RF
    verdict = None
    if not rec.role_family:
        verdict = RF.classify(rec.analysis or {}, db)
    family_id = rec.role_family or (verdict or {}).get("family") or ""
    family = RF.get(family_id, db)
    return {
        "id": family_id, "label": family["label"] if family else "",
        "source": rec.role_family_source or ("auto" if verdict else None),
        "reason": rec.role_family_reason or (f"{verdict['reason']} (confidence {verdict['confidence']})" if verdict else ""),
        "saved": bool(rec.role_family),
        "options": [{"id": f["id"], "label": f["label"]} for f in RF.families(db)],
    }


@router.put("/jobs/{job_id}/role-family")
def set_role_family(job_id: str, body: dict, db: Session = Depends(get_db)):
    """Override the classifier for an ambiguous posting. Affects which base the résumé is tailored from, nothing else."""
    from backend.copilot import role_families as RF
    _job_or_404(db, job_id)
    rec = latest_record(db, job_id)
    if rec is None:
        raise HTTPException(400, "analyze the job first")
    family_id = str(body.get("role_family") or "")
    if RF.get(family_id, db) is None:
        raise HTTPException(400, f"unknown role family {family_id!r}")
    rec.role_family, rec.role_family_source = family_id, "user"
    rec.role_family_reason = "you chose this role family"
    db.commit()
    return _role_family_block(db, rec)


@router.get("/jobs/{job_id}")
def job_workspace(job_id: str, base_version_id: str | None = None, db: Session = Depends(get_db)):
    job = _job_or_404(db, job_id)
    rec = latest_record(db, job_id)
    from backend.scraper.enrichment import validate_job_description
    jd_quality = validate_job_description(job, job.description, job.description_source)
    needs_details = not jd_quality.valid
    headlines = {F.fact_ref(f): F.fact_headline(f.kind, f.data or {}) for f in db.query(CandidateFact).all()}
    from backend.models.db import Persona
    persona = db.query(Persona).filter(Persona.id == 1).first()
    headlines.update({ref: F.identity_headline(ref, v) for ref, v in F.identity_facts(persona).items()})
    app = db.query(Application).filter(Application.job_id == job.id).order_by(Application.applied_at.desc()).first()
    out = {
        "job": {"id": str(job.id), "short_id": job.short_id, "company": job.company, "title": job.title,
                "url": job.apply_url or job.canonical_url or job.url, "canonical_url": job.canonical_url,
                "apply_url": job.apply_url, "source_url": job.source_url, "description": job.description,
                "description_source": job.description_source, "description_quality": job.description_quality,
                "needs_job_details": needs_details, "location": job.location, "status": job.status,
                "has_description": not needs_details},
        "analysis": None, "requirements": [], "match": None, "resume_audit": None, "headlines": headlines,
        "stale": False, "outdated": False,
        "running": [k for k in ("copilot_analyze", "copilot_match") if is_running(k, str(job_id))],
        "application": {"id": str(app.id), "status": app.status} if app else None,
    }
    if rec is None:
        out["needs_job_details"] = needs_details
        return out
    current_hash = hashlib.sha256((job.description or "").encode()).hexdigest()
    jd_stale = rec.jd_hash != current_hash
    if needs_details:
        out["needs_job_details"] = True
        out["stale"] = True
        out["jd_stale"] = jd_stale
        return out
    reqs = (rec.analysis or {}).get("requirements") or []
    ev = {e["requirement_id"]: e for e in (rec.evidence or [])}
    out["analysis"] = {**(rec.analysis or {}), "id": rec.id, "provider": rec.provider, "model": rec.model,
                       "created_at": rec.created_at.isoformat() if rec.created_at else None}
    def row(r):
        e = ev.get(r["id"], {})
        return {**r, "status": e.get("status", "UNKNOWN"), "explanation": e.get("explanation", ""),
                "rules": e.get("rules", []), "llm_status": e.get("llm_status"), "llm_explanation": e.get("llm_explanation", ""),
                "original_source_fact_ids": e.get("original_source_fact_ids", []),
                "dropped_source_fact_ids": e.get("dropped_source_fact_ids", e.get("dropped_ids", [])),
                "evidence": [{"ref": s, "headline": headlines.get(s) or s} for s in e.get("source_fact_ids", [])]}
    out["requirements"] = [row(r) for r in reqs]
    out["match"] = rec.match
    out["role_family"] = _role_family_block(db, rec)
    out["jd_stale"] = jd_stale
    out["outdated"] = match_is_outdated(rec.match)
    out["stale"] = rec.evidence is None or rec.profile_version != F.profile_version(db) or jd_stale or out["outdated"]
    out["needs_job_details"] = False
    if base_version_id:
        try:
            _uuid.UUID(base_version_id)
        except ValueError:
            raise HTTPException(404, "résumé version not found")
    if rec.evidence is not None and not out["outdated"]:
        from backend.copilot.resume_audits import workspace
        out["resume_audit"] = workspace(db, job.id, rec, base_version_id)
    return out


@router.post("/jobs/{job_id}/context", status_code=201)
async def add_context(job_id: str, body: dict, db: Session = Depends(get_db)):
    """New context from the gap audit. It is used only once the user confirms it is true (`confirmed: true`):
    then it is a verified fact, the profile version changes and the match and audits rerun. Unconfirmed, it waits
    unverified in the Profile and nothing cites it. The parent is whatever the user picked; none is inferred."""
    from backend.api.routes_profile import _check_evidence, _check_parent, _fact_dict, _validated
    _job_or_404(db, job_id)
    kind = str(body.get("kind") or "")
    data = _validated(kind, body.get("data"))
    _check_evidence(db, data)
    confirmed = body.get("confirmed") is True
    row = CandidateFact(kind=kind, parent_id=_check_parent(db, kind, body.get("parent_id")), data=data,
                        verified=confirmed, source="gap")
    db.add(row)
    db.commit()
    db.refresh(row)
    rerun = _launch("copilot_match", run_match, job_id) if confirmed and latest_record(db, job_id) else None
    return {"fact": _fact_dict(row), "rematch": rerun if isinstance(rerun, dict) else None}


def _overleaf_settings(db):
    from backend.models.db import Setting
    rows = {r.key: r.value for r in db.query(Setting).filter(Setting.key.in_(["overleaf_mode", "overleaf_git_remote"])).all()}
    return (rows.get("overleaf_mode") or "disabled"), (rows.get("overleaf_git_remote") or "").strip()


@router.get("/overleaf")
def overleaf_status(db: Session = Depends(get_db)):
    from backend.copilot import overleaf
    mode, remote = _overleaf_settings(db)
    try:
        return overleaf.status(mode, remote)
    except overleaf.OverleafError as e:
        return {"mode": mode, "remote_configured": bool(remote), "error": str(e)}


@router.post("/overleaf/pull")
def overleaf_pull(db: Session = Depends(get_db)):
    from backend.copilot import overleaf
    mode, remote = _overleaf_settings(db)
    if mode != "git" or not remote:
        raise HTTPException(400, "set Overleaf to Git mode and configure the remote first")
    try:
        return overleaf.pull(remote)
    except overleaf.OverleafError as e:
        raise HTTPException(502, f"Overleaf pull failed: {e}")


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    """Counts and next actions across the workflow."""
    from sqlalchemy import func
    from backend.analyzer.llm_client import REMOTE_PROVIDERS, resolve_llm_config
    from backend.models.db import JobAnalysisRecord, ResumeVersion
    version = F.profile_version(db)
    latest_ids = [r[0] for r in db.query(func.max(JobAnalysisRecord.id)).group_by(JobAnalysisRecord.job_id).all()]
    analyses = db.query(JobAnalysisRecord).filter(JobAnalysisRecord.id.in_(latest_ids)).all() if latest_ids else []
    jobs = {j.id: j for j in db.query(Job).filter(Job.id.in_([a.job_id for a in analyses])).all()} if analyses else {}
    versions = db.query(ResumeVersion).filter(ResumeVersion.status != "rejected").all()
    has_version = {v.job_id for v in versions if v.job_id}
    apps = db.query(Application).all()

    def job_row(job, **extra):
        return {"job_id": str(job.id), "title": job.title, "company": job.company, **extra}

    scored = sorted((a for a in analyses if a.match and a.match.get("score") is not None and a.job_id in jobs),
                    key=lambda a: a.match["score"], reverse=True)
    drafts = sorted((v for v in versions if v.status == "draft" and v.kind == "tailored" and v.job_id), key=lambda v: v.created_at, reverse=True)
    draft_jobs = {j.id: j for j in db.query(Job).filter(Job.id.in_([v.job_id for v in drafts])).all()} if drafts else {}
    app_jobs = {j.id: j for j in db.query(Job).filter(Job.id.in_([a.job_id for a in apps if a.status == "ready_to_apply"])).all()} if apps else {}
    facts = db.query(CandidateFact.verified, func.count()).group_by(CandidateFact.verified).all()
    cfg = resolve_llm_config("copilot", db=db)
    return {
        "counts": {
            "saved_jobs": db.query(Job).filter((Job.saved == True) | (Job.status == "saved")).count(),  # noqa: E712
            "analyzed": len(analyses),
            "drafts_to_review": len(drafts),
            "accepted_resumes": sum(1 for v in versions if v.status == "accepted"),
            "applications": {s: sum(1 for a in apps if a.status == s) for s in sorted({a.status for a in apps})},
        },
        "profile": {"verified": sum(n for v, n in facts if v), "unverified": sum(n for v, n in facts if not v), "version": version},
        "llm": {"provider": cfg["provider"], "model": cfg["model"], "external": cfg["provider"] in REMOTE_PROVIDERS},
        "review": [job_row(draft_jobs[v.job_id], version_id=str(v.id), counts=(v.audit or {}).get("counts"), blocked=bool((v.audit or {}).get("blocked")))
                   for v in drafts[:10] if v.job_id in draft_jobs],
        "generate": [job_row(jobs[a.job_id], score=a.match.get("score")) for a in scored if a.job_id not in has_version][:10],
        "apply": [job_row(app_jobs[a.job_id], application_id=str(a.id)) for a in apps if a.status == "ready_to_apply" and a.job_id in app_jobs][:10],
        "top_matches": [job_row(jobs[a.job_id], score=a.match.get("score"), stale=a.profile_version != version) for a in scored[:10]],
        "stale_matches": sum(1 for a in analyses if a.evidence is not None and a.profile_version != version),
    }


@router.get("/privacy")
def privacy(db: Session = Depends(get_db)):
    """Which provider receives what. Anything not Ollama leaves this machine."""
    from backend.analyzer.llm_client import REMOTE_PROVIDERS, ollama_base_url, resolve_llm_config
    features = [
        ("copilot", "Job analysis, evidence matching, résumé writing and audit", "job descriptions and your verified profile facts"),
        ("scoring", "Legacy AI score", "job descriptions and base résumés"),
        ("cv_tailor", "Legacy résumé tailoring", "job descriptions and a résumé"),
        ("cover_letter", "Cover letters", "job descriptions, a résumé and preferences"),
        ("autofill", "Application question drafts", "the question, your profile and saved answers"),
        ("email", "Email classification", "email subject and body"),
        ("", "Résumé PDF import", "the text of the uploaded résumé"),
    ]
    rows = []
    for key, label, sends in features:
        cfg = resolve_llm_config(key, db=db)
        remote = cfg["provider"] in REMOTE_PROVIDERS
        rows.append({"feature": key or "primary", "label": label, "provider": cfg["provider"], "model": cfg["model"],
                     "external": remote, "sends": sends,
                     "destination": ollama_base_url(db) if cfg["provider"] == "ollama" else cfg["provider"]})
    return {"features": rows, "any_external": any(r["external"] for r in rows)}
