"""Job Preferences: one canonical document, and no scraper configuration in sight."""
from backend.discovery import planner
from backend.discovery import preferences as P
from backend.tests.r4_support import client  # noqa: F401 — fixture


def test_defaults_are_a_usable_early_career_search(test_db):
    prefs = P.load(test_db)
    assert prefs["countries"] == ["US"]
    assert "software_engineering" in prefs["job_functions"]
    assert set(prefs["levels"]) >= {"intern", "new_grad", "entry"}
    assert prefs["max_years_experience"] == 2


def test_preferences_persist(test_db):
    P.patch(test_db, {"job_functions": ["data_ml"], "levels": ["new_grad"]})
    again = P.load(test_db)
    assert again["job_functions"] == ["data_ml"]
    assert again["levels"] == ["new_grad"]
    # everything untouched keeps its value
    assert again["countries"] == ["US"]


def test_unknown_ids_are_dropped_not_fatal(test_db):
    saved = P.save(test_db, {"job_functions": ["software_engineering", "underwater_basket_weaving"],
                             "levels": ["entry", "wizard"]})
    assert saved["job_functions"] == ["software_engineering"]
    assert saved["levels"] == ["entry"]


def test_no_scraper_configuration_is_expressible(test_db):
    """A preference document has no place to name a source, a board or an interval."""
    forbidden = {"sources", "search_mode", "results_wanted", "hours_old", "auto_scoring_depth",
                 "run_interval_minutes", "title_exclude_keywords", "title_include_keywords",
                 "max_pages", "proxy_url"}
    assert not forbidden & set(P.DEFAULTS)
    # and an attempt to smuggle one in is simply dropped
    assert not forbidden & set(P.save(test_db, {"sources": ["linkedin"], "results_wanted": 500}))


def test_changing_a_preference_changes_the_plan(test_db):
    P.patch(test_db, {"job_functions": ["software_engineering"]})
    before = {q["search_term"] for q in planner.plan_for(test_db)["queries"]}
    P.patch(test_db, {"job_functions": ["data_ml"]})
    after = {q["search_term"] for q in planner.plan_for(test_db)["queries"]}
    assert before and after and before != after
    assert any("data" in t or "machine learning" in t for t in after)
    assert not any("backend engineer" == t for t in after)


def test_overlapping_saved_filters_deduplicate_discovery_work(test_db):
    P.patch(test_db, {"job_functions": ["software_engineering"], "countries": ["US"], "locations": []})
    alone = planner.plan_for(test_db)["queries"]

    # Two saved filters that say the same thing as the main preferences.
    P.save_filters(test_db, [
        {"id": "a", "name": "SWE US", "active": True,
         "criteria": {"job_functions": ["software_engineering"], "countries": ["US"]}},
        {"id": "b", "name": "SWE US again", "active": True,
         "criteria": {"job_functions": ["software_engineering"], "countries": ["US"]}},
    ])
    with_filters = planner.plan_for(test_db)["queries"]
    assert len(with_filters) == len(alone)

    # A genuinely different filter does add work.
    P.save_filters(test_db, [
        {"id": "c", "name": "Data CA", "active": True,
         "criteria": {"job_functions": ["data_ml"], "countries": ["CA"]}},
    ])
    assert len(planner.plan_for(test_db)["queries"]) > len(alone)


def test_inactive_saved_filters_contribute_nothing(test_db):
    P.patch(test_db, {"job_functions": ["software_engineering"], "countries": ["US"]})
    base = len(planner.plan_for(test_db)["queries"])
    P.save_filters(test_db, [{"id": "c", "name": "Data CA", "active": False,
                              "criteria": {"job_functions": ["data_ml"], "countries": ["CA"]}}])
    assert len(planner.plan_for(test_db)["queries"]) == base


# ── API ──────────────────────────────────────────────────────────────────────

def test_get_and_patch_job_preferences(client):
    body = client.get("/api/job-preferences").json()
    assert "preferences" in body and "taxonomy" in body
    assert {f["id"] for f in body["taxonomy"]["job_functions"]} >= {"software_engineering", "data_ml"}

    r = client.patch("/api/job-preferences", json={"levels": ["new_grad", "entry"]})
    assert r.status_code == 200
    assert r.json()["preferences"]["levels"] == ["new_grad", "entry"]
    assert client.get("/api/job-preferences").json()["preferences"]["levels"] == ["new_grad", "entry"]


def test_patch_rejects_an_unknown_preference(client):
    r = client.patch("/api/job-preferences", json={"jobspy_sources": ["linkedin"]})
    assert r.status_code == 400
    assert "jobspy_sources" in r.json()["detail"]
