"""Ashby ATS handler: GET api.ashbyhq.com/posting-api/job-board/{company}; host-matched (not substring) to avoid attacker-controlled paths, with department/location/team filters applied client-side by resolving IDs to names from the board HTML since the API doesn't return names itself."""
import json
import logging
import re
from urllib.parse import parse_qs, urlparse

import httpx

from backend.scraper._shared.browser import _USER_AGENT
from backend.scraper._shared.filters import _validate_job
from backend.scraper._shared.urls import host_matches

logger = logging.getLogger("jobnavigator.scraper.ats.ashby")


def is_ashby(url: str) -> bool:
    """Check if URL is an Ashby job board (jobs.ashbyhq.com)."""
    return host_matches(url, "jobs.ashbyhq.com")


def _resolve_group_names(page_text: str, filter_ids: set) -> set:
    """Resolve department/team filter IDs to their names, including descendants, since Ashby's board
    filters a node and all its children (BFS over parent->children built from the embedded id/name JSON)."""
    if not filter_ids:
        return set()
    name_by_id = dict(re.findall(r'"id"\s*:\s*"([0-9a-f-]{36})"\s*,\s*"name"\s*:\s*"([^"]+)"', page_text))
    children: dict = {}
    for m in re.finditer(r'"id"\s*:\s*"([0-9a-f-]{36})"[^{}]*?"parent(?:Team|Department)Id"\s*:\s*"([0-9a-f-]{36})"', page_text):
        children.setdefault(m.group(2), []).append(m.group(1))
    names, seen, stack = set(), set(), list(filter_ids)
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if cur in name_by_id:
            names.add(name_by_id[cur])
        stack.extend(children.get(cur, []))
    return names


def _description(posting: dict) -> str | None:
    """Prefer Ashby's plain-text field, keeping HTML paragraph/list boundaries as fallback."""
    plain = (posting.get("descriptionPlain") or posting.get("descriptionPlainText") or "").strip()
    if plain:
        return plain
    markup = posting.get("descriptionHtml") or ""
    if not markup:
        return None
    from bs4 import BeautifulSoup
    return BeautifulSoup(markup, "html.parser").get_text(separator="\n", strip=True) or None


def _salary(posting: dict) -> dict:
    """Read only structured Salary components; never treat equity/bonus as base pay."""
    compensation = posting.get("compensation") or {}
    if not compensation:
        compensation = posting
    components = []
    for tier in compensation.get("compensationTiers") or []:
        components.extend((tier or {}).get("components") or [])
    if not components:
        components = compensation.get("summaryComponents") or []
    salaries = [c for c in components if (c or {}).get("compensationType", "Salary").lower() == "salary"
                and (c or {}).get("minValue") is not None and (c or {}).get("maxValue") is not None]
    if not salaries:
        return {}
    currencies = {c.get("currencyCode") for c in salaries if c.get("currencyCode")}
    intervals = {c.get("interval") for c in salaries if c.get("interval")}
    if len(currencies) > 1 or len(intervals) > 1:
        return {}
    low = min(float(c["minValue"]) for c in salaries)
    high = max(float(c["maxValue"]) for c in salaries)
    interval = next(iter(intervals), None)
    period = {"1 YEAR": "yearly", "1 MONTH": "monthly", "1 WEEK": "weekly", "1 DAY": "daily", "1 HOUR": "hourly"}.get(interval, interval)
    return {
        "salary_min": round(low), "salary_max": round(high),
        "salary_currency": next(iter(currencies), None), "salary_period": period,
        "salary_source": "posting_ats",
    }


async def fetch_posting(url: str) -> dict | None:
    """Fetch one published Ashby job with its public structured compensation."""
    if not is_ashby(url):
        return None
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    slug, posting_id = parts[0], parts[1]
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(api_url, params={"includeCompensation": "true"})
        if resp.status_code != 200:
            return None
        postings = resp.json().get("jobs") or []
    posting = next((p for p in postings if str(p.get("id") or "") == posting_id), None)
    if not posting:
        return None
    secondary = [x.get("location") for x in posting.get("secondaryLocations") or []
                 if isinstance(x, dict) and x.get("location")]
    out = {
        "id": posting.get("id"), "title": posting.get("title"),
        "canonical_url": posting.get("jobUrl"), "apply_url": posting.get("applyUrl"),
        "location": posting.get("location"), "secondary_locations": secondary,
        "department": posting.get("department"), "team": posting.get("team"),
        "remote": posting.get("isRemote"), "arrangement": posting.get("workplaceType"),
        "description": _description(posting), "published_at": posting.get("publishedAt"),
        "employment_type": posting.get("employmentType"),
        "compensation": posting.get("compensation") or {},
    }
    out.update(_salary(posting))
    return out


async def scrape(url: str, debug: bool = False) -> list[dict] | tuple:
    """Fetch jobs from Ashby's public JSON API; departmentId/locationId filtering is applied
    client-side since the API returns all jobs unfiltered."""
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    if not path_parts:
        if debug:
            return [], [{"title": "(none)", "url": url, "selector": "ashby_api", "reason": "No company slug in URL"}]
        return []
    company_slug = path_parts[0]

    qs = parse_qs(parsed.query)
    filter_dept_ids = set(qs.get("departmentId", []))
    filter_location_ids = set(qs.get("locationId", []))
    filter_team_ids = set(qs.get("teamId", []))

    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{company_slug}"
    logger.info(f"Ashby API: {api_url} dept_filter={len(filter_dept_ids)} loc_filter={len(filter_location_ids)} team_filter={len(filter_team_ids)}")

    jobs = []
    rejected = []

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(api_url, params={"includeCompensation": "true"})
        if resp.status_code != 200:
            logger.warning(f"Ashby API returned {resp.status_code} for {company_slug}")
            if debug:
                return [], [{"title": "(none)", "url": api_url, "selector": "ashby_api", "reason": f"HTTP {resp.status_code}"}]
            return []

        data = json.loads(resp.text)

        # Ashby embeds ID→name mappings in the page HTML, not in the API response.
        # Fetch page once to resolve the filter IDs to names.
        group_names = set()  # departmentId is Ashby's generic grouping filter — some boards group by
                              # department, others by team (e.g. Plaid), so it may resolve to either
        loc_names = set()
        if filter_dept_ids or filter_location_ids or filter_team_ids:
            try:
                page_resp = await client.get(url, headers={"Accept": "text/html", "User-Agent": _USER_AGENT})
                page_text = page_resp.text
                group_names = _resolve_group_names(page_text, filter_dept_ids | filter_team_ids)
                for loc_id in filter_location_ids:
                    # Location mapping uses "locationId"/"locationName" in job entries
                    m = re.search(
                        rf'"locationId"\s*:\s*"{re.escape(loc_id)}"[^}}]*?"locationName"\s*:\s*"([^"]+)"',
                        page_text,
                    )
                    if m:
                        loc_names.add(m.group(1))
                logger.info(f"Ashby: resolved groups={group_names}, locs={loc_names}")
            except Exception as e:
                logger.warning(f"Ashby: could not resolve filter names: {e}")

        for posting in data.get("jobs", []):
            if not posting.get("isListed", True):
                continue

            title = (posting.get("title") or "").strip()
            job_url = posting.get("jobUrl") or ""

            # A posting passes if EITHER its department or team is in the resolved group names —
            # boards vary in which field carries the real grouping (e.g. Plaid uses team, not department).
            if group_names:
                job_dept = (posting.get("department") or "").strip()
                job_team = (posting.get("team") or "").strip()
                if job_dept not in group_names and job_team not in group_names:
                    if debug:
                        rejected.append({"title": title, "url": job_url, "selector": "ashby_api", "reason": f"Dept '{job_dept}' / team '{job_team}' not in filter {group_names}"})
                    continue

            job_loc = (posting.get("location") or "").strip()
            # A posting open in several places carries the extra ones here; the
            # location filter has to read them too, or a New York filter drops
            # a posting whose primary line happens to say San Francisco.
            secondary = [(entry or {}).get("location")
                         for entry in (posting.get("secondaryLocations") or [])]
            if loc_names:
                places = [p for p in ([job_loc] + secondary) if isinstance(p, str) and p]
                if not any(ln.lower() in p.lower() for ln in loc_names for p in places):
                    if debug:
                        rejected.append({"title": title, "url": job_url, "selector": "ashby_api", "reason": f"Location '{job_loc}' not in filter {loc_names}"})
                    continue

            reason = _validate_job(title, job_url)
            if reason is None:
                # `workplaceType` is the arrangement. `isRemote` is not: a posting
                # with workplaceType "Hybrid" still reports isRemote true, so it
                # means "has a remote option", not "is a remote job".
                jobs.append({"title": title, "url": job_url,
                             "location": job_loc or None,
                             "locations": [x for x in ([job_loc] + secondary) if x],
                             "arrangement": posting.get("workplaceType") or None,
                             "id": posting.get("id"),
                             "canonical_url": posting.get("jobUrl"),
                             "apply_url": posting.get("applyUrl"),
                             "department": posting.get("department"),
                             "team": posting.get("team"),
                             "remote": posting.get("isRemote"),
                             "description": _description(posting),
                             "published_at": posting.get("publishedAt"),
                             "employment_type": posting.get("employmentType"),
                             **_salary(posting)})
            elif debug:
                rejected.append({"title": title, "url": job_url, "selector": "ashby_api", "reason": reason})

    logger.info(f"Ashby API: fetched {len(jobs)} jobs for {company_slug}")
    if debug:
        return jobs, rejected
    return jobs
