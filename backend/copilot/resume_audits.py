"""Resume Applicability per résumé version: compute, store, and assemble the job's résumé workspace.

The audit is deterministic (applicability.py), so what the screen shows is always
recomputed from the current draft. A ResumeAudit row is written whenever a match
runs, a résumé is generated or the user asks for an audit; it records what a
version scored against an analysis and profile version, for a reproducible
before/after.
"""
import hashlib
import json

from backend.copilot import applicability as AP
from backend.copilot import facts as F
from backend.models.db import ResumeAudit, ResumeVersion, SessionLocal


def resume_hash(v: ResumeVersion) -> str:
    blob = json.dumps([v.resume_json, (v.audit or {}).get("claims")], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def claims_of(v: ResumeVersion) -> dict:
    return {c["bullet_id"]: c for c in (v.audit or {}).get("claims") or []}


def base_version(db, role_family: str | None = None):
    """The résumé a job is first audited against: the newest accepted base, else the newest base draft.

    With a role family, its own base is preferred — a PM application should be
    measured against, and tailored from, the PM selection — and the universal base
    is the fallback when that family has no base yet.
    """
    def newest(q):
        return (q.filter(ResumeVersion.status == "accepted").order_by(ResumeVersion.accepted_at.desc()).first()
                or q.order_by(ResumeVersion.created_at.desc()).first())

    base = db.query(ResumeVersion).filter(ResumeVersion.kind == "base", ResumeVersion.status != "rejected")
    if role_family:
        found = newest(base.filter(ResumeVersion.role_family == role_family))
        if found is not None:
            return found
    return newest(base)


def tailored_version(db, job_id):
    return (db.query(ResumeVersion).filter(ResumeVersion.job_id == job_id, ResumeVersion.kind == "tailored",
                                           ResumeVersion.status != "rejected")
            .order_by(ResumeVersion.created_at.desc()).first())


def compute(db, rec, v: ResumeVersion, facts_by_ref: dict | None = None) -> dict:
    from backend.copilot.analysis import evidence_context, weights
    if facts_by_ref is None:
        _, facts_by_ref, _ = evidence_context(db)
    a = rec.analysis or {}
    result = AP.audit(a.get("requirements") or [], rec.evidence or [], v.resume_json or {}, facts_by_ref,
                      claims_of(v), weights(db), a.get("technologies") or [])
    result.update(resume_version_id=str(v.id), kind=v.kind, version_status=v.status, job_analysis_id=rec.id,
                  parser_health=(v.parser_health or {}).get("score"), resume_hash=resume_hash(v))
    return result


def store(db, rec, v: ResumeVersion, result: dict) -> None:
    db.add(ResumeAudit(job_analysis_id=rec.id, resume_version_id=v.id, kind=v.kind, profile_version=F.profile_version(db),
                       resume_hash=result["resume_hash"], score=result["score"], maximum=result["maximum"],
                       parser_health=result["parser_health"], result=result))


def audit_job(job_id, version_ids=None) -> dict:
    """Audit and store the base résumé and this job's latest tailored résumé (or `version_ids`) against its latest analysis."""
    from backend.copilot.analysis import evidence_context, latest_record
    db = SessionLocal()
    try:
        rec = latest_record(db, job_id)
        if rec is None or rec.evidence is None:
            return {}
        _, facts_by_ref, _ = evidence_context(db)
        targets = ([db.get(ResumeVersion, vid) for vid in version_ids] if version_ids
                   else [base_version(db), tailored_version(db, job_id)])
        out = {}
        for v in (t for t in targets if t is not None):
            out[str(v.id)] = compute(db, rec, v, facts_by_ref)
            store(db, rec, v, out[str(v.id)])
        db.commit()
        return out
    finally:
        db.close()


def _version_summary(v):
    if v is None:
        return None
    return {"id": str(v.id), "kind": v.kind, "status": v.status, "created_at": v.created_at.isoformat() if v.created_at else None,
            "parser_health": (v.parser_health or {}).get("score"), "blocked": bool((v.audit or {}).get("blocked"))}


def workspace(db, job_id, rec, base_id=None) -> dict | None:
    """Live base and tailored audits for the job screen, their comparison, and when each was last stored."""
    from backend.copilot.analysis import evidence_context
    if rec is None or rec.evidence is None:
        return None
    _, facts_by_ref, _ = evidence_context(db)
    tail = tailored_version(db, job_id)
    base = (db.get(ResumeVersion, base_id) if base_id else None) or (
        db.get(ResumeVersion, tail.base_version_id) if tail is not None and tail.base_version_id else None) or base_version(db)
    out = {"base_version": _version_summary(base), "tailored_version": _version_summary(tail), "base": None, "tailored": None,
           "comparison": None, "disclaimer": AP.DISCLAIMER, "target": AP.TARGET}
    for key, v in (("base", base), ("tailored", tail)):
        if v is None:
            continue
        live = compute(db, rec, v, facts_by_ref)
        row = (db.query(ResumeAudit).filter(ResumeAudit.job_analysis_id == rec.id, ResumeAudit.resume_version_id == v.id)
               .order_by(ResumeAudit.id.desc()).first())
        live["stored_at"] = row.created_at.isoformat() if row is not None and row.created_at else None
        live["stored_score"] = row.score if row is not None else None
        live["changed_since_stored"] = row is None or row.resume_hash != live["resume_hash"] or row.score != live["score"]
        out[key] = live
    if out["base"] and out["tailored"]:
        out["comparison"] = AP.compare(out["base"], out["tailored"])
    return out
