"""Tailoring JD resolution order, quality-first: description → live fetch (persisted) → cached page → none."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock


_VALID_JD = """Software Engineer builds reliable services for customers with the product engineering team.
Responsibilities include designing, implementing, testing, and operating software systems.
Qualifications include experience with Python, APIs, databases, and cloud infrastructure.
The engineer collaborates across teams, communicates clearly, and documents technical decisions.
This full-time role supports customer needs and improves the reliability of the platform."""


def _job(description=None, url=None, cached=None):
    return SimpleNamespace(id="job-id", title="Software Engineer", company="Example",
        source="manual", source_url=None, canonical_url=None, apply_url=None,
        description=description, description_source="ats_api" if description else None,
        description_quality=90 if description else None,
        description_fetched_at=datetime.now(timezone.utc) if description else None,
        url=url, cached_page_text=cached, location=None, employment_type=None,
        salary_min=None, salary_max=None, salary_source=None, salary_currency=None,
        salary_period=None, enrichment_sources=None, arr_remote=None, arr_hybrid=None,
        arr_onsite=None, remote=None, published_at=None)


def test_description_used_first_no_fetch(monkeypatch):
    from backend.api.routes_resumes import _resolve_tailoring_jd
    calls = {"n": 0}

    async def fake_fetch(url):
        calls["n"] += 1
        return "SHOULD NOT BE USED"

    monkeypatch.setattr("backend.scraper.ats._descriptions._fetch_job_description", fake_fetch)
    job = _job(description=_VALID_JD, url="https://x.com/j", cached="noise")
    db = MagicMock()
    out = asyncio.run(_resolve_tailoring_jd(job, db))
    assert out == _VALID_JD
    assert calls["n"] == 0
    db.commit.assert_not_called()


def test_live_fetch_preferred_over_cached_and_persisted(monkeypatch):
    """description empty → live fetch wins over cached page text, and is persisted."""
    from backend.api.routes_resumes import _resolve_tailoring_jd

    async def fake_fetch(url, job=None):
        return _VALID_JD

    monkeypatch.setattr("backend.scraper.ats._descriptions._fetch_job_description", fake_fetch)
    job = _job(description="", url="https://x.com/j", cached="noisy cached page text")
    db = MagicMock()
    out = asyncio.run(_resolve_tailoring_jd(job, db))
    assert out == _VALID_JD
    assert job.description == _VALID_JD  # improved text is persisted back
    db.commit.assert_called_once()


def test_cached_fallback_when_fetch_fails_not_persisted(monkeypatch):
    from backend.api.routes_resumes import _resolve_tailoring_jd

    async def fake_fetch(url):
        return None

    monkeypatch.setattr("backend.scraper.ats._descriptions._fetch_job_description", fake_fetch)
    job = _job(description="", url="https://x.com/j", cached="noisy cached page text")
    db = MagicMock()
    out = asyncio.run(_resolve_tailoring_jd(job, db))
    assert out == ""
    assert job.description == ""  # noisy text must NOT be persisted as the description
    db.commit.assert_not_called()


def test_no_url_skips_fetch_uses_cached(monkeypatch):
    from backend.api.routes_resumes import _resolve_tailoring_jd
    calls = {"n": 0}

    async def fake_fetch(url):
        calls["n"] += 1
        return "x"

    monkeypatch.setattr("backend.scraper.ats._descriptions._fetch_job_description", fake_fetch)
    job = _job(description="", url=None, cached="cached only")
    out = asyncio.run(_resolve_tailoring_jd(job, MagicMock()))
    assert out == ""
    assert calls["n"] == 0


def test_all_empty_returns_empty(monkeypatch):
    from backend.api.routes_resumes import _resolve_tailoring_jd

    async def fake_fetch(url):
        return None

    monkeypatch.setattr("backend.scraper.ats._descriptions._fetch_job_description", fake_fetch)
    job = _job(description="", url=None, cached=None)
    out = asyncio.run(_resolve_tailoring_jd(job, MagicMock()))
    assert out == ""
