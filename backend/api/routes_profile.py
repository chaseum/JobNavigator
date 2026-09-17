"""Career Evidence: the Résumé Library, the candidate fact database, and the reconciliation between them.

The user's job is to upload the résumés they already have, then review what was
extracted — not to retype a dossier. Uploaded documents are `ResumeSource` rows;
the canonical facts they support stay `CandidateFact`, linked through
`CandidateFactSource` so every fact can say which résumés back it. Reconciliation
and the conflict rules live in backend/copilot/evidence.py.

Identity answers (contact, work authorization, preferences, EEO) stay on the
Persona singleton and are edited through PATCH /api/persona; this router owns
the career facts every generated claim must cite.
"""
import json
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.copilot import evidence as EV
from backend.copilot import facts as F
from backend.job_monitor import JobAlreadyRunningError, is_running, launch_background
from backend.models.db import (CandidateFact, CandidateFactSource, FactConflict, Persona, Resume,
                               ResumeSource, get_db, utcnow)

logger = logging.getLogger("jobnavigator.profile")
router = APIRouter(prefix="/profile", tags=["profile"])

MAX_BATCH = 25   # a folder of historical résumés, not a bulk-upload endpoint

# Re-exported: extraction and date parsing moved to backend/copilot/evidence.py
# when reconciliation grew past a signature tuple, but this is where callers look.
facts_from_resume_json = EV.facts_from_resume_json
parse_date_range = EV.parse_date_range


@router.get("/knowledge/search")
async def search_knowledge(
    q: str = Query("", min_length=0, max_length=500),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Semantic search over persisted Knowledge Bank chunks."""
    if not q.strip():
        return {"query": q, "results": []}
    try:
        from backend.copilot.knowledge import index_facts, search
        await index_facts(db)
        db.commit()
        return {"query": q, "results": await search(db, q, limit)}
    except Exception as exc:
        logger.warning("Knowledge Bank search unavailable: %s", str(exc)[:400])
        raise HTTPException(503, "Knowledge Bank semantic search is unavailable; check Ollama and its embedding model") from exc


def _fact_dict(f: CandidateFact, sources: list | None = None, conflicts: int = 0) -> dict:
    return {
        "id": f.id,
        "ref": F.fact_ref(f),
        "kind": f.kind,
        "parent_id": f.parent_id,
        "data": f.data or {},
        "verified": bool(f.verified),
        "source": f.source,
        "sort_order": f.sort_order,
        # which uploaded résumés support this fact ("Used in: resume-swe-2025.pdf")
        "sources": sources or [],
        "open_conflicts": conflicts,
        "updated_at": f.updated_at.isoformat() if f.updated_at else None,
    }


def _check_parent(db, kind: str, parent_id) -> Optional[int]:
    if parent_id in (None, ""):
        return None
    if kind != "achievement":
        raise HTTPException(400, "only achievements have a parent")
    parent = db.get(CandidateFact, int(parent_id))
    if not parent or parent.kind not in F.PARENT_KINDS:
        raise HTTPException(400, "parent must be an experience, internship, project, research or education fact")
    return parent.id


def _check_evidence(db, data: dict) -> None:
    """A skill's evidence_ids must name existing facts; a dangling reference is a claim with no source."""
    for ref in data.get("evidence_ids") or []:
        parsed = F.parse_ref(ref)
        row = db.get(CandidateFact, parsed[1]) if parsed else None
        if not row or row.kind != parsed[0]:
            raise HTTPException(400, f"evidence_ids: {ref!r} is not a fact in your profile")


def _validated(kind: str, data) -> dict:
    try:
        return F.validate_data(kind, data if isinstance(data, dict) else {})
    except ValueError as e:
        raise HTTPException(400, str(e))


def _provenance(db) -> tuple[dict, dict]:
    """({fact_id: [{id, filename, locator, raw_text}]}, {fact_id: open conflict count})."""
    names = dict(db.query(ResumeSource.id, ResumeSource.filename).all())
    by_fact: dict = {}
    for link in db.query(CandidateFactSource).order_by(CandidateFactSource.id).all():
        by_fact.setdefault(link.candidate_fact_id, []).append({
            "id": link.resume_source_id, "filename": names.get(link.resume_source_id, "?"),
            "locator": link.locator, "raw_text": link.raw_text})
    conflicts = dict(db.query(FactConflict.candidate_fact_id, func.count())
                     .filter(FactConflict.status == "open").group_by(FactConflict.candidate_fact_id).all())
    return by_fact, conflicts


@router.get("")
def get_profile(db: Session = Depends(get_db)):
    p = db.query(Persona).filter(Persona.id == 1).first()
    rows = db.query(CandidateFact).order_by(CandidateFact.sort_order, CandidateFact.id).all()
    by_fact, conflicts = _provenance(db)
    return {
        "identity": {k: (getattr(p, k, None) or {}) for k in ("contact", "work_auth", "preferences", "compensation", "demographics")},
        "facts": [_fact_dict(f, by_fact.get(f.id), conflicts.get(f.id, 0)) for f in rows],
        "schema": F.schema_for_ui(),
        "profile_version": F.profile_version(db),
        "unverified": sum(1 for f in rows if not f.verified),
        "open_conflicts": sum(conflicts.values()),
        "resume_count": db.query(ResumeSource).count(),
        "importing": bool(is_running(EV.IMPORT_JOB)),
    }


@router.post("/facts", status_code=201)
def create_fact(body: dict, db: Session = Depends(get_db)):
    kind = str(body.get("kind") or "")
    data = _validated(kind, body.get("data"))
    _check_evidence(db, data)
    row = CandidateFact(
        kind=kind,
        parent_id=_check_parent(db, kind, body.get("parent_id")),
        data=data,
        # typed by the user, so it is their word; imports go through /import instead
        verified=bool(body.get("verified", True)),
        source=body.get("source") if body.get("source") in ("manual", "gap") else "manual",
        sort_order=int(body.get("sort_order") or 0),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _fact_dict(row)


@router.patch("/facts/{fact_id}")
def update_fact(fact_id: int, body: dict, db: Session = Depends(get_db)):
    row = db.get(CandidateFact, fact_id)
    if not row:
        raise HTTPException(404, "fact not found")
    if "data" in body:
        data = _validated(row.kind, body["data"])
        _check_evidence(db, data)
        row.data = data
    if "verified" in body:
        row.verified = bool(body["verified"])
    if "parent_id" in body:
        row.parent_id = _check_parent(db, row.kind, body["parent_id"])
    if "sort_order" in body:
        row.sort_order = int(body["sort_order"] or 0)
    row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    return _fact_dict(row)


@router.delete("/facts/{fact_id}")
def delete_fact(fact_id: int, db: Session = Depends(get_db)):
    row = db.get(CandidateFact, fact_id)
    if not row:
        raise HTTPException(404, "fact not found")
    ref = F.fact_ref(row)
    db.delete(row)
    # drop the reference from any skill that cited it, so no skill claims deleted evidence
    for skill in db.query(CandidateFact).filter(CandidateFact.kind == "skill").all():
        ids = (skill.data or {}).get("evidence_ids") or []
        if ref in ids:
            skill.data = {**skill.data, "evidence_ids": [x for x in ids if x != ref]}
    db.commit()
    return {"deleted": ref}


@router.post("/facts/verify")
def verify_facts(body: dict, db: Session = Depends(get_db)):
    ids = [int(i) for i in (body.get("ids") or []) if str(i).isdigit()]
    rows = db.query(CandidateFact).filter(CandidateFact.id.in_(ids)).all() if ids else []
    for r in rows:
        r.verified = True
    db.commit()
    return {"verified": len(rows)}




# ── Résumé Library ───────────────────────────────────────────────────────────
# Onboarding is "upload the résumés you already have", not "fill in a dossier":
#   upload many PDFs -> extract claims from each -> reconcile -> review conflicts
#   -> verified Career Evidence -> role-family and job-specific résumés.
# Extraction and reconciliation live in backend/copilot/evidence.py; nothing an
# import produces is cited by a generated résumé until the user verifies it.

def _source_dict(s: ResumeSource, facts: int = 0, conflicts: int = 0) -> dict:
    return {
        "id": s.id, "filename": s.filename, "mime_type": s.mime_type or "application/pdf", "sha256": s.sha256,
        "status": s.status, "error": s.error,
        "uploaded_at": s.uploaded_at.isoformat() if s.uploaded_at else None,
        "imported_at": s.imported_at.isoformat() if s.imported_at else None,
        "result": s.result or {}, "report": s.report or (s.result or {}).get("report", []),
        "facts": facts, "conflicts": conflicts,
    }


def _conflict_dict(c: FactConflict, fact: CandidateFact | None, filename: str | None) -> dict:
    return {
        "id": c.id, "fact_id": c.candidate_fact_id, "field": c.field,
        "current_value": c.current_value, "proposed_value": c.proposed_value,
        "status": c.status, "resolved_value": c.resolved_value,
        "from_resume": filename,
        "kind": fact.kind if fact else None,
        "headline": F.fact_headline(fact.kind, fact.data or {}) if fact else "",
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("/resumes")
def list_resume_sources(db: Session = Depends(get_db)):
    """The Résumé Library: every uploaded document, its import status and what it produced."""
    sources = db.query(ResumeSource).order_by(ResumeSource.uploaded_at.desc(), ResumeSource.id.desc()).all()
    facts = dict(db.query(CandidateFactSource.resume_source_id, func.count())
                 .group_by(CandidateFactSource.resume_source_id).all())
    conflicts = dict(db.query(FactConflict.resume_source_id, func.count())
                     .filter(FactConflict.status == "open").group_by(FactConflict.resume_source_id).all())
    statuses = dict(db.query(ResumeSource.status, func.count()).group_by(ResumeSource.status).all())
    return {
        "sources": [_source_dict(s, facts.get(s.id, 0), conflicts.get(s.id, 0)) for s in sources],
        "importing": bool(is_running(EV.IMPORT_JOB)),
        "progress": {"total": len(sources), "pending": statuses.get("pending", 0),
                     "parsing": statuses.get("parsing", 0), "imported": statuses.get("imported", 0),
                     "failed": statuses.get("failed", 0)},
        "open_conflicts": db.query(FactConflict).filter(FactConflict.status == "open").count(),
    }


@router.post("/resumes", status_code=202)
async def upload_resume_sources(files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    """Drop career documents into the Knowledge Bank at once.

    Text is extracted here (offline, fast) so the PDF bytes need not be kept; the
    LLM structuring and the reconciliation run in one background job over every
    pending document, because that is N provider calls and must not hold a request
    open. A file already in the library is reported, not imported twice — sha256
    is what makes re-dropping a folder harmless.
    """
    from backend.api.routes_resumes import check_pdf_size, extract_pdf_text
    from backend.copilot.knowledge import SUPPORTED_EXTENSIONS, extract_document_text, mime_type_for
    if not files:
        raise HTTPException(400, "choose at least one document")
    if len(files) > MAX_BATCH:
        raise HTTPException(400, f"upload at most {MAX_BATCH} résumés at a time")

    accepted, skipped, rejected = [], [], []
    for upload in files:
        name = getattr(upload, "filename", "") or "resume.pdf"
        try:
            suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if suffix not in SUPPORTED_EXTENSIONS:
                raise HTTPException(400, "Only PDF, Markdown, TXT, and DOCX files are accepted")
            # Keep rejecting the malformed MIME shape used by the retired
            # résumé-only client while accepting normal browser MIME values.
            if suffix != ".pdf" and getattr(upload, "content_type", "") == "application/pdf":
                raise HTTPException(400, "The uploaded MIME type does not match the document extension")
            pdf = await upload.read()
            check_pdf_size(pdf)
            sha = EV.digest(pdf)
            existing = db.query(ResumeSource).filter(ResumeSource.sha256 == sha).first()
            if existing is not None:
                skipped.append({"filename": name, "reason": f"already in the library as {existing.filename}",
                                "id": existing.id})
                continue
            parsed_text = extract_pdf_text(pdf) if suffix == ".pdf" else extract_document_text(pdf, name)
            row = ResumeSource(filename=name[:255], mime_type=mime_type_for(name), sha256=sha,
                               status="pending", parsed_text=parsed_text)
            db.add(row)
            db.flush()
            accepted.append(_source_dict(row))
        except HTTPException as e:
            rejected.append({"filename": name, "reason": str(e.detail)})
        except Exception as e:                     # one unreadable file must not lose the batch
            logger.warning(f"résumé upload {name!r} rejected: {e}")
            rejected.append({"filename": name, "reason": str(e)[:200]})
    db.commit()

    run_id = None
    if accepted:
        try:
            run_id = launch_background(EV.IMPORT_JOB, EV.import_pending, trigger="manual",
                                       scope_key=f"batch:{uuid.uuid4()}",
                                       func_kwargs={"source_ids": [s["id"] for s in accepted]})
        except JobAlreadyRunningError:
            # The scope is unique per upload, so this is only a defensive guard.
            logger.warning("résumé import launch was already running unexpectedly")
    return {"accepted": accepted, "skipped": skipped, "rejected": rejected,
            "run_id": run_id, "importing": bool(accepted) or bool(is_running(EV.IMPORT_JOB))}


@router.post("/resumes/reprocess")
async def reprocess_resume_sources(body: dict | None = None, db: Session = Depends(get_db)):
    """Re-run extraction/reconciliation from stored parsed text without re-uploading PDFs."""
    body = body or {}
    requested = body.get("source_id")
    q = db.query(ResumeSource)
    if requested not in (None, ""):
        row = db.get(ResumeSource, int(requested))
        if not row:
            raise HTTPException(404, "résumé source not found")
        rows = [row]
    else:
        rows = q.order_by(ResumeSource.id).all()
    ids = []
    for row in rows:
        if not (row.parsed_text or row.parsed_json):
            continue
        row.status, row.error = "pending", None
        ids.append(row.id)
    db.commit()
    if not ids:
        return {"accepted": 0, "run_id": None, "message": "No stored résumé text is available to reprocess"}
    run_id = launch_background(EV.IMPORT_JOB, EV.import_pending, trigger="manual",
                               scope_key=f"reprocess:{uuid.uuid4()}",
                               func_kwargs={"source_ids": ids})
    return {"accepted": len(ids), "source_ids": ids, "run_id": run_id}


@router.post("/resumes/{source_id}/retry")
async def retry_resume_source(source_id: int, db: Session = Depends(get_db)):
    row = db.get(ResumeSource, source_id)
    if not row:
        raise HTTPException(404, "résumé source not found")
    if row.status not in {"failed", "waiting_for_ai"}:
        raise HTTPException(409, "only failed or AI-waiting document imports can be retried")
    row.status, row.error = "pending", None
    db.commit()
    run_id = launch_background(EV.IMPORT_JOB, EV.import_pending, trigger="manual",
                               scope_key=f"retry:{source_id}:{uuid.uuid4()}",
                               func_kwargs={"source_ids": [source_id]})
    return {"source_id": source_id, "run_id": run_id}


@router.delete("/resumes/{source_id}")
def delete_resume_source(source_id: int, db: Session = Depends(get_db)):
    """Forget a source document. The facts it supported stay — other résumés may support them too."""
    row = db.get(ResumeSource, source_id)
    if not row:
        raise HTTPException(404, "résumé source not found")
    db.delete(row)
    db.commit()
    return {"deleted": source_id}


@router.get("/conflicts")
def list_conflicts(status: str = "open", db: Session = Depends(get_db)):
    """Fields two résumés state differently. The user picks the canonical value; no model does."""
    q = db.query(FactConflict)
    if status in ("open", "resolved"):
        q = q.filter(FactConflict.status == status)
    rows = q.order_by(FactConflict.id).all()
    facts = {f.id: f for f in db.query(CandidateFact).filter(
        CandidateFact.id.in_([c.candidate_fact_id for c in rows])).all()} if rows else {}
    names = dict(db.query(ResumeSource.id, ResumeSource.filename).all())
    return [_conflict_dict(c, facts.get(c.candidate_fact_id), names.get(c.resume_source_id)) for c in rows]


@router.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: int, body: dict, db: Session = Depends(get_db)):
    """Choose `current`, `proposed`, or type a value of your own."""
    c = db.get(FactConflict, conflict_id)
    if not c:
        raise HTTPException(404, "conflict not found")
    if c.status != "open":
        raise HTTPException(409, "this conflict is already resolved")
    choice = str(body.get("choice") or "").strip()
    value = body.get("value")
    if choice == "current":
        value = c.current_value
    elif choice == "proposed":
        value = c.proposed_value
    elif not isinstance(value, str):
        raise HTTPException(400, "send choice=current, choice=proposed, or a value")
    try:
        fact = EV.resolve_conflict(db, c, value)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return {"conflict_id": c.id, "field": c.field, "value": value, "fact": _fact_dict(fact)}


# ── single-document import (Persona content, a base Résumé row, or one PDF) ───
# The same reconciliation as the library: one code path decides what is a
# duplicate, what is new evidence and what is a conflict.

@router.post("/import")
async def import_profile(request: Request, file: Optional[UploadFile] = File(None), db: Session = Depends(get_db)):
    """Import a PDF, a base résumé (`resume_id`) or the Persona's résumé content (`{"source": "persona"}`) as unverified facts."""
    if file is not None and getattr(file, "filename", None):
        from backend.api.routes_resumes import check_pdf_name, check_pdf_size, parse_resume_pdf
        check_pdf_name(file.filename)
        pdf = await file.read()
        check_pdf_size(pdf)
        json_data, label, sha = await parse_resume_pdf(pdf, db), file.filename, EV.digest(pdf)
    else:
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        if body.get("source") == "persona":
            p = db.query(Persona).filter(Persona.id == 1).first()
            json_data, label = (p.resume_content if p else None) or {}, "persona"
        elif body.get("resume_id"):
            r = db.query(Resume).filter(Resume.id == str(body["resume_id"])).first()
            if not r:
                raise HTTPException(404, "Resume not found")
            json_data, label = r.json_data or {}, r.name
        else:
            raise HTTPException(400, "Upload a PDF, or send resume_id or source=persona")
        sha = EV.digest(json.dumps(json_data, sort_keys=True, default=str).encode())

    source = db.query(ResumeSource).filter(ResumeSource.sha256 == sha).first()
    if source is None:
        source = ResumeSource(filename=str(label)[:255], sha256=sha, status="imported",
                              parsed_json=json_data, imported_at=utcnow())
        db.add(source)
        db.flush()
    tally = EV.reconcile(db, EV.facts_from_resume_json(json_data), source)
    source.result, source.report, source.status, source.imported_at = tally, tally.get("report", []), "imported", utcnow()
    result = {**tally, "created": tally[EV.Outcome.NOVEL],
              "skipped": tally[EV.Outcome.DUPLICATE] + tally["dropped"],
              "source_id": source.id}
    result["contact_filled"] = EV._fill_contact(db, (json_data or {}).get("header") or {})
    db.commit()
    return result
