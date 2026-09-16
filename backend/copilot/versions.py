"""Résumé versions: generation, review, rebuild, acceptance and the files an accepted version leaves behind."""
import copy
import io
import json
import logging
import os
import re
import zipfile
from pathlib import Path

from sqlalchemy.orm.attributes import flag_modified

from backend.copilot import facts as F
from backend.copilot import latex, parser_health
from backend.copilot import resume_pipeline as P
from backend.copilot import role_families as RF
from backend.models.db import Job, JobAnalysisRecord, Persona, ResumeVersion, SessionLocal, Setting, utcnow

logger = logging.getLogger("jobnavigator.copilot.versions")


def generated_dir() -> Path:
    return Path(os.getenv("GENERATED_DIR") or Path(__file__).resolve().parents[2] / "generated")


def resume_settings(db) -> dict:
    def get(key, default):
        row = db.query(Setting).filter(Setting.key == key).first()
        return row.value if row and row.value not in (None, "") else default
    try:
        order = json.loads(get("resume_section_order", "[]")) or P.DEFAULT_SECTION_ORDER
    except ValueError:
        order = P.DEFAULT_SECTION_ORDER
    template = get("resume_template", latex.DEFAULT_TEMPLATE)
    return {"template": template if template in latex.template_names() else latex.DEFAULT_TEMPLATE,
            "page_target": int(get("resume_page_target", "1") or 1),
            "project_policy": get("resume_project_policy", "reorder"),
            "skill_ordering": get("resume_skill_ordering", "relevance"),
            "section_order": [s for s in order if s in P.SECTION_TITLES]}


def resume_refs(resume: dict) -> set:
    refs = set()
    for s in resume.get("sections") or []:
        for e in s.get("entries") or []:
            refs.add(e.get("fact_id"))
            for b in e.get("bullets") or []:
                refs.update(b.get("source_fact_ids") or [])
        for line in s.get("lines") or []:
            refs.update(line.get("source_fact_ids") or [])
    refs.discard(None)
    return refs


def resume_plain_text(resume: dict) -> str:
    header = resume.get("header") or {}
    parts = [header.get("name", "")] + [c.get("text", "") for c in header.get("contact") or []]
    for s in resume.get("sections") or []:
        parts.append(s["title"])
        for e in s.get("entries") or []:
            parts += [e.get("heading", ""), e.get("subheading", ""), e.get("date", "")] + [b["text"] for b in e.get("bullets") or []]
        for line in s.get("lines") or []:
            parts.append(f"{line['label']}: {', '.join(line['items'])}")
    return "\n".join(p for p in parts if p)


def latest_accepted(db, job_id=None, kind=None):
    q = db.query(ResumeVersion).filter(ResumeVersion.status == "accepted")
    if job_id is not None:
        q = q.filter(ResumeVersion.job_id == job_id)
    if kind:
        q = q.filter(ResumeVersion.kind == kind)
    return q.order_by(ResumeVersion.accepted_at.desc()).first()


def _index(db):
    return P.FactIndex(F.load_verified(db)), db.query(Persona).filter(Persona.id == 1).first()


# ── generation ───────────────────────────────────────────────────────────────

async def generate_base(role_family: str | None = None) -> str:
    """A base résumé from the verified Career Evidence, optionally as one role family's selection.

    Reproducible and versioned like any other résumé version: the row records the
    profile version and template version it was built from, so regenerating after
    the evidence changes leaves the old base intact.
    """
    from backend.copilot import role_families as RF
    db = SessionLocal()
    try:
        ix, persona = _index(db)
        if not ix.facts:
            raise ValueError("verify some profile facts first")
        family = None
        if role_family:
            family = RF.get(role_family, db)
            if family is None:
                raise ValueError(f"unknown role family {role_family!r}")
        st = resume_settings(db)
        resume = P.build_base(ix, persona, st["section_order"], st["template"], family)
        audit = await P.audit_resume(resume, ix, use_llm=False)
        v = ResumeVersion(kind="base", status="draft", template=st["template"], template_version=latex.template_version(st["template"]),
                          candidate_profile_version=F.profile_version(db), resume_json=resume, audit=audit,
                          role_family=family["id"] if family else None)
        db.add(v)
        db.commit()
        vid = str(v.id)
    finally:
        db.close()
    await rebuild(vid)
    return vid


def ensure_role_family(db, rec) -> str:
    """The job's role family, classified once and then left alone so a user override sticks."""
    if rec.role_family:
        return rec.role_family
    verdict = RF.classify(rec.analysis or {}, db)
    rec.role_family, rec.role_family_source = verdict["family"] or None, "auto"
    rec.role_family_reason = f"{verdict['reason']} (confidence {verdict['confidence']})"
    db.commit()
    return rec.role_family


async def generate_tailored(job_id: str) -> str:
    """Audit the role-family base résumé, tailor toward its SAFE_TO_ADD / SAFE_TO_REPHRASE gaps with verified facts only, re-audit the draft."""
    from backend.copilot import applicability as AP
    from backend.copilot import resume_audits as RA
    from backend.copilot.analysis import evidence_context, latest_record, weights
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        rec = latest_record(db, job_id)
        if job is None or rec is None or rec.evidence is None:
            raise RuntimeError("analyze this job before generating a résumé")
        ix, persona = _index(db)
        st = resume_settings(db)
        snapshot = {"analysis_id": rec.id, "analysis": rec.analysis, "evidence": rec.evidence, "match": rec.match,
                    "profile_version": rec.profile_version}
        # verified evidence -> role-family base -> this JD -> tailored draft. The
        # family decides which base selection the draft starts from; it is a label
        # for that choice and never an input to Candidate Fit.
        family_id = ensure_role_family(db, rec)
        family = RF.get(family_id, db) if family_id else None
        base = RA.base_version(db, family_id)
        base_id, label = (base.id if base else None), f"{job.company} — {job.title}"
        version = F.profile_version(db)
        _, facts_by_ref, _ = evidence_context(db)
        if base is not None:
            base_resume = base.resume_json or {}
            base_audit = RA.compute(db, rec, base, facts_by_ref)
        else:   # no base version yet: measure against what this family's base would show
            base_resume = P.build_base(ix, persona, st["section_order"], st["template"], family)
            base_audit = AP.audit(rec.analysis.get("requirements") or [], rec.evidence, base_resume,
                                  facts_by_ref, None, weights(db), rec.analysis.get("technologies") or [])
    finally:
        db.close()

    gaps = [r for r in base_audit["rows"] if r.get("gap") in ("SAFE_TO_ADD", "SAFE_TO_REPHRASE")]
    resume, audit, provider, model = await P.tailor(ix, persona, snapshot["analysis"], snapshot["evidence"], st, job_id,
                                                    gaps=gaps, base_resume=base_resume, role_family=family_id)
    snapshot["applicability"] = {"base_version_id": str(base_id) if base_id else None, "base_score": base_audit["score"],
                                 "maximum": base_audit["maximum"], "gaps_targeted": [g["requirement_id"] for g in gaps],
                                 "role_family": family_id}

    db = SessionLocal()
    try:
        v = ResumeVersion(job_id=job_id, kind="tailored", status="draft", template=st["template"],
                          template_version=latex.template_version(st["template"]), candidate_profile_version=version,
                          job_analysis_id=snapshot["analysis_id"], job_analysis=snapshot, base_version_id=base_id,
                          role_family=family_id, provider=provider, model=model, resume_json=resume, audit=audit)
        db.add(v)
        db.commit()
        vid = str(v.id)
    finally:
        db.close()
    await rebuild(vid)
    stored = RA.audit_job(job_id, [x for x in (base_id, vid) if x])
    after = stored.get(vid) or {}
    c = audit["counts"]
    return (f"Drafted résumé for {label}: Resume Applicability {base_audit['score']} → {after.get('score')} "
            f"(max {base_audit['maximum']}); {c['UNSUPPORTED']} unsupported, {c['AMBIGUOUS']} to review")


async def rebuild(version_id: str) -> None:
    """Render LaTeX and compile, unless an unsupported claim blocks the PDF. Session is not held across the compile."""
    db = SessionLocal()
    try:
        v = db.get(ResumeVersion, version_id)
        resume, template, blocked, target = copy.deepcopy(v.resume_json), v.template, (v.audit or {}).get("blocked"), resume_settings(db)["page_target"]
    finally:
        db.close()
    tex = latex.render(resume, template)
    result = {"ok": False, "pdf": None, "pages": None,
              "log": "PDF blocked: remove or edit the unsupported claims first."} if blocked else await latex.compile_pdf(tex, template)
    health = None
    if result["ok"]:
        health = parser_health.check(parser_health.extract_text(result["pdf"]), resume)
        fits = result["pages"] <= target
        health["checks"].append({"name": f"Fits the {target}-page target", "ok": fits,
                                 "detail": "" if fits else f"{result['pages']} pages"})
    db = SessionLocal()
    try:
        v = db.get(ResumeVersion, version_id)
        v.latex, v.pdf, v.pages, v.compile_log, v.parser_health = tex, result["pdf"], result["pages"], result["log"], health
        audit = dict(v.audit or {})
        audit["pdf_stale"] = False
        v.audit = audit
        flag_modified(v, "audit")
        db.commit()
    finally:
        db.close()


# ── review ───────────────────────────────────────────────────────────────────

class ReviewError(ValueError):
    pass


def _draft(db, version_id) -> ResumeVersion:
    v = db.get(ResumeVersion, version_id)
    if v is None:
        raise LookupError("résumé version not found")
    if v.status != "draft":
        raise ReviewError(f"this version is {v.status}; generate a new one to change it")
    return v


def _find(resume, bullet_id):
    for s, e, b in P.iter_bullets(resume):
        if b["id"] == bullet_id:
            return e, b
    raise LookupError("bullet not found")


def _save_review(v, resume, claims):
    audit = {**P.summarize_audit(claims), "pdf_stale": True}
    v.resume_json, v.audit = resume, audit
    flag_modified(v, "resume_json")
    flag_modified(v, "audit")


def review_bullet(version_id: str, bullet_id: str, action: str, text: str | None = None) -> dict:
    """accept (ambiguous, reviewed) · reject (back to the fact's own words) · edit (your text, checked again)."""
    db = SessionLocal()
    try:
        v = _draft(db, version_id)
        resume = copy.deepcopy(v.resume_json)
        claims = {c["bullet_id"]: dict(c) for c in (v.audit or {}).get("claims") or []}
        entry, bullet = _find(resume, bullet_id)
        claim = claims.setdefault(bullet_id, {"bullet_id": bullet_id, "status": "SUPPORTED", "reasons": [], "reviewed": False})
        ix, _ = _index(db)
        if action == "accept":
            if claim["status"] == "UNSUPPORTED":
                raise ReviewError("an unsupported claim cannot be accepted; reject it or edit it")
            claim["reviewed"] = True
        elif action in ("reject", "edit"):
            if action == "reject":
                bullet["text"] = bullet.get("original") or bullet["text"]
                bullet["source_fact_ids"] = bullet.get("planned_sources") or bullet["source_fact_ids"]
                bullet["reason"] = "change rejected; original wording restored"
            else:
                clean = " ".join((text or "").split())
                if not clean:
                    raise ReviewError("edited text is empty")
                bullet["text"], bullet["reason"], bullet["edited"] = clean, "edited by you", True
            analysis = (v.job_analysis or {}).get("analysis") or {}
            issues = P.check_bullet(bullet, ix.allowed_for(entry["fact_id"]), ix,
                                    analysis.get("technologies") or [], analysis.get("domain_terms") or [])
            claim.update(status=P.worst(i["status"] for i in issues), reasons=[i["reason"] for i in issues],
                         reviewed=not issues or all(i["status"] != "UNSUPPORTED" for i in issues))
        else:
            raise ReviewError(f"unknown action {action!r}")
        _save_review(v, resume, claims)
        db.commit()
        return {"bullet": bullet, "claim": claim, "audit": v.audit}
    finally:
        db.close()


async def regenerate_bullet(version_id: str, bullet_id: str) -> dict:
    db = SessionLocal()
    try:
        v = _draft(db, version_id)
        entry, bullet = _find(v.resume_json, bullet_id)
        ix, _ = _index(db)
        analysis = (v.job_analysis or {}).get("analysis") or {}
        evidence = {e["requirement_id"]: e for e in (v.job_analysis or {}).get("evidence") or []}
        group = bullet.get("planned_sources") or bullet["source_fact_ids"]
        job_id = str(v.job_id) if v.job_id else None
    finally:
        db.close()
    group = [s for s in group if s in ix.allowed_for(entry["fact_id"])]
    if not group:
        raise ReviewError("this bullet's sources are no longer verified facts")
    new, _, _ = await P.rewrite_entry(ix, entry["fact_id"], [group], analysis.get("requirements") or [], evidence, job_id)
    fresh = {"id": bullet_id, **new[0]}
    one = {"template": "default", "header": {}, "sections": [{"id": "x", "title": "x", "entries": [{**entry, "bullets": [fresh]}]}]}
    verdict = (await P.audit_resume(one, ix, analysis.get("technologies") or [], analysis.get("domain_terms") or [], True, job_id))["claims"][0]

    db = SessionLocal()
    try:
        v = _draft(db, version_id)
        resume = copy.deepcopy(v.resume_json)
        _, target = _find(resume, bullet_id)
        target.clear()
        target.update(fresh)
        claims = {c["bullet_id"]: dict(c) for c in (v.audit or {}).get("claims") or []}
        claims[bullet_id] = verdict
        _save_review(v, resume, claims)
        db.commit()
        return {"bullet": fresh, "claim": verdict, "audit": v.audit}
    finally:
        db.close()


def reject_version(version_id: str) -> None:
    db = SessionLocal()
    try:
        v = _draft(db, version_id)
        v.status = "rejected"
        db.commit()
    finally:
        db.close()


async def accept_version(version_id: str, accept_all: bool = False) -> dict:
    """Accept a draft: nothing unsupported, every ambiguous claim reviewed, a PDF that compiled. Then it is frozen."""
    db = SessionLocal()
    try:
        v = _draft(db, version_id)
        claims = {c["bullet_id"]: dict(c) for c in (v.audit or {}).get("claims") or []}
        if accept_all:
            for c in claims.values():
                if c["status"] == "AMBIGUOUS":
                    c["reviewed"] = True
            _save_review(v, copy.deepcopy(v.resume_json), claims)
            db.commit()
        audit = P.summarize_audit(claims)
        stale = (v.audit or {}).get("pdf_stale") or v.pdf is None
    finally:
        db.close()
    if audit["blocked"]:
        raise ReviewError(f"{audit['counts']['UNSUPPORTED']} unsupported claim(s) must be rejected or edited first")
    if audit["unreviewed"]:
        raise ReviewError(f"{len(audit['unreviewed'])} ambiguous claim(s) still need your review")
    if stale:
        await rebuild(version_id)

    db = SessionLocal()
    try:
        v = _draft(db, version_id)
        if v.pdf is None:
            raise ReviewError("the PDF did not compile: " + (v.compile_log or "unknown error")[:600])
        job = db.query(Job).filter(Job.id == v.job_id).first() if v.job_id else None
        v.output_dir = str(write_outputs(v, job))
        v.status, v.accepted_at = "accepted", utcnow()
        db.commit()
        return {"id": str(v.id), "status": v.status, "output_dir": v.output_dir}
    finally:
        db.close()


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60] or "untitled"


def write_outputs(v: ResumeVersion, job) -> Path:
    """generated/<company>/<role>/<date>-<id>/ — a new folder per version, so nothing accepted is ever overwritten."""
    stamp = f"{(v.created_at or utcnow()):%Y%m%d}-{str(v.id)[:8]}"
    out = generated_dir() / ("base" if job is None else Path(_slug(job.company)) / _slug(job.title)) / stamp
    out.mkdir(parents=True, exist_ok=False)
    for name, content in export_files(v).items():
        (out / name).write_bytes(content)
    (out / "resume.json").write_text(json.dumps(v.resume_json, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "audit.json").write_text(json.dumps(v.audit, indent=2, ensure_ascii=False), encoding="utf-8")
    if v.job_analysis:
        (out / "job-analysis.json").write_text(json.dumps(v.job_analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def export_files(v: ResumeVersion) -> dict:
    """The Overleaf-compatible project: resume.tex, the template's support files, and the PDF when there is one."""
    files = {"resume.tex": (v.latex or latex.render(v.resume_json, v.template)).encode("utf-8")}
    for f in latex.template_dir(v.template).iterdir():
        if f.is_file() and f.suffix != ".j2":
            files[f.name] = f.read_bytes()
    if v.pdf:
        files["resume.pdf"] = v.pdf
    return files


def export_zip(v: ResumeVersion) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in export_files(v).items():
            z.writestr(name, content)
    return buf.getvalue()
