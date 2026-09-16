"""Résumé versions: generate, review claim by claim, rebuild, accept, download."""
import functools
import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session, undefer

from backend.copilot import versions as V
from backend.job_monitor import JobAlreadyRunningError, launch_background
from backend.models.db import ImmutableVersionError, Job, ResumeVersion, get_db

router = APIRouter(prefix="/resume-versions", tags=["resume-versions"])


def _summary(v: ResumeVersion) -> dict:
    return {
        "id": str(v.id), "job_id": str(v.job_id) if v.job_id else None, "kind": v.kind, "status": v.status,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "accepted_at": v.accepted_at.isoformat() if v.accepted_at else None,
        "template": v.template, "template_version": v.template_version,
        "candidate_profile_version": v.candidate_profile_version, "job_analysis_id": v.job_analysis_id,
        "provider": v.provider, "model": v.model, "pages": v.pages,
        "counts": (v.audit or {}).get("counts"), "blocked": bool((v.audit or {}).get("blocked")),
        "parser_health": (v.parser_health or {}).get("score"), "output_dir": v.output_dir,
        "match_score": ((v.job_analysis or {}).get("match") or {}).get("score"),
        "role_family": v.role_family,
    }


def _get(db, version_id: str, *load) -> ResumeVersion:
    try:
        _uuid.UUID(version_id)
    except ValueError:
        raise HTTPException(404, "résumé version not found")
    q = db.query(ResumeVersion)
    if load:
        q = q.options(*[undefer(getattr(ResumeVersion, c)) for c in load])
    v = q.filter(ResumeVersion.id == version_id).first()
    if v is None:
        raise HTTPException(404, "résumé version not found")
    return v


def _errors(fn):
    """Map pipeline errors to HTTP: review rules 409/400, missing rows 404."""
    @functools.wraps(fn)   # FastAPI reads the handler's real signature through __wrapped__
    async def run(*a, **kw):
        try:
            return await fn(*a, **kw)
        except (V.ReviewError, ImmutableVersionError) as e:
            raise HTTPException(409, str(e))
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
    return run


@router.get("")
def list_versions(job_id: str | None = None, status: str | None = None, db: Session = Depends(get_db)):
    q = db.query(ResumeVersion)
    if job_id:
        q = q.filter(ResumeVersion.job_id == job_id)
    if status:
        q = q.filter(ResumeVersion.status == status)
    rows = q.order_by(ResumeVersion.created_at.desc()).limit(200).all()
    jobs = {j.id: j for j in db.query(Job).filter(Job.id.in_([r.job_id for r in rows if r.job_id])).all()} if rows else {}
    return [{**_summary(r), "company": getattr(jobs.get(r.job_id), "company", None), "title": getattr(jobs.get(r.job_id), "title", None)}
            for r in rows]


@router.get("/role-families")
def role_families(db: Session = Depends(get_db)):
    """Each role family and the base résumé it currently has, for viewing and regenerating them.

    A family base is a selection over the same verified Career Evidence, so
    regenerating one never changes a fact — only what that role's résumé leads with.
    """
    from backend.copilot import resume_audits as RA
    from backend.copilot import role_families as RF
    out = []
    for f in RF.families(db):
        base = RA.base_version(db, f["id"])
        if base is not None and base.role_family != f["id"]:
            base = None                       # the universal base is a fallback, not this family's base
        out.append({"id": f["id"], "label": f["label"], "builtin": f["builtin"],
                    "base": _summary(base) if base is not None else None})
    universal = RA.base_version(db)
    return {"families": out,
            "universal_base": _summary(universal) if universal is not None and not universal.role_family else None}


@router.post("/base", status_code=201)
@_errors
async def create_base(body: dict | None = None):
    """Generate a base résumé; `role_family` makes it that family's base instead of the universal one."""
    vid = await V.generate_base((body or {}).get("role_family") or None)
    db = V.SessionLocal()
    try:
        return _summary(db.get(ResumeVersion, vid))
    finally:
        db.close()


@router.post("/for-job/{job_id}", status_code=202)
async def create_for_job(job_id: str, db: Session = Depends(get_db)):
    from backend.copilot.analysis import latest_record
    try:
        rec = latest_record(db, _uuid.UUID(job_id))
    except ValueError:
        raise HTTPException(404, "job not found")
    if rec is None or rec.evidence is None:
        raise HTTPException(400, "analyze this job before generating a résumé")
    try:
        run_id = launch_background("copilot_resume", V.generate_tailored, trigger="manual", scope_key=job_id,
                                   target_job_id=_uuid.UUID(job_id), func_kwargs={"job_id": job_id})
    except ValueError:
        raise HTTPException(404, "job not found")
    except JobAlreadyRunningError:
        return JSONResponse(status_code=409, content={"detail": "a résumé is already being generated for this job"})
    return {"run_id": run_id, "status": "running"}


@router.get("/{version_id}")
def get_version(version_id: str, db: Session = Depends(get_db)):
    v = _get(db, version_id)
    base = (db.get(ResumeVersion, v.base_version_id) if v.base_version_id else None) or V.latest_accepted(db, kind="base")
    return {**_summary(v), "resume": v.resume_json, "audit": v.audit, "parser_health_detail": v.parser_health,
            "compile_log": v.compile_log, "job_analysis": v.job_analysis,
            "base": {"id": str(base.id), "resume": base.resume_json} if base and base.id != v.id else None}


@router.get("/{version_id}/pdf")
def get_pdf(version_id: str, db: Session = Depends(get_db)):
    v = _get(db, version_id, "pdf")
    if not v.pdf:
        raise HTTPException(404, "no PDF for this version: " + (v.compile_log or "not built yet")[:300])
    return Response(v.pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="resume-{str(v.id)[:8]}.pdf"'})


@router.get("/{version_id}/tex")
def get_tex(version_id: str, db: Session = Depends(get_db)):
    v = _get(db, version_id, "latex")
    return Response(v.latex or "", media_type="application/x-tex", headers={"Content-Disposition": 'attachment; filename="resume.tex"'})


@router.get("/{version_id}/export.zip")
def get_zip(version_id: str, db: Session = Depends(get_db)):
    v = _get(db, version_id, "latex", "pdf")
    return Response(V.export_zip(v), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="resume-{str(v.id)[:8]}-overleaf.zip"'})


@router.post("/{version_id}/bullets/{bullet_id}")
@_errors
async def review(version_id: str, bullet_id: str, body: dict):
    return V.review_bullet(version_id, bullet_id, str(body.get("action") or ""), body.get("text"))


@router.post("/{version_id}/bullets/{bullet_id}/regenerate")
@_errors
async def regenerate(version_id: str, bullet_id: str):
    return await V.regenerate_bullet(version_id, bullet_id)


@router.post("/{version_id}/rebuild")
@_errors
async def rebuild(version_id: str):
    db = V.SessionLocal()
    try:
        if db.get(ResumeVersion, version_id) is None:
            raise LookupError("résumé version not found")
    finally:
        db.close()
    await V.rebuild(version_id)
    return {"ok": True}


@router.post("/{version_id}/overleaf")
def push_to_overleaf(version_id: str, db: Session = Depends(get_db)):
    from backend.copilot import overleaf
    from backend.models.db import Setting
    v = _get(db, version_id, "latex", "pdf")
    rows = {r.key: r.value for r in db.query(Setting).filter(Setting.key.in_(["overleaf_mode", "overleaf_git_remote"])).all()}
    if rows.get("overleaf_mode") != "git" or not (rows.get("overleaf_git_remote") or "").strip():
        raise HTTPException(400, "set Overleaf to Git mode and configure the remote in Settings first")
    job = db.query(Job).filter(Job.id == v.job_id).first() if v.job_id else None
    try:
        return overleaf.push(v, job, rows["overleaf_git_remote"].strip())
    except overleaf.OverleafError as e:
        raise HTTPException(409 if "accepted" in str(e) else 502, str(e))


@router.post("/{version_id}/accept")
@_errors
async def accept(version_id: str, body: dict | None = None):
    return await V.accept_version(version_id, accept_all=bool((body or {}).get("all")))


@router.post("/{version_id}/reject")
@_errors
async def reject(version_id: str):
    V.reject_version(version_id)
    return {"id": version_id, "status": "rejected"}
