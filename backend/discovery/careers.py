"""Resolving a company's direct careers source from evidence already in hand.

The hard rule is in the name of the one thing this module refuses to do: it
never *invents* a URL. It does not guess `boards.greenhouse.io/<company-name>`
from a company called "Acme" and hope. It only ever derives a board root from a
posting URL the collectors actually returned — `.../acme/jobs/4012345678` is
evidence that Acme's Greenhouse board is `.../acme`, and nothing else here is.

When no posting carries a supported ATS URL, the company is recorded with
`direct_monitor_status = "unresolved"` and keeps getting its jobs from the
aggregators. That is a fine steady state, not a failure.
"""
import logging
from urllib.parse import urlparse

logger = logging.getLogger("jobnavigator.discovery.careers")

UNRESOLVED = "unresolved"
MONITORED = "monitored"


def _slug_root(url: str, depth: int = 1) -> str | None:
    """`https://host/a/b/c` -> `https://host/a` (depth 1), or None without one."""
    parsed = urlparse(url)
    parts = [p for p in (parsed.path or "").strip("/").split("/") if p]
    if len(parts) < depth:
        return None
    return f"{parsed.scheme or 'https'}://{parsed.netloc}/" + "/".join(parts[:depth])


def board_url_from_posting(url: str | None) -> tuple[str, str] | None:
    """``(board url, ATS label)`` derived from one posting URL, or None.

    Only the ATSes whose board root is literally a prefix of the posting URL are
    handled. Workday and Oracle bury the board behind a tenant/site pair that a
    single posting link does not always carry, so a posting on those is left for
    the user (or an already-configured URL) rather than half-guessed.
    """
    if not url or not str(url).strip():
        return None
    url = str(url).strip()
    try:
        from backend.scraper.ats.greenhouse import is_greenhouse
        from backend.scraper.ats.lever import is_lever
        from backend.scraper.ats.ashby import is_ashby
        from backend.scraper.ats.smartrecruiters import is_smartrecruiters
        from backend.scraper.ats.rippling import is_rippling
    except Exception as e:                     # pragma: no cover - import-time only
        logger.debug("ATS detectors unavailable: %s", e)
        return None

    for detect, label, depth in (
        (is_greenhouse, "Greenhouse", 1),
        (is_lever, "Lever", 1),
        (is_ashby, "Ashby", 1),
        (is_smartrecruiters, "SmartRecruiters", 1),
        (is_rippling, "Rippling", 1),
    ):
        try:
            if not detect(url):
                continue
        except Exception:
            continue
        root = _slug_root(url, depth)
        # A board root that is only the host is not a board — it is the ATS's
        # own marketing site, and scraping it would return nothing forever.
        if root and urlparse(root).path.strip("/"):
            return root, label
    return None


def resolve_from_job(job) -> tuple[str, str] | None:
    """The first supported board root any of this posting's URLs points at."""
    for field in ("canonical_url", "apply_url", "url", "source_url"):
        found = board_url_from_posting(getattr(job, field, None))
        if found:
            return found
    return None


def ats_label(url: str) -> str:
    """The human name of whatever handler would take this URL."""
    from backend.api.routes_companies import detect_scrape_type
    return detect_scrape_type(url)
