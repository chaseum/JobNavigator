"""GET /jobs carries each job's latest Role Match; the Role Match filters, since_days and sort_by=match use it."""
from datetime import timedelta

from backend.models.db import JobAnalysisRecord, utcnow
from backend.tests.r4_support import client, make_job  # noqa: F401 — client is a fixture

REQS = [
    {"id": "r1", "text": "Python", "category": "technology", "required": True, "importance": 3},
    {"id": "r2", "text": "Kubernetes", "category": "technology", "required": True, "importance": 2},
]


def _analyze(db, job, score, level="Entry level", years="2+ years of Python"):
    db.add(JobAnalysisRecord(
        job_id=job.id,
        analysis={"requirements": REQS, "experience_level": level, "employment_type": "Full-time",
                  "experience_requirements": [years]},
        evidence=[{"requirement_id": "r1", "status": "MATCHED", "source_fact_ids": ["project_1"]},
                  {"requirement_id": "r2", "status": "MISSING", "source_fact_ids": []}],
        match={"score": score, "counts": {"MATCHED": 1, "PARTIAL": 0, "MISSING": 1, "UNKNOWN": 0}, "hard_blockers": []}))
    db.commit()


def test_role_match_summary_filters_and_sort(client, test_db):
    hi, lo = make_job(test_db, title="Hi"), make_job(test_db, title="Lo")
    make_job(test_db, title="Unanalyzed")
    _analyze(test_db, lo, 40, level="Senior", years="5-7 years")
    _analyze(test_db, hi, 10)   # superseded by the newer record below
    _analyze(test_db, hi, 88)

    rows = client.get("/api/jobs?sort_by=match").json()["jobs"]
    assert [r["title"] for r in rows] == ["Hi", "Lo", "Unanalyzed"]
    rm = rows[0]["role_match"]
    assert rm["score"] == 88 and rm["matched"] == ["Python"] and rm["missing"] == ["Kubernetes"]
    assert rm["years_required"] == 2 and rm["employment_type"] == "Full-time" and rm["stale"] is True
    assert rows[1]["role_match"]["years_required"] == 5
    assert rows[2]["role_match"] is None

    def titles(q):
        return {r["title"] for r in client.get(f"/api/jobs?{q}").json()["jobs"]}
    assert titles("min_match=50") == {"Hi"}
    assert titles("level=senior") == {"Lo"}
    assert titles("max_years=3") == {"Hi"}

    facets = client.get("/api/jobs/facets").json()
    assert {x["name"]: x["count"] for x in facets["levels"]} == {"entry level": 1, "senior": 1}
    # a level count still narrows by the OTHER Role Match filters
    assert client.get("/api/jobs/facets?min_match=50").json()["levels"] == [{"name": "entry level", "count": 1}]


def test_since_days_uses_discovery_time(client, test_db):
    make_job(test_db, title="Old", discovered_at=utcnow() - timedelta(days=10))
    make_job(test_db, title="New")
    assert {r["title"] for r in client.get("/api/jobs?since_days=3").json()["jobs"]} == {"New"}
