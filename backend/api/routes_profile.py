"""Profile: the candidate fact database (CRUD, verification, résumé import).

Identity answers (contact, work authorization, preferences, EEO) stay on the
Persona singleton and are edited through PATCH /api/persona; this router owns
the career facts every generated claim must cite.
"""
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from backend.copilot import facts as F
from backend.models.db import CandidateFact, Persona, Resume, get_db, utcnow

logger = logging.getLogger("jobnavigator.profile")
router = APIRouter(prefix="/profile", tags=["profile"])


def _fact_dict(f: CandidateFact) -> dict:
    return {
        "id": f.id,
        "ref": F.fact_ref(f),
        "kind": f.kind,
        "parent_id": f.parent_id,
        "data": f.data or {},
        "verified": bool(f.verified),
        "source": f.source,
        "sort_order": f.sort_order,
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


@router.get("")
def get_profile(db: Session = Depends(get_db)):
    p = db.query(Persona).filter(Persona.id == 1).first()
    rows = db.query(CandidateFact).order_by(CandidateFact.sort_order, CandidateFact.id).all()
    return {
        "identity": {k: (getattr(p, k, None) or {}) for k in ("contact", "work_auth", "preferences", "compensation", "demographics")},
        "facts": [_fact_dict(f) for f in rows],
        "schema": F.schema_for_ui(),
        "profile_version": F.profile_version(db),
        "unverified": sum(1 for f in rows if not f.verified),
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


# ── import ───────────────────────────────────────────────────────────────────
# A résumé (base Resume row, the Persona's résumé content, or a PDF) becomes
# UNVERIFIED facts. Nothing imported is cited until the user verifies it, and an
# import never overwrites an existing fact or a contact answer already set.

_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_PART_RE = re.compile(r"(?:([a-z]{3})[a-z]*\.?\s+)?((?:19|20)\d{2})|(present|current|now)", re.I)
_RANGE_SPLIT_RE = re.compile(r"\s*(?:[–—−]|\s-\s|-(?=\s*[A-Za-z]{3}|\s*(?:19|20)\d{2})|\bto\b)\s*")


def _one_date(text: str) -> str:
    m = _PART_RE.search(text or "")
    if not m:
        return ""
    if m.group(3):
        return "present"
    mon = _MONTHS.get((m.group(1) or "").lower()[:3])
    return f"{m.group(2)}-{mon:02d}" if mon else m.group(2)


def parse_date_range(text: str) -> tuple[str, str]:
    """'May 2021 – Present' -> ('2021-05', 'present'); anything unparseable -> ''."""
    parts = [p for p in _RANGE_SPLIT_RE.split(str(text or "").strip()) if p]
    if not parts:
        return "", ""
    start = _one_date(parts[0])
    end = _one_date(parts[-1]) if len(parts) > 1 else ""
    return start, end


def _split_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in re.split(r"[,;•|]", str(v or "")) if x.strip()]


def facts_from_resume_json(json_data: dict) -> list[dict]:
    """Résumé json_data -> [{kind, data, children:[...]}]; pure, so it is tested without a DB."""
    out = []
    for e in (json_data or {}).get("experience") or []:
        if not isinstance(e, dict) or not (e.get("company") or e.get("title")):
            continue
        start, end = parse_date_range(e.get("date") or e.get("dates") or "")
        title = str(e.get("title") or "").strip() or "Role"
        out.append({
            "kind": "internship" if "intern" in title.lower() else "experience",
            "data": {"employer": str(e.get("company") or "").strip() or "Unknown", "title": title,
                     "location": str(e.get("location") or ""), "start_date": start, "end_date": end,
                     "description": str(e.get("description") or ""),
                     "notes": "" if start else f"Imported date: {e.get('date') or ''}".strip(": ")},
            "children": [{"kind": "achievement", "data": {"text": str(b).strip()}}
                         for b in e.get("bullets") or [] if str(b).strip()],
        })
    for ed in (json_data or {}).get("education") or []:
        if not isinstance(ed, dict) or not ed.get("school"):
            continue
        _, grad = parse_date_range(ed.get("years") or ed.get("year") or "")
        if not grad:
            grad = _one_date(str(ed.get("years") or ed.get("year") or ""))
        out.append({"kind": "education", "data": {
            "institution": str(ed["school"]).strip(), "degree": str(ed.get("degree") or ""),
            "location": str(ed.get("location") or ""), "graduation_date": grad if grad != "present" else ""}})
    for pr in (json_data or {}).get("projects") or []:
        if not isinstance(pr, dict) or not pr.get("name"):
            continue
        out.append({"kind": "project",
                    "data": {"name": str(pr["name"]).strip(), "description": str(pr.get("description") or "")},
                    "children": [{"kind": "achievement", "data": {"text": str(b).strip()}}
                                 for b in pr.get("bullets") or [] if str(b).strip()]})
    skills = (json_data or {}).get("skills") or {}
    groups = skills.items() if isinstance(skills, dict) else [("", skills)]
    for category, items in groups:
        for name in _split_list(items):
            out.append({"kind": "skill", "data": {"name": name, "category": str(category or "")}})
    for pub in (json_data or {}).get("publications") or []:
        if isinstance(pub, dict) and pub.get("title"):
            out.append({"kind": "publication", "data": {"title": str(pub["title"]).strip(),
                                                         "venue": str(pub.get("description") or pub.get("venue") or "")}})
    return out


def _signature(kind: str, data: dict) -> tuple:
    key = {"experience": ("employer", "title"), "internship": ("employer", "title"), "education": ("institution", "degree"),
           "project": ("name",), "skill": ("name",), "publication": ("title",), "achievement": ("text",)}.get(kind, ())
    return (kind,) + tuple(str(data.get(k) or "").strip().lower() for k in key)


def store_imported(db, items: list[dict], label: str) -> dict:
    existing = {_signature(f.kind, f.data or {}): f for f in db.query(CandidateFact).all()}
    created = skipped = 0

    def add(item, parent_id=None):
        nonlocal created, skipped
        try:
            data = F.validate_data(item["kind"], item["data"])
        except ValueError as e:
            logger.info(f"profile import: dropped {item['kind']}: {e}")
            skipped += 1
            return None
        sig = _signature(item["kind"], data)
        if sig in existing:
            skipped += 1
            return existing[sig]
        row = CandidateFact(kind=item["kind"], parent_id=parent_id, data=data, verified=False, source=f"import:{label}"[:200])
        db.add(row)
        db.flush()
        existing[sig] = row
        created += 1
        return row

    for item in items:
        parent = add(item)
        for child in item.get("children") or []:
            if parent is not None:
                add(child, parent.id)
    return {"created": created, "skipped": skipped}


def _fill_contact(p: Persona, header: dict) -> int:
    """Contact answers the résumé header gives, only where the Persona has none yet."""
    from backend.api.routes_persona import _contact_from_header
    contact = dict(p.contact or {})
    added = 0
    for k, v in _contact_from_header(header or {}).items():
        if v and not contact.get(k):
            contact[k] = v
            added += 1
    if added:
        p.contact = contact
    return added


@router.post("/import")
async def import_profile(request: Request, file: Optional[UploadFile] = File(None), db: Session = Depends(get_db)):
    """Import a PDF, a base résumé (`resume_id`) or the Persona's résumé content (`{"source": "persona"}`) as unverified facts."""
    p = db.query(Persona).filter(Persona.id == 1).first()
    if file is not None and getattr(file, "filename", None):
        from backend.api.routes_resumes import check_pdf_name, check_pdf_size, parse_resume_pdf
        check_pdf_name(file.filename)
        pdf = await file.read()
        check_pdf_size(pdf)
        json_data, label = await parse_resume_pdf(pdf, db), file.filename
    else:
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        if body.get("source") == "persona":
            json_data, label = (p.resume_content if p else None) or {}, "persona"
        elif body.get("resume_id"):
            r = db.query(Resume).filter(Resume.id == str(body["resume_id"])).first()
            if not r:
                raise HTTPException(404, "Resume not found")
            json_data, label = r.json_data or {}, r.name
        else:
            raise HTTPException(400, "Upload a PDF, or send resume_id or source=persona")

    result = store_imported(db, facts_from_resume_json(json_data), label)
    if p is not None:
        result["contact_filled"] = _fill_contact(p, json_data.get("header") or {})
    db.commit()
    return result
