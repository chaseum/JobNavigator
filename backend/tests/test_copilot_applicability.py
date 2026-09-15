"""Candidate Fit vs Resume Applicability: the citation contract, deterministic proof, the truthful ceiling, legacy isolation.

The degree and enrollment wording is taken from real postings (an early-career and
a 2027-graduate role) as regression examples; nothing in the code knows them.
"""
from datetime import date

from backend.copilot import applicability as AP
from backend.copilot import facts as F
from backend.copilot import matching as M
from backend.copilot import parser_health
from backend.copilot import resume_pipeline as P
from backend.copilot import rules as R
from backend.tests.r4_support import client  # noqa: F401 — fixture

TODAY = date(2026, 9, 15)
DEGREE_REQ = ("Bachelor’s or Master’s degree in Computer Science, Engineering, or a related field "
              "(or equivalent practical experience), graduating in 2026 or 2027")
ENROLLED_REQ = ("Currently enrolled full time in a Bachelors or Masters degree program in Computer Science or related "
                "technical field and are graduating by August 2027")


class Fact:
    def __init__(self, id, kind, data, parent_id=None):
        self.id, self.kind, self.data, self.parent_id = id, kind, data, parent_id


def by_ref(facts):
    ids = {f.id: f for f in facts}
    return {F.fact_ref(f): {"kind": f.kind, "data": f.data,
                            "parent": F.fact_ref(ids[f.parent_id]) if f.parent_id in ids else None} for f in facts}


def edu(**kw):
    return Fact(10, "education", {"institution": "State University", "degree": "Bachelor of Science", "major": "Computer Science",
                                  "start_date": "2024-08", "graduation_date": "2027-05", "coursework": ["Database Systems"], **kw})


def req(rid, text, category, required=True, importance=2):
    return {"id": rid, "text": text, "category": category, "required": required, "importance": importance, "source_quote": text}


def one(requirement, matches, facts, extra_tech=()):
    return M.sanitize_evidence([requirement], matches, by_ref(facts), {}, extra_tech, TODAY)[0]


# ── A. the citation contract ─────────────────────────────────────────────────

def test_citations_normalize_to_one_canonical_id_and_fabrications_are_dropped():
    assert F.normalize_ref("[education_10]") == "education_10"
    assert F.normalize_ref("  education_10 ") == "education_10"
    assert F.normalize_ref("[[education_10]]") is None, "exactly one pair of brackets, nothing else repaired"
    assert F.normalize_ref("education 10") is None and F.normalize_ref("Education") is None

    facts = [edu()]
    r = req("r1", "Built a compiler", "experience")
    bracketed = one(r, [{"requirement_id": "r1", "status": "MATCHED", "source_fact_ids": ["[education_10]"], "explanation": "x"}], facts)
    assert bracketed["status"] == "MATCHED" and bracketed["source_fact_ids"] == ["education_10"]
    assert bracketed["original_source_fact_ids"] == ["[education_10]"] and bracketed["normalized_source_fact_ids"] == ["education_10"]
    plain = one(r, [{"requirement_id": "r1", "status": "MATCHED", "source_fact_ids": ["education_10"], "explanation": "x"}], facts)
    assert plain["status"] == "MATCHED" and plain["dropped_source_fact_ids"] == []
    fake = one(r, [{"requirement_id": "r1", "status": "MATCHED", "source_fact_ids": ["[fake_999]"], "explanation": "x"}], facts)
    assert fake["status"] == "MISSING" and fake["source_fact_ids"] == [] and fake["dropped_source_fact_ids"] == ["[fake_999]"]


# ── B. deterministic proof beats the model ───────────────────────────────────

def test_a_verified_cs_degree_matches_the_degree_requirement_even_when_the_model_says_missing():
    row = one(req("r1", DEGREE_REQ, "education"), [{"requirement_id": "r1", "status": "MISSING", "source_fact_ids": [], "explanation": "none"}], [edu()])
    assert row["status"] == "MATCHED" and row["source_fact_ids"] == ["education_10"]
    assert row["llm_status"] == "MISSING" and row["rules"] == ["education"]
    assert "within the window" in row["explanation"]
    # and with no model answer at all
    assert one(req("r1", DEGREE_REQ, "education"), [], [edu()])["status"] == "MATCHED"


def test_degree_levels_and_fields_normalize_conservatively():
    for text, level in (("B.S.", "bachelor"), ("BS", "bachelor"), ("BSc", "bachelor"), ("Bachelor's", "bachelor"),
                        ("Master of Science", "master"), ("M.S.", "master"), ("MSc", "master"), ("Ph.D.", "doctorate"), ("PhD", "doctorate")):
        assert R.degree_level(text)[0] == level, text
    assert R.degree_fields(" Bachelor of Science, Computer Science ") == {"computer science"}
    assert R.degree_fields("CS") == {"computer science"} and R.degree_fields("Biology") == set()
    # the level and major in one degree string, no separate major
    combined = Fact(10, "education", {"institution": "State University", "degree": "Bachelor of Science, Computer Science", "graduation_date": "2027-05"})
    assert one(req("r1", DEGREE_REQ, "education"), [], [combined])["status"] == "MATCHED"
    # an unrelated degree is not CS: the model cannot turn it into a match
    bio = Fact(10, "education", {"institution": "State University", "degree": "Bachelor of Science", "major": "Biology", "graduation_date": "2027-05"})
    capped = one(req("r1", DEGREE_REQ, "education"), [{"requirement_id": "r1", "status": "MATCHED", "source_fact_ids": ["education_10"], "explanation": "degree"}], [bio])
    assert capped["status"] == "PARTIAL"


def test_a_future_graduation_supports_enrollment_but_never_proves_full_time():
    row = one(req("r1", ENROLLED_REQ, "education"), [{"requirement_id": "r1", "status": "MATCHED", "source_fact_ids": ["education_10"], "explanation": "yes"}], [edu()])
    assert row["status"] == "PARTIAL" and row["source_fact_ids"] == ["education_10"]
    assert "implies current enrollment" in row["explanation"] and "full-time enrollment is not stated" in row["explanation"]
    late = one(req("r1", ENROLLED_REQ.replace("full time ", ""), "education"), [], [edu(graduation_date="2028-05")])
    assert late["status"] == "PARTIAL" and "outside the window" in late["explanation"]
    stated = one(req("r1", ENROLLED_REQ, "education"), [], [edu(honors=["Full-time student"])])
    assert stated["status"] == "MATCHED"


def test_technology_aliases_are_safe_and_similar_names_stay_apart():
    facts = [Fact(1, "project", {"name": "Tracker", "description": "Node service on Postgres with a React front end, JS tooling"}),
             Fact(2, "skill", {"name": "Go"})]
    tech = ["PostgreSQL", "JavaScript", "Java", "React", "React Native", "Go"]
    assert one(req("r1", "Experience with PostgreSQL", "technology"), [], facts, tech)["status"] == "MATCHED"
    assert one(req("r1", "Experience with JavaScript", "technology"), [], facts, tech)["status"] == "MATCHED"
    assert one(req("r1", "Experience with Java", "technology"), [], facts, tech)["status"] == "UNKNOWN", "Java is not JavaScript"
    assert one(req("r1", "Experience with React Native", "technology"), [], facts, tech)["status"] == "UNKNOWN", "React is not React Native"
    listed = one(req("r1", "Experience with Go", "technology"), [], facts, tech)
    assert listed["status"] == "PARTIAL" and "only listed" in listed["explanation"], "a bare skill is weaker than use"


def test_any_listed_way_of_building_software_is_direct_evidence():
    facts = [Fact(1, "project", {"name": "Tracker"}), Fact(6, "experience", {"employer": "Acme", "title": "Software Engineering Intern"})]
    text = "Experience building software through internships, personal projects, open source contributions, hackathons, or coursework"
    row = one(req("r4", text, "experience"), [{"requirement_id": "r4", "status": "MISSING", "source_fact_ids": [], "explanation": "none"}], facts)
    assert row["status"] == "MATCHED" and set(row["source_fact_ids"]) == {"project_1", "experience_6"}
    single = one(req("r9", "Previous internship experience at a technology company", "experience"), [], facts)
    assert single["status"] == "UNKNOWN", "one avenue with a qualifier is left to the model"


def test_years_are_only_counted_from_dated_evidence_with_overlaps_unioned():
    r = req("r1", "3+ years of Python", "experience")
    overlapping = [Fact(1, "experience", {"employer": "A", "title": "Dev", "start_date": "2022-01", "end_date": "2023-06", "technologies": ["Python"]}),
                   Fact(2, "project", {"name": "B", "start_date": "2023-01", "end_date": "2024-01", "description": "Python tooling"})]
    row = one(r, [{"requirement_id": "r1", "status": "MATCHED", "source_fact_ids": ["experience_1"], "explanation": "yes"}], overlapping)
    assert row["status"] == "PARTIAL" and "24 months" in row["explanation"], "18 + 12 months overlapping is 24, not 30, and never 3 years"
    long = overlapping + [Fact(3, "project", {"name": "C", "start_date": "2024-01", "end_date": "2025-06", "description": "Python"})]
    assert one(r, [], long)["status"] == "MATCHED"
    undated = [Fact(1, "project", {"name": "B", "description": "Python tooling"})]
    row = one(r, [], undated, ["Python"])
    assert row["status"] == "PARTIAL" and "no dates" in row["explanation"]
    assert one(r, [], [Fact(1, "project", {"name": "B", "description": "Rust"})], ["Python"])["status"] == "UNKNOWN"


# ── C–F. résumé audit against the profile ────────────────────────────────────

class Persona:
    contact = {"first_name": "Sam", "last_name": "Rivera", "email": "sam@example.com"}


def _world():
    facts = [edu(),
             Fact(6, "internship", {"employer": "Acme", "title": "Software Engineering Intern", "start_date": "2026-05", "end_date": "2026-08"}),
             Fact(7, "achievement", {"text": "Wrote the release notes for each sprint"}, parent_id=6),
             Fact(9, "achievement", {"text": "Built a React and TypeScript dashboard for queue metrics"}, parent_id=6)]
    reqs = [req("r1", DEGREE_REQ, "education"),
            req("r2", "Experience with React and TypeScript", "technology"),
            req("r3", "Experience with Kubernetes", "technology", required=False, importance=1)]
    tech = ["React", "TypeScript", "Kubernetes"]
    evidence = M.sanitize_evidence(reqs, [{"requirement_id": "r3", "status": "MISSING", "source_fact_ids": [], "explanation": "none"}],
                                   by_ref(facts), {}, tech, TODAY)
    return P.FactIndex(facts), by_ref(facts), reqs, tech, evidence


def _resume(ix, groups):
    bullets = {ref: [{"text": P.original_text(ix, g), "source_fact_ids": g} for g in gs] for ref, gs in groups.items()}
    return P.build_resume(ix, Persona, bullets, [], [])


def test_profile_supported_but_absent_from_the_resume_is_omitted_supported_not_missing():
    ix, fbr, reqs, tech, evidence = _world()
    assert [e["status"] for e in evidence] == ["MATCHED", "MATCHED", "MISSING"]
    base = AP.audit(reqs, evidence, _resume(ix, {"internship_6": [["achievement_7"]]}), fbr, None, None, tech, TODAY)
    rows = {r["requirement_id"]: r for r in base["rows"]}
    assert rows["r1"]["resume_status"] == "PRESENT" and rows["r1"]["resume_evidence"] == ["education_10"]
    assert rows["r2"]["candidate_status"] == "MATCHED" and rows["r2"]["resume_status"] == "OMITTED_SUPPORTED"
    assert rows["r2"]["gap"] == "SAFE_TO_ADD" and "achievement_9" in rows["r2"]["reason"]
    assert rows["r3"]["resume_status"] == "UNSUPPORTED" and rows["r3"]["gap"] == "CANNOT_CLAIM" and rows["r3"]["question"]
    # weights: r1 3×2 = 6, r2 3×2 = 6, r3 1×1 = 1
    assert base["score"] == round(6 / 13 * 100) and base["maximum"] == round(12 / 13 * 100)
    assert base["maximum"] == M.score(reqs, evidence)["score"], "the ceiling is exactly Candidate Fit"
    assert base["target_reachable"] and "toward" in base["message"]
    assert "not a score reported by any employer" in base["disclaimer"]


async def test_tailoring_surfaces_safe_to_add_evidence_and_the_re_audit_shows_why_it_rose():
    ix, fbr, reqs, tech, evidence = _world()
    base_resume = _resume(ix, {"internship_6": [["achievement_7"]]})
    before = AP.audit(reqs, evidence, base_resume, fbr, None, None, tech, TODAY)
    gaps = [r for r in before["rows"] if r["gap"] in ("SAFE_TO_ADD", "SAFE_TO_REPHRASE")]

    groups, projects = {"internship_6": [["achievement_7"]]}, []
    notes = P.surface_gaps(ix, groups, projects, gaps, evidence, "reorder", 4)
    assert groups["internship_6"] == [["achievement_7"], ["achievement_9"]] and "added achievement_9" in notes[0]
    tailored = _resume(ix, groups)
    claims = {c["bullet_id"]: c for c in (await P.audit_resume(tailored, ix, tech, [], use_llm=False))["claims"]}
    after = AP.audit(reqs, evidence, tailored, fbr, claims, None, tech, TODAY)
    assert after["score"] > before["score"] and after["score"] == after["maximum"]
    diff = AP.compare(before, after)
    assert diff["delta"] == after["score"] - before["score"]
    assert diff["changes"] == [{"requirement_id": "r2", "requirement": reqs[1]["text"], "from": "OMITTED_SUPPORTED", "to": "PRESENT",
                                "points": round(6 / 13 * 100, 1), "caused_by": ["achievement_9"], "lost": []}]
    back = AP.compare(after, before)["changes"][0]
    assert back["points"] < 0 and back["caused_by"] == [] and back["lost"] == ["achievement_9"]


async def test_an_unsupported_claim_earns_nothing_and_cannot_fabricate_a_requirement():
    ix, fbr, reqs, tech, evidence = _world()
    before = AP.audit(reqs, evidence, _resume(ix, {"internship_6": [["achievement_7"]]}), fbr, None, None, tech, TODAY)
    forged = P.build_resume(ix, Persona, {"internship_6": [
        {"text": "Built a React and TypeScript dashboard deployed on Kubernetes", "source_fact_ids": ["achievement_9"]}]}, [], [])
    audit = await P.audit_resume(forged, ix, tech, [], use_llm=False)
    assert audit["blocked"]
    claims = {c["bullet_id"]: c for c in audit["claims"]}
    after = AP.audit(reqs, evidence, forged, fbr, claims, None, tech, TODAY)
    rows = {r["requirement_id"]: r for r in after["rows"]}
    assert after["excluded_bullets"] == ["internship_6:1"]
    assert rows["r3"]["resume_status"] == "UNSUPPORTED", "Kubernetes on the page is not Kubernetes in the profile"
    assert rows["r2"]["resume_status"] == "OMITTED_SUPPORTED", "the failing bullet contributes nothing, not even its true part"
    assert after["score"] <= before["score"]


def test_the_ceiling_is_below_target_when_the_profile_cannot_support_it():
    ix, fbr, reqs, tech, _ = _world()
    reqs[2] = req("r3", "Experience with Kubernetes", "technology")        # now required
    evidence = M.sanitize_evidence(reqs, [{"requirement_id": "r3", "status": "MISSING", "source_fact_ids": [], "explanation": "none"}],
                                   fbr, {}, tech, TODAY)
    full = _resume(ix, {"internship_6": [["achievement_7"], ["achievement_9"]]})
    result = AP.audit(reqs, evidence, full, fbr, None, None, tech, TODAY)
    assert result["score"] == result["maximum"] == round(12 / 18 * 100) and result["maximum"] < AP.TARGET
    assert not result["target_reachable"] and "not currently achievable" in result["message"]


# ── Parser Health stays separate ─────────────────────────────────────────────

def test_parser_health_fails_a_blank_extraction_and_checks_contact_and_dates():
    ix, *_ = _world()
    resume = _resume(ix, {"internship_6": [["achievement_9"]]})
    blank = {c["name"]: c["ok"] for c in parser_health.check("", resume)["checks"]}
    assert not blank["Text extracted"] and not blank["Contact details extracted"] and not blank["Dates extracted"]
    from backend.copilot.versions import resume_plain_text
    text = resume_plain_text(resume)
    assert parser_health.check(text, resume)["score"] == 100
    # a hyphenated word broken across lines is still in order; a line whose spaces vanished is a named failure, not "order"
    broken = text.replace("React and TypeScript", "React and Type-\nScript")
    assert parser_health.check(broken, resume)["score"] == 100
    squashed = {c["name"]: c for c in parser_health.check(text.replace("Built a React and TypeScript", "BuiltaReactandTypeScript"), resume)["checks"]}
    assert squashed["Bullet order preserved"]["ok"] and not squashed["Words separated"]["ok"]


# ── J. legacy scores never compete with Candidate Fit ────────────────────────

def test_the_feed_shows_candidate_fit_not_the_max_of_a_legacy_score(client, test_db, monkeypatch):
    import hashlib
    import backend.api.routes_jobs as routes_jobs
    from backend.models.db import JobAnalysisRecord
    from backend.tests.r4_support import make_job

    # JD-quality gating hides any score for a thin posting; it is not what this test is about
    monkeypatch.setattr(routes_jobs, "_job_description_is_valid", lambda job: True)

    job = make_job(test_db, title="Fit")
    job.description = """Software Engineer builds reliable services for customers and product teams.
        Responsibilities include designing and operating systems with Python and Kubernetes.
        Qualifications include collaborating across teams, reviewing code, and documenting decisions.
        This role supports cloud infrastructure, APIs, data pipelines, and application reliability.
        Engineers work with customers to understand needs and improve the platform over time."""
    job.cv_scores, job.best_cv_score, job.best_cv = {"Legacy CV": 95}, 95.0, "Legacy CV"
    test_db.commit()
    test_db.add(JobAnalysisRecord(job_id=job.id, jd_hash=hashlib.sha256(job.description.encode()).hexdigest(),
                                  analysis={"requirements": []}, evidence=[],
                                  match={"score": 72, "counts": {}, "hard_blockers": [], "formula": M.FORMULA}))
    test_db.commit()
    fit = next(r for r in client.get("/api/jobs?sort_by=match").json()["jobs"] if r["title"] == "Fit")
    assert fit["role_match"]["score"] == 72, f"Candidate Fit 72, not max(95, 72): {fit['role_match']}"
    assert fit["role_match"]["outdated"] is False and fit["cv_scores"] == {"Legacy CV": 95}
