"""Regression coverage for authoritative posting data and grounded Copilot inputs."""
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from bs4 import BeautifulSoup


def _posting_text():
    return """Software Engineer responsibilities include building reliable services for customers.
    What you'll do: design, implement, and operate production systems with the engineering team.
    Must-haves: experience writing production Python and collaborating across teams.
    Nice-to-haves: distributed systems, developer tools, and cloud infrastructure experience.
    This role is full time, hybrid, and offers a base salary range of $130K-$150K per year.
    Applicants should communicate clearly, write maintainable code, and learn from feedback."""


def test_generic_careers_navigation_is_rejected():
    from backend.scraper.enrichment import validate_job_description
    job = SimpleNamespace(title="Software Engineer, 2027 Graduate U.S.")
    text = "Careers\nBrowse Jobs\nOur Teams\nEarly Careers\nCandidate Resources\nJoin Talent Community"
    result = validate_job_description(job, text, "generic_html")
    assert not result.valid
    assert {"navigation_page", "title_mismatch"} <= set(result.reasons)


def test_title_mismatched_careers_copy_scores_as_low_quality():
    from backend.scraper.enrichment import validate_job_description
    job = SimpleNamespace(title="Software Engineer, 2027 Graduate U.S. in Seattle, Washington")
    text = """Careers\nBrowse Jobs\nOur Teams\nEarly Careers\nTeam Experience\nCandidate Resources
    Join our Talent Community\n\nDon’t see an exact role match? No problem! Join our talent community and
    receive updates about future opportunities, company news, events, and ways
    to connect with teams around the world. Explore our products, values, and
    employee stories, then create a profile to hear from our recruiting team."""
    result = validate_job_description(job, text, "generic_html")
    assert not result.valid
    assert "title_mismatch" in result.reasons
    assert result.score < 65


def test_schema_jobposting_extracts_employer_metadata():
    from backend.scraper.ats._descriptions import _jobposting_jsonld
    html = '''<script type="application/ld+json">{
      "@context":"https://schema.org", "@type":"JobPosting", "title":"Software Engineer",
      "description":"<p>Software Engineer responsibilities include building reliable cloud systems for customers.</p><p>Qualifications include Python, testing, and collaboration across a full-time engineering team.</p>",
      "employmentType":"FULL_TIME", "datePosted":"2026-09-01", "jobLocation":{"address":{"addressLocality":"Seattle","addressRegion":"WA"}},
      "baseSalary":{"currency":"USD","value":{"minValue":130000,"maxValue":150000,"unitText":"YEAR"}}
    }</script>'''
    details = {"title": "Software Engineer"}
    text = _jobposting_jsonld(BeautifulSoup(html, "html.parser"), details)
    assert "responsibilities include building reliable" in text
    assert details["employment_type"] == "FULL_TIME"
    assert details["location"] == "Seattle, WA"
    assert (details["salary_min"], details["salary_max"]) == (130000, 150000)
    assert details["salary_currency"] == "USD" and details["salary_period"] == "yearly"


@pytest.mark.asyncio
async def test_ashby_structured_posting_retains_description_metadata_and_salary(monkeypatch):
    body = {"jobs": [{
        "id": "job-123", "title": "Software Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/job-123",
        "applyUrl": "https://jobs.ashbyhq.com/acme/job-123/application", "location": "Seattle, WA",
        "secondaryLocations": [{"location": "Remote - US"}], "department": "Engineering", "team": "Platform",
        "isRemote": True, "workplaceType": "Hybrid", "employmentType": "FullTime",
        "descriptionPlain": _posting_text(), "publishedAt": "2026-09-01T12:00:00Z",
        "compensation": {"compensationTiers": [{"components": [{
            "compensationType": "Salary", "minValue": 130000, "maxValue": 150000,
            "currencyCode": "USD", "interval": "1 YEAR",
        }]}]},
    }]}
    response = MagicMock(status_code=200)
    response.json.return_value = body
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: client)

    from backend.scraper.ats.ashby import fetch_posting
    result = await fetch_posting("https://jobs.ashbyhq.com/acme/job-123")
    assert result["description"] == _posting_text()
    assert result["employment_type"] == "FullTime"
    assert result["arrangement"] == "Hybrid"
    assert result["salary_min"] == 130000
    assert result["salary_max"] == 150000
    assert result["salary_currency"] == "USD"
    assert result["salary_period"] == "yearly"
    assert result["salary_source"] == "posting_ats"


def test_ats_salary_replaces_lca_estimate_and_is_not_annualized_again():
    from backend.scraper.enrichment import EnrichedJob, apply_enrichment
    job = SimpleNamespace(salary_min=188000, salary_max=188000, salary_currency="USD",
                          salary_period="yearly", salary_source="lca_estimate")
    changed = apply_enrichment(job, EnrichedJob(salary_min=130000, salary_max=150000,
                          salary_currency="USD", salary_period="yearly", salary_source="posting_ats"))
    assert {job.salary_min, job.salary_max, job.salary_source} == {130000, 150000, "posting_ats"}
    assert "salary_source" in changed


def test_posting_salary_does_not_guess_currency_or_period():
    from backend.analyzer.salary_extractor import apply_salary_to_job
    from backend.scraper.enrichment import _salary_from_description
    job = SimpleNamespace(description="Base salary range: $130K-$150K", salary_min=None, salary_max=None,
                          salary_currency=None, salary_period=None, salary_source=None)
    apply_salary_to_job(job)
    assert job.salary_source == "posting_description"
    assert job.salary_currency is None and job.salary_period is None
    parsed = _salary_from_description("Base salary range: $130K-$150K per year")
    assert parsed["salary_period"] == "yearly"


def test_salary_parser_does_not_choose_first_of_multiple_geographic_bands():
    from backend.scraper.enrichment import _salary_from_description
    text = "Current base pay by location: Zone A: $122,400 - $159,800. Zone B: $110,160 - $143,820."
    assert _salary_from_description(text) == {}


def test_unquoted_llm_requirement_is_discarded_and_exact_quote_is_kept():
    from backend.copilot.analysis import normalize_requirements
    jd = _posting_text()
    requirements = [
        {"text": "3+ years of Python", "source_quote": "3+ years of professional Python experience",
         "category": "experience", "required": True},
        {"text": "Production Python experience", "source_quote": "experience writing production Python",
         "category": "technology", "required": True},
    ]
    result = normalize_requirements({"requirements": requirements}, jd=jd)
    assert [r["text"] for r in result["requirements"]] == ["Production Python experience"]
    assert result["requirements"][0]["source_quote"] == "experience writing production Python"


@pytest.mark.asyncio
async def test_generic_careers_page_does_not_reach_llm(monkeypatch):
    from backend.copilot import analysis
    job = SimpleNamespace(
        id="job-id", title="Software Engineer", company="Example", source="jobspy_linkedin",
        description="Careers\nBrowse Jobs\nOur Teams\nTalent Community", description_source="jobspy_posting",
        description_quality=None, description_fetched_at=None, source_url=None, canonical_url=None,
        apply_url=None, url=None, location=None, employment_type=None, salary_min=None, salary_max=None,
        salary_source=None, salary_currency=None, salary_period=None, cached_page_text=None,
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = job
    monkeypatch.setattr(analysis, "SessionLocal", lambda: db)
    call = AsyncMock()
    monkeypatch.setattr(analysis, "call_structured", call)
    with pytest.raises(RuntimeError, match="Could not retrieve a complete job description"):
        await analysis.run_analysis("job-id")
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_tracking_url_reuses_validated_authoritative_sibling(monkeypatch):
    from backend.scraper.enrichment import apply_enrichment, enrich_job

    title = "Software Engineer, 2027 Graduate U.S. in Seattle, Washington"
    url = "https://campus-americas.icims.com/jobs/25813/software-engineer/job?jr_id=posting-25813"
    target = SimpleNamespace(
        id="duplicate", title=title, company="Campus Americas", url=url, source_url=None,
        canonical_url=None, description="Careers\nBrowse Jobs\nTalent Community", source="extension",
        description_source=None, description_quality=None, description_fetched_at=None,
        arr_remote=None, arr_hybrid=None, arr_onsite=None, remote=None,
    )
    sibling = SimpleNamespace(
        id="trusted", title=title, company="Atlassian", url=url + "&mobile=false&width=2367",
        source_url=url, canonical_url="https://www.atlassian.com/company/careers/details/25813",
        apply_url="https://www.atlassian.com/company/careers/details/25813",
        description=_posting_text(), description_source="employer_page", description_quality=100,
        salary_min=130000, salary_max=150000, salary_currency="USD", salary_period="yearly",
        salary_source="posting_description", employment_type="FullTime", arrangement="Hybrid",
        location="Seattle, Washington", published_at=None,
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.limit.return_value.all.return_value = [sibling]
    monkeypatch.setattr("backend.models.db.SessionLocal", lambda: db)

    enriched = await enrich_job(target)
    changed = apply_enrichment(target, enriched)

    assert target.description == sibling.description
    assert target.description_source == "employer_page"
    assert target.canonical_url == sibling.canonical_url
    assert target.company == "Atlassian"
    assert (target.salary_min, target.salary_max, target.salary_source) == (130000, 150000, "posting_description")
    assert {"description", "canonical_url", "company"} <= changed


def test_reenrichment_replaces_garbage_and_changes_analysis_hash():
    from backend.api.routes_jobs import _role_match_summary
    from backend.scraper.enrichment import EnrichedJob, apply_enrichment
    job = SimpleNamespace(
        title="Software Engineer", source="jobspy_linkedin", description="Careers\nBrowse Jobs\nTalent Community",
        description_source="jobspy_posting", description_quality=0, description_fetched_at=None,
        salary_min=188000, salary_max=188000, salary_currency="USD", salary_period="yearly",
        salary_source="lca_estimate", canonical_url=None, apply_url=None, employment_type=None,
        location=None, published_at=None, url=None, enrichment_sources=None,
    )
    old_hash = hashlib.sha256(job.description.encode()).hexdigest()
    fresh = _posting_text()
    apply_enrichment(job, EnrichedJob(description=fresh, description_source="ats_api", description_quality=90,
                                      salary_min=130000, salary_max=150000, salary_currency="USD",
                                      salary_period="yearly", salary_source="posting_ats", fetched=True))
    rec = SimpleNamespace(analysis={"requirements": []}, match={"score": 68}, evidence=[],
                          profile_version="v1", jd_hash=old_hash)
    summary = _role_match_summary(rec, "v1", job)
    assert job.description == fresh
    assert "Must-haves" in job.description and "Nice-to-haves" in job.description
    assert (job.salary_min, job.salary_max, job.salary_source) == (130000, 150000, "posting_ats")
    assert summary["stale"] and summary["jd_stale"]
    assert not summary["needs_job_details"]


def test_rejected_stored_description_is_marked_low_quality():
    from backend.scraper.enrichment import EnrichedJob, apply_enrichment
    job = SimpleNamespace(title="Software Engineer", source="jobspy_linkedin",
                          description="Careers\nBrowse Jobs\nTalent Community",
                          description_source=None, description_quality=None)
    changed = apply_enrichment(job, EnrichedJob(quality_reasons=["navigation_page"]))
    assert job.description_source == "jobspy_posting"
    assert job.description_quality < 65
    assert {"description_source", "description_quality"} <= changed
