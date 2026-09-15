"""Resolve discovered jobs to employer-owned postings before analysis or tailoring."""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import urlparse

from bs4 import BeautifulSoup

logger = logging.getLogger("jobnavigator.scraper.enrichment")

_NAV_LINES = {
    "careers", "browse jobs", "our teams", "early careers", "candidate resources",
    "join our talent community", "join the talent community", "talent community",
    "view all jobs", "search jobs", "about us", "privacy policy", "cookie policy",
    "accessibility", "terms of use", "follow us", "our locations", "life at",
}
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into",
    "is", "it", "of", "on", "or", "our", "the", "this", "to", "we", "with", "you",
}
_JOB_CONTENT = re.compile(
    r"\b(responsibilit(?:y|ies)|qualifications?|requirements?|what you.ll do|"
    r"what we.re looking for|about the role|must.haves?|nice.to.haves?|experience|"
    r"skills?|benefits|compensation|salary|employment type)\b", re.I,
)
_ATS_HOSTS = ("ashbyhq.com", "greenhouse.io", "lever.co", "myworkdayjobs.com")
_AGGREGATOR_HOSTS = ("linkedin.com", "indeed.com", "ziprecruiter.com", "glassdoor.com", "google.com")
_DESC_PRIORITY = {"ats_api": 100, "employer_page": 80, "jobspy_posting": 60, "generic_html": 20, "unknown": 10}
_SALARY_PRIORITY = {"posting_ats": 100, "posting_description": 80, "jobspy_posting": 60,
                    "jobspy_description": 50, "posting": 50, "lca_estimate": 10, "unknown": 0}


@dataclass
class DescriptionQuality:
    valid: bool
    score: int
    reasons: list[str] = field(default_factory=list)


@dataclass
class EnrichedJob:
    description: str | None = None
    description_source: str | None = None
    description_quality: int | None = None
    company: str | None = None
    canonical_url: str | None = None
    apply_url: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    salary_source: str | None = None
    employment_type: str | None = None
    arrangement: str | None = None
    location: str | None = None
    published_at: datetime | None = None
    fetched: bool = False
    field_sources: dict = field(default_factory=dict)
    quality_reasons: list[str] = field(default_factory=list)


def validate_job_description(job, text: str | None, source: str | None = None) -> DescriptionQuality:
    """Reject navigation shells and text that cannot plausibly describe this role."""
    del source  # source is retained in the public contract for source-aware rules later.
    raw = BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)
    words = re.findall(r"[a-z0-9+#.]+", raw.casefold())
    lines = [re.sub(r"\s+", " ", line).strip(" .|-").casefold() for line in (text or "").splitlines()]
    nav_lines = sum(1 for line in lines if line in _NAV_LINES)
    title_tokens = [t for t in re.findall(r"[a-z0-9]+", str(getattr(job, "title", "") or "").casefold())
                    if len(t) >= 3 and t not in _STOPWORDS and t not in {"senior", "junior", "graduate", "early", "career"}]
    words_set = set(words)
    title_hits = sum(1 for token in title_tokens
                     if token in words_set or any(w.startswith(token[:6]) for w in words_set if len(token) >= 6))
    reasons = []
    score = 100
    if len(raw) < 220 or len(words) < 35:
        reasons.append("too_short")
        score -= 50
    if title_tokens and not title_hits:
        reasons.append("title_mismatch")
        score -= 35
    if nav_lines >= 3 and nav_lines >= max(3, len(lines) // 2):
        reasons.append("navigation_page")
        score -= 60
    if len(set(words) - _STOPWORDS) < 28:
        reasons.append("low_content_density")
        score -= 35
    if title_hits and title_tokens:
        score += min(10, round(10 * title_hits / len(title_tokens)))
    if _JOB_CONTENT.search(raw):
        score += 5
    score = max(0, min(score, 100))
    if reasons:
        # The maintenance selector uses <65 as its low-quality threshold, so a
        # rejected description must never score as though it passed validation.
        score = min(score, 64)
    return DescriptionQuality(valid=not reasons, score=score, reasons=reasons)


def _is_host(url: str, domains: tuple[str, ...]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _clean_description(text: str | None) -> str | None:
    if not text:
        return None
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:30000] or None


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _page_enrichment(text: str, source: str, url: str, quality: DescriptionQuality, details: dict) -> EnrichedJob:
    result = EnrichedJob(description=text, description_source=source, description_quality=quality.score,
                         canonical_url=url if source == "employer_page" else None, fetched=True)
    for key in ("employment_type", "arrangement", "location"):
        if details.get(key):
            setattr(result, key, details[key])
            result.field_sources[key] = source
    result.published_at = _parse_date(details.get("published_at"))
    if result.published_at:
        result.field_sources["published_at"] = source
    if details.get("salary_min") is not None:
        for key in ("salary_min", "salary_max", "salary_currency", "salary_period"):
            setattr(result, key, details.get(key))
        result.salary_source = details.get("salary_source") or "posting_description"
        if source == "generic_html":
            result.salary_source = "jobspy_description"
        result.field_sources["salary"] = source
    result.field_sources["description"] = source
    if result.canonical_url:
        result.field_sources["canonical_url"] = source
    return result


def _salary_from_description(text: str) -> dict:
    from backend.analyzer.salary_extractor import extract_salary, salary_period_from_text
    if len(re.findall(r"\bzone\s+[a-z]\s*:", text or "", re.I)) > 1:
        # A posting may publish different bands by geography. Without a reliable
        # location-to-zone mapping, selecting the first band would be misleading.
        return {}
    found = extract_salary(text)
    if found.get("salary_min") is None:
        return {}
    sample = text.casefold()
    period = salary_period_from_text(sample)
    currency = re.search(r"\b(usd|cad|aud|nzd|gbp|eur|inr|jpy|sgd|chf)\b", sample)
    return {"salary_min": found["salary_min"], "salary_max": found["salary_max"],
            "salary_currency": currency.group(1).upper() if currency else None, "salary_period": period,
            "salary_source": "posting_description"}


async def _fetch_direct(url: str, job) -> EnrichedJob | None:
    """Use the exact ATS detail when known, then existing ATS detail handlers."""
    from backend.scraper.ats._descriptions import _fetch_description_ats

    if _is_host(url, ("ashbyhq.com",)):
        from backend.scraper.ats.ashby import fetch_posting
        posting = await fetch_posting(url)
        if posting:
            desc = _clean_description(posting.get("description"))
            quality = validate_job_description(job, desc, "ats_api")
            if quality.valid:
                return EnrichedJob(
                    description=desc, description_source="ats_api", description_quality=quality.score,
                    canonical_url=posting.get("canonical_url") or url, apply_url=posting.get("apply_url"),
                    salary_min=posting.get("salary_min"), salary_max=posting.get("salary_max"),
                    salary_currency=posting.get("salary_currency"), salary_period=posting.get("salary_period"),
                    salary_source=posting.get("salary_source"), employment_type=posting.get("employment_type"),
                    arrangement=posting.get("arrangement"), location=posting.get("location"),
                    published_at=_parse_date(posting.get("published_at")),
                    fetched=True,
                    field_sources={k: "ats_api" for k in ("description", "canonical_url", "apply_url", "salary", "employment_type", "arrangement", "location", "published_at")},
                )
    details = {"title": getattr(job, "title", None), "company": getattr(job, "company", None)}
    text = await _fetch_description_ats(url, job=details)
    text = _clean_description(text)
    if not text:
        return None
    quality = validate_job_description(job, text, "ats_api")
    if not quality.valid:
        return None
    return EnrichedJob(description=text, description_source="ats_api", description_quality=quality.score,
                       canonical_url=url, fetched=True,
                       field_sources={"description": "ats_api", "canonical_url": "ats_api"})


def _company_ats_urls(company_name: str) -> list[str]:
    """Return configured ATS boards for this company; no third-party company guessing."""
    if not company_name:
        return []
    try:
        from backend.models.db import Company, SessionLocal
        db = SessionLocal()
        try:
            company = db.query(Company).filter(Company.name.ilike(company_name)).first()
            return [u for u in (company.scrape_urls or []) if _is_host(u, _ATS_HOSTS)][:3] if company else []
        finally:
            db.close()
    except Exception as e:
        logger.debug("Company ATS lookup failed: %s", e)
        return []


def _title_similarity(left: str, right: str) -> float:
    norm = lambda value: " ".join(re.findall(r"[a-z0-9]+", (value or "").casefold()))
    return SequenceMatcher(None, norm(left), norm(right)).ratio()


def _posting_identity(url: str | None) -> tuple[str, str] | None:
    """Identify a detail URL independent of tracking query parameters."""
    if not url:
        return None
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = re.sub(r"/+", "/", parsed.path or "").rstrip("/").casefold()
    segments = [segment for segment in path.split("/") if segment]
    if (not host or len(path) < 12
            or (len(segments) <= 2 and segments[-1] in {"career", "careers", "job", "jobs", "search", "openings"})):
        return None
    return host, path


def _existing_posting_enrichment(job) -> EnrichedJob | None:
    """Reuse trusted data when this is another saved copy of the same posting."""
    title = str(getattr(job, "title", "") or "")
    identities = {
        identity
        for url in (getattr(job, "url", None), getattr(job, "source_url", None),
                    getattr(job, "canonical_url", None))
        if (identity := _posting_identity(url)) is not None
    }
    if not title or not identities:
        return None
    try:
        from backend.models.db import Job, SessionLocal
        db = SessionLocal()
        try:
            siblings = (db.query(Job)
                        .filter(Job.title == title, Job.id != getattr(job, "id", None),
                                Job.description_source.in_(("ats_api", "employer_page")))
                        .limit(20).all())
        finally:
            db.close()
    except Exception as e:
        logger.debug("Existing posting lookup failed: %s", e)
        return None

    for sibling in siblings:
        if _title_similarity(title, getattr(sibling, "title", "")) < 0.9:
            continue
        sibling_identities = {
            identity
            for url in (getattr(sibling, "url", None), getattr(sibling, "source_url", None),
                        getattr(sibling, "canonical_url", None))
            if (identity := _posting_identity(url)) is not None
        }
        if not identities.intersection(sibling_identities):
            continue
        description = _clean_description(getattr(sibling, "description", None))
        quality = validate_job_description(job, description, getattr(sibling, "description_source", None))
        if not quality.valid:
            continue
        result = EnrichedJob(
            description=description,
            description_source=sibling.description_source,
            description_quality=getattr(sibling, "description_quality", None) or quality.score,
            company=getattr(sibling, "company", None),
            canonical_url=getattr(sibling, "canonical_url", None),
            apply_url=getattr(sibling, "apply_url", None),
            salary_min=getattr(sibling, "salary_min", None),
            salary_max=getattr(sibling, "salary_max", None),
            salary_currency=getattr(sibling, "salary_currency", None),
            salary_period=getattr(sibling, "salary_period", None),
            salary_source=getattr(sibling, "salary_source", None),
            employment_type=getattr(sibling, "employment_type", None),
            arrangement=getattr(sibling, "arrangement", None),
            location=getattr(sibling, "location", None),
            published_at=getattr(sibling, "published_at", None),
            fetched=True,
            field_sources={"description": sibling.description_source,
                           "canonical_url": "existing_posting"},
        )
        return result
    return None


async def _resolve_company_ats(job) -> str | None:
    """Resolve a configured company board to the matching canonical posting URL."""
    title = str(getattr(job, "title", "") or "")
    for board_url in _company_ats_urls(str(getattr(job, "company", "") or "")):
        try:
            if _is_host(board_url, ("ashbyhq.com",)):
                from backend.scraper.ats.ashby import scrape
            elif _is_host(board_url, ("greenhouse.io",)):
                from backend.scraper.ats.greenhouse import scrape
            elif _is_host(board_url, ("lever.co",)):
                from backend.scraper.ats.lever import scrape
            elif _is_host(board_url, ("myworkdayjobs.com",)):
                from backend.scraper.ats.workday import scrape
            else:
                continue
            postings = await scrape(board_url)
            match = max(postings or [], key=lambda p: _title_similarity(title, p.get("title", "")), default=None)
            if match and _title_similarity(title, match.get("title", "")) >= 0.88:
                return match.get("canonical_url") or match.get("url")
        except Exception as e:
            logger.debug("Company ATS resolution failed for %s: %s", board_url, e)
    return None


async def enrich_job(job, *, now: datetime | None = None) -> EnrichedJob:
    """Resolve employer/ATS fields, then fall back to trusted JobSpy text if needed."""
    now = now or datetime.now(timezone.utc)
    existing_text = _clean_description(getattr(job, "description", None))
    existing_source = getattr(job, "description_source", None)
    if not existing_source and str(getattr(job, "source", "")).startswith("jobspy"):
        existing_source = "jobspy_posting"
    existing_quality = validate_job_description(job, existing_text, existing_source)
    fetched_at = getattr(job, "description_fetched_at", None)
    fresh = isinstance(fetched_at, datetime) and now - fetched_at < timedelta(days=30)
    if existing_quality.valid and existing_source in ("ats_api", "employer_page") and fresh:
        return EnrichedJob(description=existing_text, description_source=existing_source,
                           description_quality=getattr(job, "description_quality", None) or existing_quality.score)

    urls = []
    for url in (getattr(job, "canonical_url", None), getattr(job, "url", None), getattr(job, "source_url", None)):
        if url and url not in urls:
            urls.append(url)

    existing_posting = _existing_posting_enrichment(job)
    if existing_posting:
        return existing_posting

    for url in urls:
        if not _is_host(url, _ATS_HOSTS):
            continue
        try:
            result = await _fetch_direct(url, job)
            if result:
                return result
        except Exception as e:
            logger.debug("Direct ATS enrichment failed for %s: %s", url, e)

    from backend.scraper.ats._descriptions import _fetch_job_description
    employer_urls = [url for url in urls if not _is_host(url, _ATS_HOSTS + _AGGREGATOR_HOSTS)]
    for url in employer_urls:
        try:
            details = {"title": getattr(job, "title", None), "company": getattr(job, "company", None)}
            text = _clean_description(await _fetch_job_description(url, job=details))
            quality = validate_job_description(job, text, "employer_page")
            if quality.valid and (not existing_quality.valid or
                                  _DESC_PRIORITY["employer_page"] > _DESC_PRIORITY.get(existing_source or "unknown", 10)):
                result = _page_enrichment(text, "employer_page", url, quality, details)
                salary = _salary_from_description(text)
                if result.salary_source is None:
                    result.__dict__.update(salary)
                    if salary:
                        result.field_sources["salary"] = "employer_page"
                return result
        except Exception as e:
            logger.debug("Employer page enrichment failed for %s: %s", url, e)

    resolved = await _resolve_company_ats(job)
    if resolved and resolved not in urls:
        try:
            result = await _fetch_direct(resolved, job)
            if result:
                return result
        except Exception as e:
            logger.debug("Resolved ATS detail failed for %s: %s", resolved, e)

    for url in urls:
        if url in employer_urls:
            continue
        try:
            details = {"title": getattr(job, "title", None), "company": getattr(job, "company", None)}
            text = _clean_description(await _fetch_job_description(url, job=details))
            quality = validate_job_description(job, text, "employer_page")
            if quality.valid:
                source = ("generic_html" if _is_host(url, _AGGREGATOR_HOSTS)
                          else "ats_api" if _is_host(url, _ATS_HOSTS) else "employer_page")
                if existing_quality.valid and _DESC_PRIORITY[source] <= _DESC_PRIORITY.get(existing_source or "unknown", 10):
                    continue
                result = _page_enrichment(text, source, url, quality, details)
                if source == "employer_page":
                    salary = _salary_from_description(text)
                    if result.salary_source is None:
                        result.__dict__.update(salary)
                        if salary:
                            result.field_sources["salary"] = "employer_page"
                return result
        except Exception as e:
            logger.debug("Page enrichment failed for %s: %s", url, e)

    if existing_quality.valid:
        fallback_source = existing_source or ("jobspy_posting" if str(getattr(job, "source", "")).startswith("jobspy") else "unknown")
        return EnrichedJob(description=existing_text, description_source=fallback_source,
                           description_quality=existing_quality.score,
                           field_sources={"description": fallback_source})
    return EnrichedJob(quality_reasons=existing_quality.reasons)


def apply_enrichment(job, enriched: EnrichedJob, *, fetched_at: datetime | None = None) -> set[str]:
    """Apply only equal or stronger provenance; return fields whose values changed."""
    fetched_at = fetched_at or datetime.now(timezone.utc)
    changed = set()
    new_desc_priority = _DESC_PRIORITY.get(enriched.description_source or "unknown", 0)
    old_desc_source = getattr(job, "description_source", None)
    if not old_desc_source and str(getattr(job, "source", "")).startswith("jobspy"):
        old_desc_source = "jobspy_posting"
    old_desc_priority = (_DESC_PRIORITY.get(old_desc_source or "unknown", 10)
                         if validate_job_description(job, getattr(job, "description", None), old_desc_source).valid else 0)
    if enriched.description and new_desc_priority >= old_desc_priority:
        values = [("description", enriched.description), ("description_source", enriched.description_source),
                  ("description_quality", enriched.description_quality)]
        if enriched.fetched:
            values.append(("description_fetched_at", fetched_at))
        for key, value in values:
            if getattr(job, key, None) != value:
                setattr(job, key, value)
                changed.add(key)

    for key in ("company", "canonical_url", "apply_url", "employment_type", "location", "published_at"):
        value = getattr(enriched, key)
        if value and getattr(job, key, None) != value:
            setattr(job, key, value)
            changed.add(key)
    if enriched.canonical_url and not getattr(job, "url", None):
        job.url = enriched.canonical_url
        changed.add("url")
    if enriched.arrangement:
        from backend.analyzer.work_arrangement import apply_arrangement_to_job
        apply_arrangement_to_job(job, structured=enriched.arrangement)

    salary_priority = _SALARY_PRIORITY.get(enriched.salary_source or "unknown", 0)
    old_salary_priority = _SALARY_PRIORITY.get(getattr(job, "salary_source", None) or "unknown", 0)
    if enriched.salary_min is not None and salary_priority >= old_salary_priority:
        for key in ("salary_min", "salary_max", "salary_currency", "salary_period", "salary_source"):
            value = getattr(enriched, key)
            if getattr(job, key, None) != value:
                setattr(job, key, value)
                changed.add(key)
    if enriched.field_sources:
        current = dict(getattr(job, "enrichment_sources", None) or {})
        if current != {**current, **enriched.field_sources}:
            current.update(enriched.field_sources)
            job.enrichment_sources = current
            changed.add("enrichment_sources")
    elif not enriched.description and enriched.quality_reasons:
        # Keep rejected legacy text available for diagnostics, but persist that it
        # failed the same gate used by analysis and the UI.
        quality = validate_job_description(job, getattr(job, "description", None),
                                           getattr(job, "description_source", None))
        if not quality.valid and getattr(job, "description_quality", None) != quality.score:
            job.description_quality = quality.score
            changed.add("description_quality")
        if (not getattr(job, "description_source", None)
                and str(getattr(job, "source", "")).startswith("jobspy")):
            job.description_source = "jobspy_posting"
            changed.add("description_source")
    return changed


async def re_enrich_query(*, job_id: str | None = None, low_quality: bool = False,
                          recent_days: int | None = None, limit: int = 50,
                          reanalyze: bool = False) -> dict:
    """Re-enrich a bounded set of existing rows; usable from the CLI and tests."""
    from backend.models.db import Job, SessionLocal
    db = SessionLocal()
    try:
        query = db.query(Job)
        if job_id:
            query = query.filter(Job.id == job_id)
        if recent_days is not None:
            query = query.filter(Job.discovered_at >= datetime.now(timezone.utc) - timedelta(days=recent_days))
        if low_quality:
            from sqlalchemy import or_
            query = query.filter(or_(Job.description_quality.is_(None), Job.description_quality < 65,
                                     Job.description.is_(None), Job.description == ""))
        jobs = query.order_by(Job.discovered_at.desc()).limit(max(1, min(limit, 500))).all()
        ids = [str(job.id) for job in jobs]
    finally:
        db.close()

    report = {"processed": 0, "updated": 0, "rejected": 0, "reanalyzed": 0, "jobs": []}
    from backend.models.db import Job, SessionLocal
    for selected_id in ids:
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.id == selected_id).first()
            if not job:
                continue
            enriched = await enrich_job(job)
            changed = apply_enrichment(job, enriched)
            db.commit()
            report["processed"] += 1
            report["updated"] += bool(changed)
            report["rejected"] += not bool(enriched.description)
            report["jobs"].append({"id": selected_id, "title": job.title, "company": job.company,
                                   "description_source": job.description_source,
                                   "description_quality": job.description_quality, "changed": sorted(changed)})
            should_analyze = reanalyze and bool(enriched.description)
        finally:
            db.close()
        if should_analyze:
            from backend.copilot.analysis import run_analysis
            await run_analysis(selected_id)
            report["reanalyzed"] += 1
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-enrich stored jobs from employer and ATS sources")
    parser.add_argument("--job-id", help="Re-enrich one Job UUID")
    parser.add_argument("--low-quality", action="store_true", help="Select jobs without a passing stored quality score")
    parser.add_argument("--recent-days", type=int, help="Limit selection to jobs discovered this many days ago")
    parser.add_argument("--limit", type=int, default=50, help="Maximum rows to process (capped at 500)")
    parser.add_argument("--reanalyze", action="store_true", help="Run Copilot after each valid posting is stored")
    args = parser.parse_args()
    print(asyncio.run(re_enrich_query(job_id=args.job_id, low_quality=args.low_quality,
                                      recent_days=args.recent_days, limit=args.limit,
                                      reanalyze=args.reanalyze)))


if __name__ == "__main__":
    main()
