"""The Jobs feed filters on DERIVED metadata, not on an LLM analysis.

The distinction matters: the old level/experience filters read the Copilot
analysis, so setting one hid every job that had not been analyzed yet. These
columns are filled for every posting at insert.
"""
from backend.discovery import engine
from backend.discovery import preferences as P
from backend.tests.r4_support import client, make_job  # noqa: F401 — client is a fixture


def _titles(client, query):
    return {r["title"] for r in client.get(f"/api/jobs?{query}").json()["jobs"]}


def test_level_filter_works_without_any_analysis(client, test_db):
    make_job(test_db, title="Senior Software Engineer", url="https://x.com/1")
    make_job(test_db, title="New Grad Software Engineer", url="https://x.com/2")
    assert _titles(client, "level=senior") == {"Senior Software Engineer"}
    assert _titles(client, "level=new_grad") == {"New Grad Software Engineer"}


def test_a_posting_with_no_resolved_seniority_stays_visible(client, test_db):
    """Unknown is not a mismatch — dropping it is how a feed goes quiet for no reason."""
    make_job(test_db, title="Senior Software Engineer", url="https://x.com/1")
    make_job(test_db, title="Software Engineer", url="https://x.com/2")
    assert _titles(client, "level=new_grad") == {"Software Engineer"}


def test_job_function_filter(client, test_db):
    make_job(test_db, title="Backend Engineer", url="https://x.com/1")
    make_job(test_db, title="Machine Learning Engineer", url="https://x.com/2")
    assert _titles(client, "job_function=software_engineering") == {"Backend Engineer"}
    assert _titles(client, "job_function=data_ml") == {"Machine Learning Engineer"}


def test_job_type_filter(client, test_db):
    make_job(test_db, title="Software Engineer Intern", url="https://x.com/1")
    make_job(test_db, title="Software Engineer", employment_type="fulltime", url="https://x.com/2")
    assert "Software Engineer Intern" in _titles(client, "job_type=internship")
    assert _titles(client, "job_type=fulltime") >= {"Software Engineer"}


def test_max_years_uses_the_parsed_floor_and_keeps_unknowns(client, test_db):
    make_job(test_db, title="Software Engineer A", url="https://x.com/1",
             description="Requires 8+ years of experience.")
    make_job(test_db, title="Software Engineer B", url="https://x.com/2",
             description="Requires 1+ years of experience.")
    make_job(test_db, title="Software Engineer C", url="https://x.com/3",
             description="Come build with us.")
    got = _titles(client, "max_years=2")
    assert "Software Engineer A" not in got            # explicit evidence, rejected
    assert {"Software Engineer B", "Software Engineer C"} <= got   # stated-low and unknown both stay


def test_facets_count_every_posting_not_only_analyzed_ones(client, test_db):
    make_job(test_db, title="Senior Backend Engineer", url="https://x.com/1")
    make_job(test_db, title="Data Scientist", url="https://x.com/2")
    facets = client.get("/api/jobs/facets").json()
    assert {x["name"] for x in facets["job_functions"]} == {"software_engineering", "data_ml"}
    assert {x["name"]: x["count"] for x in facets["levels"]} == {"senior": 1}


def test_recommended_requires_passing_the_preference_gate(client, test_db):
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": ["new_grad", "entry"],
                      "countries": [], "max_years_experience": 2})
    make_job(test_db, title="New Grad Software Engineer", url="https://x.com/1")
    make_job(test_db, title="Senior Staff Software Engineer", url="https://x.com/2")
    engine.gate_pending(test_db)

    assert _titles(client, "recommended=1&status=new,saved") == {"New Grad Software Engineer"}
    # ...and the unfiltered feed still holds everything that was collected
    assert len(_titles(client, "status=new,saved")) == 2


def test_recommended_shows_a_matching_job_that_is_not_analyzed_yet(client, test_db):
    """A job waiting on Candidate Fit appears as "Analyzing…", not hidden."""
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": [], "countries": []})
    make_job(test_db, title="Software Engineer", url="https://x.com/1")
    engine.gate_pending(test_db)
    rows = client.get("/api/jobs?recommended=1&status=new,saved").json()["jobs"]
    assert [r["title"] for r in rows] == ["Software Engineer"]
    assert rows[0]["role_match"] is None          # the UI reads this as "waiting"


def test_the_job_payload_carries_the_derived_metadata(client, test_db):
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": ["new_grad"], "countries": []})
    make_job(test_db, title="Senior Software Engineer", url="https://x.com/1",
             description="You have 7+ years of experience.")
    engine.gate_pending(test_db)
    row = client.get("/api/jobs").json()["jobs"][0]
    assert row["job_function"] == "software_engineering"
    assert row["experience_levels"] == ["senior"]
    assert row["min_years_experience"] == 7
    assert row["gate_ok"] is False
    assert row["gate_reasons"]
