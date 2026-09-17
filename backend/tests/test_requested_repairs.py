"""Regression coverage for the résumé evidence and external-origin repairs."""
from types import SimpleNamespace

from backend.copilot import evidence as EV
from backend.copilot import resume_pipeline as RP
from backend.models.db import Job
from backend.scraper._shared.dedup import make_content_hash, make_external_id


def test_resume_import_preserves_structured_date_range_and_never_invents_role():
    items = EV.facts_from_resume_json({
        "experience": [{"company": "CED", "bullets": ["Built the pipeline"]}],
        "education": [{"school": "UH", "degree": "B.S.", "years": "August 2023 – May 2027"}],
    })
    experience = next(x for x in items if x["kind"] == "experience")
    education = next(x for x in items if x["kind"] == "education")
    assert "Role" not in str(experience)
    assert experience["data"]["title"] == ""
    assert education["data"]["start_date"] == "2023-08"
    assert education["data"]["graduation_date"] == "2027-05"


def test_bullet_numeric_conflict_is_not_a_duplicate():
    assert EV.bullet_match("Improved latency by 20%", "Improved latency by 40%") == (False, True)
    assert EV.bullet_match("Improved latency by 20%", "Improved latency by 20 percent") == (True, False)


def test_jake_semantic_headings_are_title_then_employer_and_state_only():
    fact = SimpleNamespace(kind="experience", data={"title": "Software Engineer", "employer": "CED", "location": "Houston, TX", "start_date": "2025-05", "end_date": "2025-08"})
    head = RP.entry_head(fact, semantic=True)
    assert head["title"] == "Software Engineer"
    assert head["employer"] == "CED"
    assert head["location"] == "TX"


def test_external_marker_is_independent_of_scraper_source(api_client, test_db):
    url = "https://jobs.example.test/posting/1"
    job = Job(external_id=make_external_id("Acme", "Engineer", url),
              content_hash=make_content_hash("Acme", "Engineer"), company="Acme", title="Engineer",
              url=url, source="jobspy_linkedin", status="new")
    test_db.add(job)
    test_db.commit()
    out = api_client.post("/api/jobs/manual", json={"title": "Engineer", "company": "Acme", "url": url})
    assert out.status_code == 200 and out.json()["created"] is False
    test_db.refresh(job)
    assert job.externally_added_at is not None
    assert api_client.get("/api/jobs", params={"origin": "external"}).json()["total"] == 1
    assert api_client.get("/api/jobs", params={"origin": "discovered"}).json()["total"] == 0
