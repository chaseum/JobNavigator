"""Résumé pipeline: provenance, unsupported-claim rejection, immutability, LaTeX, Parser Health."""
import shutil
import uuid

import pytest

from backend.copilot import latex, parser_health
from backend.copilot import resume_pipeline as P
from backend.copilot.schemas import ResumePlan


class Fact:
    def __init__(self, id, kind, data, parent_id=None):
        self.id, self.kind, self.data, self.parent_id = id, kind, data, parent_id


def _index():
    return P.FactIndex([
        Fact(1, "internship", {"employer": "CED", "title": "Software Intern", "start_date": "2024-05", "end_date": "2024-08",
                               "responsibilities": ["Built ETL jobs in Python"]}),
        Fact(2, "achievement", {"text": "Cut report generation time by 32%"}, parent_id=1),
        Fact(3, "project", {"name": "EEG pipeline", "technologies": ["Python", "NumPy"], "description": "Signal processing for EEG data"}),
        Fact(4, "project", {"name": "Old site", "end_date": "2019"}),
        Fact(5, "skill", {"name": "Python", "category": "Languages", "evidence_ids": ["internship_1", "project_3"]}),
        Fact(6, "education", {"institution": "State U", "degree": "B.S.", "major": "Computer Science", "graduation_date": "2025-05", "gpa": "3.8"}),
    ])


class Persona:
    contact = {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com", "linkedin": "linkedin.com/in/ada_l"}


# ── provenance and claim checks ──────────────────────────────────────────────

def test_a_bullet_without_sources_or_citing_another_entry_is_unsupported():
    ix = _index()
    allowed = ix.allowed_for("internship_1")
    assert allowed == {"internship_1", "achievement_2", "skill_5"}
    assert P.check_bullet({"text": "Built ETL jobs", "source_fact_ids": []}, allowed, ix)[0]["status"] == "UNSUPPORTED"
    borrowed = P.check_bullet({"text": "Processed EEG signals", "source_fact_ids": ["project_3"]}, allowed, ix)
    assert borrowed[0]["status"] == "UNSUPPORTED" and "outside" in borrowed[0]["reason"]


def test_invented_tools_and_metrics_are_unsupported_real_ones_pass():
    ix = _index()
    allowed = ix.allowed_for("internship_1")
    tech = ["Python", "Kubernetes", "C++"]
    assert P.check_bullet({"text": "Cut report generation time by 32% with Python ETL jobs",
                           "source_fact_ids": ["achievement_2", "internship_1"]}, allowed, ix, tech) == []
    k8s = P.check_bullet({"text": "Deployed ETL jobs on Kubernetes", "source_fact_ids": ["internship_1"]}, allowed, ix, tech)
    assert [i["status"] for i in k8s] == ["UNSUPPORTED"] and "Kubernetes" in k8s[0]["reason"]
    inflated = P.check_bullet({"text": "Cut report time by 45%", "source_fact_ids": ["achievement_2"]}, allowed, ix, tech)
    assert inflated[0]["status"] == "UNSUPPORTED" and "45" in inflated[0]["reason"]
    led = P.check_bullet({"text": "Led the ETL effort in Python", "source_fact_ids": ["internship_1"]}, allowed, ix, tech)
    assert [i["status"] for i in led] == ["AMBIGUOUS"]


async def test_audit_blocks_on_unsupported_and_asks_review_for_ambiguous():
    ix = _index()
    resume = P.build_base(ix, Persona)
    _, entry, bullet = next((s, e, b) for s, e, b in P.iter_bullets(resume) if e["fact_id"] == "internship_1")
    clean = await P.audit_resume(resume, ix, use_llm=False)
    assert clean["counts"]["UNSUPPORTED"] == 0 and not clean["blocked"]

    bullet["text"] = "Orchestrated Kubernetes clusters for 10,000 users"
    dirty = await P.audit_resume(resume, ix, jd_tech=["Kubernetes"], use_llm=False)
    assert dirty["blocked"] and dirty["counts"]["UNSUPPORTED"] == 1


async def test_a_failed_model_audit_means_review_not_pass(monkeypatch):
    ix = _index()
    resume = P.build_resume(ix, Persona, {"internship_1": [{"text": "Wrote Python ETL jobs", "source_fact_ids": ["internship_1"],
                                                           "original": "Built ETL jobs in Python"}]}, [], [])

    async def boom(*a, **kw):
        raise RuntimeError("ollama down")
    monkeypatch.setattr(P, "call_structured", boom)
    audit = await P.audit_resume(resume, ix)
    assert audit["counts"]["AMBIGUOUS"] == 1 and audit["unreviewed"] == ["internship_1:1"]


def test_headings_come_from_facts_and_dates_render_from_them():
    resume = P.build_base(_index(), Persona)
    exp = next(s for s in resume["sections"] if s["id"] == "experience")["entries"][0]
    assert (exp["heading"], exp["subheading"], exp["date"]) == ("CED", "Software Intern", "May 2024 – Aug 2024")
    assert [b["id"] for b in exp["bullets"]] == ["internship_1:1", "internship_1:2"]
    assert exp["bullets"][0]["source_fact_ids"] == ["achievement_2"]


def test_plan_validation_drops_unknown_ids_and_honours_project_policy():
    ix = _index()
    plan = ResumePlan(entries=[
        {"fact_id": "project_4", "bullet_sources": [["project_4"]]},
        {"fact_id": "project_3", "bullet_sources": [["project_3", "internship_1"]]},  # internship_1 is not project_3's
        {"fact_id": "experience_99", "bullet_sources": [["experience_99"]]},
    ], skill_ids=["skill_5", "skill_77"])
    base = ["project_3"]
    groups, projects, skills, notes, planned = P.validate_plan(plan, ix, base, "keep", 4)
    assert projects == ["project_3"] and "project_4" not in groups
    assert groups["project_3"] == [["project_3"]]
    assert "internship_1" in groups and skills == ["skill_5"]
    assert any("experience_99" in n for n in notes)
    assert planned == {"project_3"}, "entries the plan did not choose are kept verbatim, not sent to the model"
    _, replaced, _, _, _ = P.validate_plan(plan, ix, base, "replace", 4)
    assert replaced == ["project_4", "project_3"]


# ── LaTeX and Parser Health ──────────────────────────────────────────────────

def test_latex_escapes_every_value_and_keeps_links():
    ix = _index()
    resume = P.build_base(ix, Persona)
    resume["sections"][1]["entries"][0]["bullets"][0]["text"] = r"Saved $5k & 20% on C# \o/ {builds} ~ ^_^"
    tex = latex.render(resume)
    assert r"Saved \$5k \& 20\% on C\# \textbackslash{}o/ \{builds\} \textasciitilde{} \textasciicircum{}\_\textasciicircum{}" in tex
    assert r"\href{https://linkedin.com/in/ada_l}{linkedin.com/in/ada\_l}" in tex
    assert "(((" not in tex and "((*" not in tex


def test_parser_health_checks_the_extracted_text():
    resume = P.build_base(_index(), Persona)
    import backend.copilot.versions as V
    good = V.resume_plain_text(resume)
    assert parser_health.check(good, resume)["score"] == 100
    broken = parser_health.check(good.replace("CED", "C�D"), resume)
    failed = {c["name"] for c in broken["checks"] if not c["ok"]}
    assert {"Company names extracted", "No garbled characters"} <= failed


@pytest.mark.skipif(shutil.which("pdflatex") is None, reason="no local pdflatex")
async def test_real_compile_extracts_cleanly():
    resume = P.build_base(_index(), Persona)
    result = await latex.compile_pdf(latex.render(resume))
    assert result["ok"], result["log"]
    text = parser_health.extract_text(result["pdf"])
    health = parser_health.check(text, resume)
    assert health["score"] == 100, [c for c in health["checks"] if not c["ok"]]
    assert result["pages"] == 1


async def test_a_latex_error_is_reported_not_papered_over():
    if shutil.which("pdflatex") is None:
        pytest.skip("no local pdflatex")
    result = await latex.compile_pdf(r"\documentclass{article}\begin{document}\undefinedmacro\end{document}")
    assert not result["ok"] and result["pdf"] is None and "Undefined control sequence" in result["log"]


# ── versions: immutability, review and acceptance end to end ─────────────────

def test_accepted_versions_cannot_change_or_be_deleted(test_db):
    from backend.models.db import ImmutableVersionError, ResumeVersion
    v = ResumeVersion(kind="base", status="draft", resume_json={"sections": []})
    test_db.add(v)
    test_db.commit()
    v.resume_json = {"sections": [], "edited": True}
    test_db.commit()                       # a draft may change
    v.status = "accepted"
    test_db.commit()                       # draft -> accepted is the one allowed transition
    test_db.expire_all()
    v.resume_json = {"sections": [], "sneaky": True}
    with pytest.raises(ImmutableVersionError):
        test_db.commit()
    test_db.rollback()
    test_db.delete(v)
    with pytest.raises(ImmutableVersionError):
        test_db.commit()
    test_db.rollback()


async def test_generate_review_accept_flow(api_client, test_db, monkeypatch, tmp_path):
    import backend.copilot.versions as V
    from backend.copilot.schemas import BulletRewrites, ResumeAudit, ResumePlan
    from backend.models.db import CandidateFact, Job, JobAnalysisRecord, Persona as PersonaRow

    monkeypatch.setenv("GENERATED_DIR", str(tmp_path))

    async def fake_compile(tex, template="default", timeout=90):
        return {"ok": True, "pdf": b"%PDF-1.4 fake", "pages": 1, "log": ""}
    monkeypatch.setattr(V.latex, "compile_pdf", fake_compile)
    monkeypatch.setattr(V.parser_health, "extract_text", lambda pdf: "")

    test_db.add(PersonaRow(id=1, contact=Persona.contact))
    intern = CandidateFact(kind="internship", data={"employer": "CED", "title": "Software Intern", "responsibilities": ["Built ETL jobs in Python"]}, verified=True)
    test_db.add(intern)
    test_db.flush()
    test_db.add(CandidateFact(kind="achievement", parent_id=intern.id, data={"text": "Cut report generation time by 32%"}, verified=True))
    job = Job(external_id="j1", company="Acme Corp", title="Data Engineer", description="Python, Kubernetes")
    test_db.add(job)
    test_db.flush()
    ref = f"internship_{intern.id}"
    test_db.add(JobAnalysisRecord(job_id=job.id, analysis={"technologies": ["Python", "Kubernetes"], "requirements": [
        {"id": "r1", "text": "Python", "category": "technology", "required": True, "importance": 3}]},
        evidence=[{"requirement_id": "r1", "status": "MATCHED", "source_fact_ids": [ref]}], match={"score": 80}, profile_version="x"))
    test_db.commit()
    job_id = str(job.id)

    # base: verbatim facts, accepted, written to disk
    base = api_client.post("/api/resume-versions/base")
    assert base.status_code == 201 and base.json()["counts"]["UNSUPPORTED"] == 0
    acc = api_client.post(f"/api/resume-versions/{base.json()['id']}/accept")
    assert acc.status_code == 200, acc.text
    out = tmp_path / "base"
    assert any(p.name == "resume.tex" for p in out.rglob("*")) and any(p.name == "audit.json" for p in out.rglob("*"))
    assert api_client.post(f"/api/resume-versions/{base.json()['id']}/bullets/{ref}:1", json={"action": "edit", "text": "x"}).status_code == 409

    async def fake_structured(schema, prompt, system, **kw):
        if schema is ResumePlan:
            return ResumePlan(entries=[{"fact_id": ref, "bullet_sources": [[ref], [f"achievement_{intern.id + 1}"]]}]), "ollama", "m"
        if schema is BulletRewrites:
            return BulletRewrites(bullets=[
                {"text": "Built Python ETL jobs deployed on Kubernetes", "source_fact_ids": [ref], "requirement_ids": ["r1"], "reason": "JD terms"},
                {"text": "Reduced report generation time by 32%", "source_fact_ids": [f"achievement_{intern.id + 1}"], "reason": "stronger verb"},
            ]), "ollama", "m"
        if schema is ResumeAudit:
            return ResumeAudit(claims=[]), "ollama", "m"
        raise AssertionError(schema)
    monkeypatch.setattr(P, "call_structured", fake_structured)

    vid = await V.generate_tailored(job_id)
    detail = api_client.get(f"/api/resume-versions/{(await _latest_draft(test_db, job_id))}").json()
    by_id = {c["bullet_id"]: c for c in detail["audit"]["claims"]}
    assert by_id[f"{ref}:1"]["status"] == "UNSUPPORTED"
    assert detail["blocked"] and detail["base"] is not None
    assert "blocked" in (detail["compile_log"] or "")
    bullet = next(b for s, e, b in P.iter_bullets(detail["resume"]) if b["id"] == f"{ref}:1")
    assert bullet["requirement_ids"] == ["r1"] and bullet["original"] == "Built ETL jobs in Python"
    v_id = detail["id"]

    r = api_client.post(f"/api/resume-versions/{v_id}/accept", json={"all": True})
    assert r.status_code == 409 and "unsupported" in r.json()["detail"]
    assert api_client.post(f"/api/resume-versions/{v_id}/bullets/{ref}:1", json={"action": "accept"}).status_code == 409

    fixed = api_client.post(f"/api/resume-versions/{v_id}/bullets/{ref}:1", json={"action": "reject"}).json()
    assert fixed["bullet"]["text"] == "Built ETL jobs in Python" and fixed["claim"]["status"] == "SUPPORTED"
    assert api_client.post(f"/api/resume-versions/{v_id}/accept", json={"all": True}).status_code == 200
    listed = api_client.get("/api/resume-versions", params={"job_id": job_id, "status": "accepted"}).json()
    assert len(listed) == 1 and listed[0]["company"] == "Acme Corp" and listed[0]["match_score"] == 80
    assert any((tmp_path / "acme-corp" / "data-engineer").rglob("job-analysis.json"))
    assert api_client.get(f"/api/resume-versions/{v_id}/export.zip").content[:2] == b"PK"


async def _latest_draft(db, job_id):
    from backend.models.db import ResumeVersion
    db.expire_all()
    return str(db.query(ResumeVersion).filter(ResumeVersion.job_id == job_id).order_by(ResumeVersion.created_at.desc()).first().id)
