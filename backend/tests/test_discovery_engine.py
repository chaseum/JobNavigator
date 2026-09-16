"""Discovery: plan -> collectors -> gate -> company monitoring -> Candidate Fit.

Nothing here reaches the network. The JobSpy adapter is stubbed at the one seam
the engine calls it through, which is also the assertion: the engine speaks to
collectors, and preferences reach them.
"""
import pytest

from backend.discovery import careers, engine, planner
from backend.discovery import preferences as P
from backend.models.db import Company, Job, JobAnalysisRecord
from backend.tests.r4_support import client, make_company, make_job  # noqa: F401 — client is a fixture


# ── the plan reaches the collectors ──────────────────────────────────────────

def test_software_engineering_new_grad_produces_relevant_query_intent(test_db):
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": ["new_grad", "entry"],
                      "countries": ["US"], "date_posted_days": 7})
    plan = planner.plan_for(test_db)
    terms = {q["search_term"] for q in plan["queries"]}
    assert "software engineer" in terms
    assert {"backend engineer", "frontend engineer"} & terms
    for q in plan["queries"]:
        assert q["country"] == "US" and q["location"] == "United States"
        assert q["hours_old"] == 7 * 24
        assert q["levels"] == ["new_grad", "entry"]


def test_data_ml_preferences_produce_different_search_intent(test_db):
    P.patch(test_db, {"job_functions": ["data_ml"]})
    terms = {q["search_term"] for q in planner.plan_for(test_db)["queries"]}
    assert {"machine learning engineer", "data scientist"} & terms
    assert "backend engineer" not in terms


def test_country_and_date_flow_into_the_collector(test_db):
    P.patch(test_db, {"countries": ["CA"], "date_posted_days": 3,
                      "job_functions": ["software_engineering"], "job_types": ["fulltime"]})
    query = planner.plan_for(test_db)["queries"][0]
    search = planner.to_search(query)
    assert search.location == "Canada"
    assert search.country_indeed == "Canada"
    assert search.hours_old == 72
    assert search.job_type == "fulltime"


def test_several_job_types_ask_the_board_for_any(test_db):
    """One slot on the board. Asking for none of them beats picking one arbitrarily —
    that arbitrary pick is what used to return no internships."""
    P.patch(test_db, {"job_types": ["fulltime", "internship"], "job_functions": ["software_engineering"]})
    query = planner.plan_for(test_db)["queries"][0]
    assert query["job_type"] is None
    assert planner.to_search(query).job_type is None


def test_a_discovery_search_carries_no_seniority_title_filter(test_db):
    P.patch(test_db, {"job_functions": ["software_engineering"]})
    search = planner.to_search(planner.plan_for(test_db)["queries"][0])
    assert search.title_exclude_keywords == []
    assert search.title_include_keywords == []
    assert search.id is None          # discovery owns no Search row


def test_the_plan_is_deterministic_and_bounded(test_db):
    P.patch(test_db, {"job_functions": ["software_engineering", "data_ml", "product", "design",
                                        "security", "devops_cloud", "qa", "data_analytics"],
                      "locations": [f"City {i}" for i in range(20)]})
    first = planner.plan_for(test_db)
    second = planner.plan_for(test_db)
    assert first == second
    assert len(first["queries"]) <= planner.MAX_QUERIES
    assert first["truncated"] is True


# ── collection isolates failures ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_failing_query_preserves_the_others(test_db, monkeypatch):
    P.patch(test_db, {"job_functions": ["software_engineering"], "countries": ["US"]})
    plan = planner.plan_for(test_db)
    calls = []

    async def fake_run(search, proxy_url=None):
        calls.append(search.search_term)
        if search.search_term == "backend engineer":
            raise RuntimeError("board exploded")
        return {"jobs_found": 4, "new_jobs": 2, "error": None, "duration": 0.1,
                "source_breakdown": {"linkedin": {"seen": 4, "new": 2}}}

    monkeypatch.setattr("backend.scraper.sources.jobspy.run", fake_run)
    totals = await engine.collect(plan, P.load(test_db))

    assert len(calls) == len(plan["queries"])          # every query still ran
    assert totals["new_jobs"] == 2 * (len(plan["queries"]) - 1)
    assert totals["failed_queries"] == 1


@pytest.mark.asyncio
async def test_a_board_error_inside_a_query_is_recorded_not_raised(test_db, monkeypatch):
    from backend.models.db import ScrapeLog
    P.patch(test_db, {"job_functions": ["qa"], "countries": ["US"]})   # one search term

    async def fake_run(search, proxy_url=None):
        return {"jobs_found": 3, "new_jobs": 1, "error": None, "duration": 0.1,
                "source_breakdown": {"linkedin": {"seen": 3, "new": 1},
                                     "zip_recruiter": {"seen": 0, "new": 0, "error": "403"}}}

    monkeypatch.setattr("backend.scraper.sources.jobspy.run", fake_run)
    await engine.collect(planner.plan_for(test_db), P.load(test_db))

    rows = test_db.query(ScrapeLog).filter(ScrapeLog.source == "discovery").all()
    assert rows and rows[0].new_jobs == 1
    assert rows[0].is_warning is True                 # a refused board is a warning...
    assert rows[0].error is None                      # ...not a failed run


# ── metadata is derived for every stored posting ─────────────────────────────

def test_metadata_is_derived_at_insert_whatever_the_source(test_db):
    job = make_job(test_db, title="Senior Backend Engineer",
                   description="You bring 6+ years of experience with distributed systems.")
    assert job.job_function == "software_engineering"
    assert "senior" in (job.experience_levels or "")
    assert job.min_years_experience == 6


def test_an_unknown_posting_records_unknowns_not_zeros(test_db):
    job = make_job(test_db, title="Software Engineer", description="Join our team!")
    assert job.job_function == "software_engineering"
    assert job.min_years_experience is None           # unknown, NOT zero
    assert not job.experience_levels


# ── the gate runs over what was collected ────────────────────────────────────

def test_gate_pending_stores_a_verdict_per_job(test_db):
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": ["new_grad", "entry"],
                      "max_years_experience": 2, "countries": []})
    make_job(test_db, title="New Grad Software Engineer", url="https://x.com/1")
    make_job(test_db, title="Senior Staff Software Engineer", url="https://x.com/2")

    result = engine.gate_pending(test_db)
    assert result == {"passed": 1, "rejected": 1}
    good = test_db.query(Job).filter(Job.title.like("New Grad%")).first()
    bad = test_db.query(Job).filter(Job.title.like("Senior%")).first()
    assert good.gate_ok is True and bad.gate_ok is False
    assert bad.gate_reasons["reasons"]


def test_changing_preferences_regates_without_recomputing_candidate_fit(test_db, client):
    make_job(test_db, title="Machine Learning Engineer", url="https://x.com/ml")
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": [], "countries": []})
    engine.gate_pending(test_db)
    ml = test_db.query(Job).first()
    test_db.refresh(ml)
    assert ml.gate_ok is False

    client.patch("/api/job-preferences", json={"job_functions": ["data_ml"]})
    test_db.expire_all()
    assert test_db.query(Job).first().gate_ok is True
    # the cheap gate moved; no analysis was created or destroyed
    assert test_db.query(JobAnalysisRecord).count() == 0


def test_discovery_does_not_duplicate_jobs_when_preferences_change(test_db):
    make_job(test_db, title="Software Engineer", url="https://x.com/1")
    before = test_db.query(Job).count()
    P.patch(test_db, {"levels": ["entry"]})
    engine.regate_all(test_db)
    P.patch(test_db, {"levels": ["new_grad"]})
    engine.regate_all(test_db)
    assert test_db.query(Job).count() == before


# ── company automation ───────────────────────────────────────────────────────

def test_relevant_company_with_a_resolvable_board_becomes_monitored(test_db):
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": [], "countries": []})
    make_job(test_db, company="Cloudflare", title="Software Engineer",
             url="https://boards.greenhouse.io/cloudflare/jobs/4012345678")
    engine.gate_pending(test_db)
    engine.reconcile_companies(test_db)

    company = test_db.query(Company).filter(Company.name == "Cloudflare").first()
    assert company is not None
    assert company.scrape_urls == ["https://boards.greenhouse.io/cloudflare"]
    assert company.direct_monitor_status == careers.MONITORED
    assert company.active is True


def test_an_unresolvable_careers_source_is_never_invented(test_db):
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": [], "countries": []})
    make_job(test_db, company="Mystery Corp", title="Software Engineer",
             url="https://www.indeed.com/viewjob?jk=abc123")
    engine.gate_pending(test_db)
    engine.reconcile_companies(test_db)

    company = test_db.query(Company).filter(Company.name == "Mystery Corp").first()
    assert company.scrape_urls == []
    assert company.direct_monitor_status == careers.UNRESOLVED
    assert company.active is False        # aggregators keep supplying its jobs


def test_repeated_jobs_from_one_company_do_not_duplicate_the_record(test_db):
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": [], "countries": []})
    for i in range(5):
        make_job(test_db, company="Cloudflare", title=f"Software Engineer {i}",
                 url=f"https://boards.greenhouse.io/cloudflare/jobs/{i}")
    engine.gate_pending(test_db)
    engine.reconcile_companies(test_db)
    engine.reconcile_companies(test_db)
    assert test_db.query(Company).filter(Company.name == "Cloudflare").count() == 1


def test_an_irrelevant_company_does_not_become_a_direct_monitor(test_db):
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": [], "countries": []})
    make_job(test_db, company="SalesCo", title="Regional Sales Director",
             url="https://boards.greenhouse.io/salesco/jobs/99")
    engine.gate_pending(test_db)
    engine.reconcile_companies(test_db)
    assert test_db.query(Company).filter(Company.name == "SalesCo").first() is None


def test_a_manually_configured_company_is_left_alone(test_db):
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": [], "countries": []})
    make_company(test_db, name="Cloudflare", active=False,
                 scrape_urls=["https://careers.cloudflare.com/hand-written"])
    make_job(test_db, company="Cloudflare", title="Software Engineer",
             url="https://boards.greenhouse.io/cloudflare/jobs/1")
    engine.gate_pending(test_db)
    engine.reconcile_companies(test_db)

    company = test_db.query(Company).filter(Company.name == "Cloudflare").first()
    assert company.scrape_urls == ["https://careers.cloudflare.com/hand-written"]
    assert company.active is False
    assert company.direct_monitor_status is None


def test_an_alias_does_not_create_a_second_company(test_db):
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": [], "countries": []})
    make_company(test_db, name="Google", aliases=["Google LLC"], scrape_urls=[])
    make_job(test_db, company="Google LLC", title="Software Engineer",
             url="https://boards.greenhouse.io/googlellc/jobs/1")
    engine.gate_pending(test_db)
    engine.reconcile_companies(test_db)
    assert test_db.query(Company).count() == 1


@pytest.mark.parametrize("url,expected", [
    ("https://boards.greenhouse.io/acme/jobs/1", "https://boards.greenhouse.io/acme"),
    ("https://jobs.lever.co/acme/abc-123", "https://jobs.lever.co/acme"),
    ("https://jobs.ashbyhq.com/acme/uuid-here", "https://jobs.ashbyhq.com/acme"),
    ("https://www.linkedin.com/jobs/view/123", None),
    ("https://boards.greenhouse.io/", None),            # no slug is not a board
    ("", None),
])
def test_board_url_derivation(url, expected):
    found = careers.board_url_from_posting(url)
    assert (found[0] if found else None) == expected


# ── automatic Candidate Fit ──────────────────────────────────────────────────

def test_a_preference_pass_queues_candidate_fit(test_db, monkeypatch):
    launched = []
    monkeypatch.setattr("backend.job_monitor.launch_background",
                        lambda *a, **kw: launched.append(kw.get("scope_key")) or "run-1")
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": [], "countries": []})
    job = make_job(test_db, title="Software Engineer", description="Build things.",
                   url="https://x.com/1")
    engine.gate_pending(test_db)
    assert engine.queue_candidate_fit(test_db) == 1
    assert launched == [str(job.id)]


def test_a_preference_fail_never_spends_an_llm_analysis(test_db, monkeypatch):
    launched = []
    monkeypatch.setattr("backend.job_monitor.launch_background",
                        lambda *a, **kw: launched.append(kw.get("scope_key")) or "run-1")
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": ["new_grad"],
                      "countries": []})
    make_job(test_db, title="Senior Staff Software Engineer", description="Build things.",
             url="https://x.com/1")
    engine.gate_pending(test_db)
    assert engine.queue_candidate_fit(test_db) == 0
    assert launched == []


def test_an_already_analyzed_job_is_not_re_queued(test_db, monkeypatch):
    launched = []
    monkeypatch.setattr("backend.job_monitor.launch_background",
                        lambda *a, **kw: launched.append(kw.get("scope_key")) or "run-1")
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": [], "countries": []})
    job = make_job(test_db, title="Software Engineer", description="Build things.", url="https://x.com/1")
    test_db.add(JobAnalysisRecord(job_id=job.id, analysis={"requirements": []}))
    test_db.commit()
    engine.gate_pending(test_db)
    assert engine.queue_candidate_fit(test_db) == 0


def test_queueing_is_bounded_per_cycle(test_db, monkeypatch):
    launched = []
    monkeypatch.setattr("backend.job_monitor.launch_background",
                        lambda *a, **kw: launched.append(kw.get("scope_key")) or "run-1")
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": [], "countries": []})
    for i in range(12):
        make_job(test_db, title=f"Software Engineer {i}", description="Build things.",
                 url=f"https://x.com/{i}")
    engine.gate_pending(test_db)
    assert engine.queue_candidate_fit(test_db, limit=5) == 5
    assert len(launched) == 5


def test_automatic_analysis_can_be_switched_off_globally(test_db, monkeypatch):
    from backend.models.db import Setting
    launched = []
    monkeypatch.setattr("backend.job_monitor.launch_background",
                        lambda *a, **kw: launched.append(kw.get("scope_key")) or "run-1")
    test_db.add(Setting(key="auto_analysis_enabled", value="false"))
    test_db.commit()
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": [], "countries": []})
    make_job(test_db, title="Software Engineer", description="Build things.", url="https://x.com/1")
    engine.gate_pending(test_db)
    assert engine.queue_candidate_fit(test_db) == 0


def test_a_failed_analysis_never_loses_the_job_or_the_batch(test_db, monkeypatch):
    """One job that cannot be queued costs that job only — not the posting, not the rest."""
    launched = []

    def flaky(*a, **kw):
        if kw.get("scope_key") == str(bad.id):
            raise RuntimeError("LLM provider is down")
        launched.append(kw.get("scope_key"))
        return "run-1"

    monkeypatch.setattr("backend.job_monitor.launch_background", flaky)
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": [], "countries": []})
    bad = make_job(test_db, title="Software Engineer A", description="Build things.", url="https://x.com/1")
    good = make_job(test_db, title="Software Engineer B", description="Build things.", url="https://x.com/2")
    engine.gate_pending(test_db)

    assert engine.queue_candidate_fit(test_db) == 1
    assert launched == [str(good.id)]
    assert test_db.query(Job).count() == 2          # both postings survive
    test_db.refresh(bad)
    assert bad.gate_ok is True                      # and the failed one keeps its verdict


def test_a_job_with_nothing_to_read_is_not_queued(test_db, monkeypatch):
    launched = []
    monkeypatch.setattr("backend.job_monitor.launch_background",
                        lambda *a, **kw: launched.append(kw.get("scope_key")) or "run-1")
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": [], "countries": []})
    make_job(test_db, title="Software Engineer", description=None, url="", source_url=None,
             canonical_url=None, external_id="no-text-at-all")
    engine.gate_pending(test_db)
    assert engine.queue_candidate_fit(test_db) == 0


def test_status_reports_the_pipeline(test_db):
    P.patch(test_db, {"job_functions": ["software_engineering"], "levels": [], "countries": []})
    make_job(test_db, title="Software Engineer", url="https://x.com/1")
    engine.gate_pending(test_db)
    status = engine.status(test_db)
    assert status["counts"]["gate_passed"] == 1
    assert status["plan"]["queries"]
    assert "preferences" in status and "gate_rejects" in status
