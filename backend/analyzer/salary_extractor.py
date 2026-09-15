"""Salary extraction from JD text + H-1B LCA median fallback."""
import re
import logging

logger = logging.getLogger("jobnavigator.salary")


def salary_period_from_text(description: str) -> str | None:
    """Return a pay period only when the posting states one explicitly."""
    text = (description or "").casefold()
    for period, pattern in (
        ("hourly", r"\b(?:hourly|per\s+hour|/\s*hr|/\s*hour)\b"),
        ("monthly", r"\b(?:monthly|per\s+month|/\s*mo|/\s*month)\b"),
        ("weekly", r"\b(?:weekly|per\s+week|/\s*wk|/\s*week)\b"),
        ("daily", r"\b(?:daily|per\s+day|/\s*day)\b"),
        ("yearly", r"\b(?:annual(?:ly)?|yearly|per\s+year|per\s+annum|/\s*yr|/\s*year|a\s+year)\b"),
    ):
        if re.search(pattern, text):
            return period
    return None


def extract_salary(description: str, h1b_median_salary: int = None) -> dict:
    """Extract salary range from job description text, falling back to H-1B LCA median if none found."""
    if not description:
        if h1b_median_salary:
            return {
                "salary_min": h1b_median_salary,
                "salary_max": h1b_median_salary,
                "salary_source": "lca_estimate",
            }
        return {"salary_min": None, "salary_max": None, "salary_source": "unknown"}

    # Cap input size before any regex runs (matches the scraper's 30K truncation) to bound the
    # HTML stripper + salary regex against pathological input.
    description = description[:30_000]

    if "<" in description and ">" in description:
        import html as _html
        description = _html.unescape(re.sub(r'<[^>]+>', ' ', description))

    # Pattern 1: $XXX,XXX - $XXX,XXX (full dollar amounts with range), e.g. $140,000 USD - $210,000 USD,
    # USD$150,000 - USD$200,000, 173,900.00 - 235,200.00 USD, $122,550.00-$201,000.00.
    match = re.search(
        r'(?:USD\s*)?\$\s*(\d{1,3}(?:,\d{3})*)(?:\.\d{1,2})?\s*(?:USD|per\s*year|annually|/\s*yr|/\s*year|a\s*year)?\s*(?:[-–—/]+|(?:and\s+)?up\s+to|to|and)\s*(?:USD\s*)?\$?\s*(\d{1,3}(?:,\d{3})*)(?:\.\d{1,2})?',
        description
    )
    if not match:
        # Bare numbers with USD: 173,900.00 - 235,200.00 USD
        match = re.search(
            r'(\d{1,3}(?:,\d{3})*)(?:\.\d{1,2})?\s*(?:USD\s*)?(?:[-–—/]+|(?:and\s+)?up\s+to|to)\s*(\d{1,3}(?:,\d{3})*)(?:\.\d{1,2})?\s*USD',
            description
        )
    if match:
        low = int(match.group(1).replace(",", ""))
        high = int(match.group(2).replace(",", ""))
        return {"salary_min": low, "salary_max": high, "salary_source": "posting"}

    # Pattern 2: $XXXk - $XXXk (k notation range)
    match = re.search(
        r'\$\s*(\d{2,3})\s*[kK]\s*(?:[-–—/]+|(?:and\s+)?up\s+to|to)\s*\$\s*(\d{2,3})\s*[kK]',
        description
    )
    if match:
        low = int(match.group(1)) * 1000
        high = int(match.group(2)) * 1000
        return {"salary_min": low, "salary_max": high, "salary_source": "posting"}

    # Pattern 3: $XXX,XXX per year / annually (single amount)
    match = re.search(
        r'\$\s*(\d{1,3}(?:,\d{3})*)(?:\.\d{1,2})?\s*(?:per\s*year|annually|/\s*yr|/\s*year|a\s*year)',
        description, re.IGNORECASE
    )
    if match:
        val = int(match.group(1).replace(",", ""))
        return {"salary_min": val, "salary_max": val, "salary_source": "posting"}

    # Pattern 4: $XXXk (single k notation)
    match = re.search(r'\$\s*(\d{2,3})\s*[kK]', description)
    if match:
        val = int(match.group(1)) * 1000
        return {"salary_min": val, "salary_max": val, "salary_source": "posting"}

    # Pattern 5: bare range like $120,000
    match = re.search(r'\$\s*(\d{3},\d{3})(?:\.\d{1,2})?', description)
    if match:
        val = int(match.group(1).replace(",", ""))
        if val >= 30000:  # Sanity check it's an annual salary
            return {"salary_min": val, "salary_max": val, "salary_source": "posting"}

    # Fallback to LCA median
    if h1b_median_salary:
        return {
            "salary_min": h1b_median_salary,
            "salary_max": h1b_median_salary,
            "salary_source": "lca_estimate",
        }

    return {"salary_min": None, "salary_max": None, "salary_source": "unknown"}


def apply_salary_to_job(job, company_h1b_median: int = None) -> None:
    """Keep employer-posted pay ahead of aggregator values and estimates."""
    result = extract_salary(job.description or "", company_h1b_median)
    priority = {"posting_ats": 100, "posting_description": 80, "posting": 80,
                "jobspy_posting": 60, "jobspy_description": 50,
                "lca_estimate": 10, "unknown": 0}
    existing = priority.get(getattr(job, "salary_source", None) or "unknown", 0)
    incoming = 80 if result["salary_source"] == "posting" else 10 if result["salary_source"] == "lca_estimate" else 0
    if job.salary_min is not None and incoming < existing:
        return
    job.salary_min = result["salary_min"]
    job.salary_max = result["salary_max"]
    job.salary_source = "posting_description" if result["salary_source"] == "posting" else result["salary_source"]
    if result["salary_source"] == "posting":
        text = (job.description or "").casefold()
        currency = re.search(r"\b(usd|cad|aud|nzd|gbp|eur|inr|jpy|sgd|chf)\b", text)
        job.salary_currency = currency.group(1).upper() if currency else None
        job.salary_period = salary_period_from_text(text)
    elif result["salary_source"] == "lca_estimate":
        job.salary_currency = "USD"
        job.salary_period = "yearly"
