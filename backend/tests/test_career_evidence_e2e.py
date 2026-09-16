"""The whole path, once: two résumés in, reconciled evidence, role-family bases out.

upload -> extract claims -> reconcile (duplicate / more evidence / conflict) ->
confirm -> role-family base -> LaTeX -> PDF. The pieces are unit-tested elsewhere;
this pins that they still add up.
"""
from backend.copilot import evidence as EV
from backend.copilot import versions as V
from backend.models.db import CandidateFact, FactConflict, Persona, ResumeSource, ResumeVersion

NEWER = {
    "experience": [{"company": "CED Engineering", "title": "Software Engineering Intern", "date": "May 2025 - Aug 2025",
                    "bullets": ["Built a FastAPI service backed by PostgreSQL",
                                "Trained a classification model in PyTorch",
                                "Wrote the stakeholder roadmap and ran the launch review"]}],
    "education": [{"school": "State U", "degree": "B.S. Computer Science", "years": "May 2027"}],
    "skills": {"Languages": "Python, C++", "Libraries": "PyTorch, FastAPI"},
}
# the same internship under another name, one shared bullet, one it alone has, and a different graduation date
OLDER = {
    "experience": [{"company": "CED Engineering, Inc.", "title": "SWE Intern", "date": "May 2025 - Aug 2025",
                    "bullets": ["Built a FastAPI service backed by PostgreSQL", "Cut report turnaround by 30%"]}],
    "education": [{"school": "State U", "degree": "BS Computer Science", "years": "December 2027"}],
}


def _lead_bullet(resume):
    entry = next(e for s in resume["sections"] if s["id"] == "experience" for e in s["entries"])
    return entry["bullets"][0]["text"]


async def test_two_resumes_become_one_body_of_evidence_and_three_role_resumes(test_db):
    test_db.add(Persona(id=1, contact={"first_name": "Dana", "last_name": "Okafor", "email": "dana@example.edu"}))
    newer = ResumeSource(filename="resume-2025.pdf", sha256="a" * 64, status="imported")
    older = ResumeSource(filename="resume-2023.pdf", sha256="b" * 64, status="imported")
    test_db.add_all([newer, older])
    test_db.commit()

    EV.reconcile(test_db, EV.facts_from_resume_json(NEWER), newer)
    EV.reconcile(test_db, EV.facts_from_resume_json(OLDER), older)
    test_db.commit()

    # one canonical internship, whatever each résumé called the employer and the role
    roles = test_db.query(CandidateFact).filter(CandidateFact.kind.in_(["experience", "internship"])).all()
    assert len(roles) == 1 and roles[0].data["employer"] == "CED Engineering"
    bullets = {c.data["text"] for c in test_db.query(CandidateFact).filter(CandidateFact.parent_id == roles[0].id)}
    assert bullets == {"Built a FastAPI service backed by PostgreSQL",
                       "Trained a classification model in PyTorch",
                       "Wrote the stakeholder roadmap and ran the launch review",
                       "Cut report turnaround by 30%"}

    # one degree, and the graduation dates disagree: that is the user's call, not a merge
    education = test_db.query(CandidateFact).filter(CandidateFact.kind == "education").all()
    assert len(education) == 1 and education[0].data["graduation_date"] == "2027-05"
    conflict = test_db.query(FactConflict).one()
    assert (conflict.field, conflict.proposed_value, conflict.status) == ("graduation_date", "2027-12", "open")

    for fact in test_db.query(CandidateFact).all():
        fact.verified = True
    test_db.commit()

    leads = {}
    for family in ("software_engineering", "product", "data_ml"):
        version = test_db.get(ResumeVersion, await V.generate_base(family))
        test_db.refresh(version)
        assert version.kind == "base" and version.role_family == family
        assert version.template == "jakes" and version.candidate_profile_version
        assert version.latex and "\\resumeSubheading" in version.latex
        leads[family] = _lead_bullet(version.resume_json)

    assert "FastAPI" in leads["software_engineering"]
    assert "roadmap" in leads["product"]
    assert "PyTorch" in leads["data_ml"]

    universal = test_db.get(ResumeVersion, await V.generate_base())
    test_db.refresh(universal)
    assert universal.role_family is None

    # a selection, never a rewrite: every family shows the same four facts, in its own order
    for row in test_db.query(ResumeVersion).filter(ResumeVersion.kind == "base").all():
        assert sorted(b["text"] for s in row.resume_json["sections"] if s["id"] == "experience"
                      for e in s["entries"] for b in e["bullets"]) == sorted(bullets)
