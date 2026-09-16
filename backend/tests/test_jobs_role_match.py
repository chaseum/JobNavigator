"""GET /jobs carries each job's latest Role Match; the Role Match filters, since_days and sort_by=match use it."""
import hashlib
from datetime import timedelta

from backend.models.db import JobAnalysisRecord, utcnow
from backend.tests.r4_support import client, make_job  # noqa: F401 — client is a fixture

REQS = [
    {"id": "r1", "text": "Python", "category": "technology", "required": True, "importance": 3},
    {"id": "r2", "text": "Kubernetes", "category": "technology", "required": True, "importance": 2},
]


def _analyze(db, job, score, level="Entry level", years="2+ years of Python"):
    if not job.description:
        job.description = """Software Engineer builds reliable services for customers and product teams.
        Responsibilities include designing and operating systems with Python and Kubernetes.
        Qualifications include collaborating across teams, reviewing code, and documenting decisions.
        This role supports cloud infrastructure, APIs, data pipelines, and application reliability.
        Engineers work with customers to understand needs and improve the platform over time."""
        db.commit()
    db.add(JobAnalysisRecord(
        job_id=job.id,
        jd_hash=hashlib.sha256(job.description.encode()).hexdigest(),
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
    # `level` and `max_years` moved to the DERIVED metadata columns
    # (backend/discovery) and are covered by test_jobs_derived_filters.py. What
    # is still analysis-backed is the score and the employment type.
    assert titles("employment_type=full-time") == {"Hi", "Lo"}

    facets = client.get("/api/jobs/facets").json()
    assert {x["name"]: x["count"] for x in facets["employment_types"]} == {"full-time": 2}
    # an employment-type count still narrows by the OTHER Role Match filters
    assert client.get("/api/jobs/facets?min_match=50").json()["employment_types"] == [{"name": "full-time", "count": 1}]


def test_since_days_uses_discovery_time(client, test_db):
    make_job(test_db, title="Old", discovered_at=utcnow() - timedelta(days=10))
    make_job(test_db, title="New")
    assert {r["title"] for r in client.get("/api/jobs?since_days=3").json()["jobs"]} == {"New"}
