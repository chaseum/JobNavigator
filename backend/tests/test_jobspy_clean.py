"""JobSpy field cleaning: pandas nulls must never be stringified into 'None'/'nan' — `_clean` detects the actual null instead of doing `str(cell)` on a None/NaN DataFrame cell."""


def test_clean_returns_none_for_nulls():
    from backend.scraper.sources.jobspy import _clean
    assert _clean(None) is None
    assert _clean(float("nan")) is None
    assert _clean("") is None
    assert _clean("   ") is None


def test_clean_preserves_real_values():
    from backend.scraper.sources.jobspy import _clean
    assert _clean("Senior PM") == "Senior PM"
    assert _clean("  padded text  ") == "padded text"
    assert _clean(42) == "42"  # non-string scalars still coerce


def test_clean_never_produces_literal_none_or_nan():
    """The exact regression: a null must not become the string 'None' or 'nan'."""
    from backend.scraper.sources.jobspy import _clean
    assert _clean(None) != "None"
    assert _clean(float("nan")) != "nan"


def test_jobspy_direct_url_keeps_aggregator_url_too():
    from backend.scraper.sources.jobspy import _row_urls
    apply_url, source_url, canonical_url = _row_urls({
        "job_url": "https://www.linkedin.com/jobs/view/123",
        "job_url_direct": "https://jobs.ashbyhq.com/acme/posting-123",
    })
    assert apply_url == canonical_url == "https://jobs.ashbyhq.com/acme/posting-123"
    assert source_url == "https://www.linkedin.com/jobs/view/123"


def test_jobspy_salary_retains_currency_and_period_without_annualizing():
    from backend.scraper.sources.jobspy import _row_salary
    salary = _row_salary({"min_amount": 62.5, "max_amount": 75, "currency": "CAD",
                          "interval": "hourly", "salary_source": "direct_data"})
    assert salary == {"salary_min": 62, "salary_max": 75, "salary_currency": "CAD",
                      "salary_period": "hourly", "salary_source": "jobspy_posting"}
