"""The Discovery Engine: execute the current plan, then let the pipeline finish.

    preferences -> planner -> collectors -> canonical jobs
                                 |
                                 v
                        Preference Gate (cheap, deterministic)
                           |                     |
                        rejected              passed
                                                 |
                                   +-------------+-------------+
                                   v                           v
                        Candidate Fit queue          company auto-monitoring

Nothing in here is a new mechanism. Collection is the existing JobSpy adapter
with its per-board subprocess isolation intact, analysis is the existing
Candidate Fit run behind the existing `scoring` concurrency limiter, and company
scraping is the existing scheduler sweep over active companies. What is new is
that the user no longer has to drive any of it.
"""
import asyncio
import logging
from datetime import timedelta, timezone

from backend.models.db import (
    Company, Job, JobAnalysisRecord, ScrapeLog, SessionLocal, Setting, utcnow,
)
from backend.discovery import careers, gate, planner
from backend.discovery import preferences as P

logger = logging.getLogger("jobnavigator.discovery.engine")

# One cycle's ceiling on LLM work. Discovering 400 postings must not turn into
# 400 analyses; the newest and best-matching are worth the money, the tail is not.
MAX_ANALYSES_PER_CYCLE = 40
# How many plan queries run at once. Each one already forks a subprocess per
# board, so this multiplies — three is a working machine, not a fork bomb.
MAX_CONCURRENT_QUERIES = 3
# Companies resolved per cycle. Resolution is cheap (string work on URLs we
# already have), but creating rows without bound is how a Companies list becomes
# ten thousand names nobody asked for.
MAX_COMPANIES_PER_CYCLE = 50

_SETTING_ENABLED = "auto_analysis_enabled"


def _setting(db, key: str, default: str = "") -> str:
    row = db.query(Setting).filter(Setting.key == key).first()
    return row.value if row and row.value is not None else default


def auto_analysis_enabled(db) -> bool:
    return str(_setting(db, _SETTING_ENABLED, "true")).strip().lower() not in ("false", "0", "no", "off")


# ── 1. Collection ────────────────────────────────────────────────────────────

async def _run_query(query: dict, boards: list, proxy_url, prefs: dict) -> dict:
    """One plan query through the JobSpy adapter, logged like any other source."""
    from backend.scraper.sources.jobspy import run as jobspy_run

    search = planner.to_search(
        query, boards=boards,
        excluded_companies=prefs.get("excluded_companies"),
        companies=prefs.get("companies"),
    )
    try:
        result = await jobspy_run(search, proxy_url=proxy_url)
    except Exception as e:
        # One query failing is one query failing. The rest of the plan, and
        # every board inside the other queries, still runs.
        logger.exception("Discovery query %r failed: %s", query.get("search_term"), e)
        result = {"jobs_found": 0, "new_jobs": 0, "error": str(e), "duration": 0}

    db = SessionLocal()
    try:
        from backend.scraper.orchestrator import source_errors
        breakdown = result.get("source_breakdown") or None
        db.add(ScrapeLog(
            search_id=None,          # discovery owns no Search row
            source="discovery",
            jobs_found=result.get("jobs_found", 0),
            new_jobs=result.get("new_jobs", 0),
            error=result.get("error"),
            is_warning=bool(source_errors(breakdown)
                            or (result.get("jobs_found", 0) == 0 and not result.get("error"))),
            source_breakdown=breakdown,
            duration_seconds=result.get("duration", 0),
        ))
        db.commit()
    finally:
        db.close()
    return result


async def collect(plan: dict, prefs: dict) -> dict:
    """Run every query in a plan; returns the run totals."""
    db = SessionLocal()
    try:
        proxy_url = _setting(db, "proxy_url", "") or None
    finally:
        db.close()

    queries = plan.get("queries") or []
    boards = plan.get("boards") or planner.DEFAULT_BOARDS
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_QUERIES)

    async def _bounded(q):
        async with semaphore:
            return await _run_query(q, boards, proxy_url, prefs)

    results = await asyncio.gather(*(_bounded(q) for q in queries), return_exceptions=True)
    found = new = failed = 0
    for r in results:
        if isinstance(r, Exception) or not isinstance(r, dict):
            failed += 1
            continue
        found += r.get("jobs_found", 0) or 0
        new += r.get("new_jobs", 0) or 0
        if r.get("error"):
            failed += 1
    return {"queries": len(queries), "jobs_found": found, "new_jobs": new, "failed_queries": failed}


# ── 2. Preference Gate ───────────────────────────────────────────────────────

def _store_verdict(job, verdict: dict) -> None:
    job.gate_ok = verdict["eligible"]
    job.gate_reasons = {"reasons": verdict["reasons"], "uncertain": verdict["uncertain"]}


def gate_jobs(db, jobs, criteria=None, force_metadata: bool = False) -> dict:
    """Score a list of postings against the active criteria and store the verdicts.

    `force_metadata` re-parses title and description instead of trusting the
    stored columns. Off for a preference change — the posting has not changed,
    so re-reading it would be wasted work. On for the explicit repair action,
    which is what fixes rows derived by an older, worse classifier.
    """
    criteria = criteria if criteria is not None else P.active_criteria(db)
    passed = rejected = 0
    for job in jobs:
        gate.apply_derived_metadata(job, force=force_metadata)
        verdict = gate.evaluate_any(job, criteria)
        _store_verdict(job, verdict)
        if verdict["eligible"]:
            passed += 1
        else:
            rejected += 1
    db.commit()
    return {"passed": passed, "rejected": rejected}


def gate_pending(db, limit: int = 2000) -> dict:
    """Gate every posting that has never been gated (new arrivals)."""
    jobs = (db.query(Job)
            .filter(Job.gate_ok.is_(None), Job.status.notin_(["ignored", "skip"]))
            .order_by(Job.discovered_at.desc()).limit(limit).all())
    return gate_jobs(db, jobs)


def regate_all(db, limit: int = 20000, force_metadata: bool = False) -> dict:
    """Re-run the CHEAP gate over every stored posting.

    Called when preferences change. It deliberately does not touch Candidate
    Fit: the user narrowing a location has not changed how well their evidence
    matches any posting, and re-running analyses on a filter change is exactly
    the runaway cost this gate exists to prevent.
    """
    jobs = (db.query(Job).filter(Job.status.notin_(["ignored", "skip"]))
            .order_by(Job.discovered_at.desc()).limit(limit).all())
    return gate_jobs(db, jobs, force_metadata=force_metadata)


# ── 3. Candidate Fit queue ───────────────────────────────────────────────────

def _needs_analysis(db, job_ids) -> set:
    """Of these postings, the ones with no Candidate Fit result yet."""
    if not job_ids:
        return set()
    analyzed = {r[0] for r in db.query(JobAnalysisRecord.job_id)
                .filter(JobAnalysisRecord.job_id.in_(list(job_ids))).all()}
    return set(job_ids) - analyzed


def queue_candidate_fit(db, limit: int = MAX_ANALYSES_PER_CYCLE) -> int:
    """Queue Candidate Fit for gate-passed postings that have never had one.

    Priority is newest first, then the postings whose gate verdict had the least
    left over as UNKNOWN — a posting we are sure about is worth analysing before
    one we had to guess at. Concurrency is NOT managed here: `launch_background`
    takes the existing `scoring` limiter before the worker opens its first
    session, so queueing forty costs forty coroutines and never forty LLM calls.
    """
    if not auto_analysis_enabled(db):
        logger.info("Automatic job analysis is off — nothing queued")
        return 0

    from backend.copilot.analysis import run_analysis
    from backend.job_monitor import JobAlreadyRunningError, launch_background

    candidates = (db.query(Job)
                  .filter(Job.gate_ok.is_(True), Job.status.in_(["new", "saved"]))
                  .order_by(Job.discovered_at.desc())
                  .limit(limit * 4).all())
    pending = _needs_analysis(db, [j.id for j in candidates])
    ranked = sorted(
        (j for j in candidates if j.id in pending),
        key=lambda j: (len((j.gate_reasons or {}).get("uncertain") or []),
                       -(j.discovered_at or utcnow()).timestamp()),
    )

    queued = 0
    for job in ranked:
        if queued >= limit:
            break
        # Analysis needs something to read. A posting with neither text nor a
        # URL would fail every time and burn a run slot doing it.
        readable = any((getattr(job, f, None) or "").strip()
                       for f in ("description", "canonical_url", "url", "source_url"))
        if not readable:
            continue
        try:
            launch_background("copilot_analyze", run_analysis, trigger="discovery",
                              scope_key=str(job.id), target_job_id=job.id,
                              func_kwargs={"job_id": str(job.id)})
            queued += 1
        except JobAlreadyRunningError:
            continue
        except Exception:
            # One job that could not be queued costs that job only — the posting
            # itself is already stored and stays exactly as it is, and the next
            # cycle will pick it up again. Logged loudly rather than swallowed:
            # this is how an LLM outage becomes visible instead of looking like
            # a quiet day with nothing to analyse.
            logger.exception("could not queue Candidate Fit for job %s", job.id)
            continue
    if queued:
        logger.info("Queued %d job(s) for Candidate Fit", queued)
    return queued


# ── 4. Company auto-discovery and monitoring ─────────────────────────────────

def reconcile_companies(db, limit: int = MAX_COMPANIES_PER_CYCLE) -> dict:
    """Canonicalise the companies behind gate-passed postings and monitor what we can.

    Only postings that PASSED the gate get a company row: a preference-irrelevant
    employer observed once in a keyword result is not something to start scraping
    every half hour (spec: relevance rules, not "every name ever seen").
    """
    from backend.models.db import get_company_all_names

    known = get_company_all_names(db)          # {lowercased name or alias: Company}
    recent = (db.query(Job)
              .filter(Job.gate_ok.is_(True), Job.company.isnot(None))
              .order_by(Job.discovered_at.desc()).limit(limit * 20).all())

    created = resolved = 0
    touched: set[str] = set()
    for job in recent:
        name = (job.company or "").strip()
        key = name.casefold()
        if not name or key in touched:
            continue
        touched.add(key)
        if created + resolved >= limit:
            break

        company = known.get(key)
        if company is None:
            company = Company(name=name, active=False, tier=None, playwright_enabled=False,
                              scrape_urls=[], auto_discovered=True)
            db.add(company)
            db.flush()
            known[key] = company
            created += 1

        # A company the user configured is theirs. Never rewrite its URLs, never
        # switch its monitoring off, never re-resolve it.
        if company.scrape_urls or company.direct_monitor_status:
            continue

        found = careers.resolve_from_job(job)
        if not found:
            # Record that we looked, so the next posting from this employer does
            # not send us round the same loop again.
            company.direct_monitor_status = careers.UNRESOLVED
            continue

        board_url, label = found
        company.scrape_urls = [board_url]
        company.direct_monitor_status = careers.MONITORED
        company.active = True
        resolved += 1
        logger.info("Monitoring %s via %s: %s", company.name, label, board_url)

    db.commit()
    return {"companies_created": created, "companies_monitored": resolved}


# ── 5. The whole cycle ───────────────────────────────────────────────────────

def post_collection() -> dict:
    """Everything that happens to a posting AFTER some collector stored it.

    Shared by discovery and by the company/search sweep, because a job found by
    a company scrape must meet exactly the same preference gate — and earn its
    Candidate Fit the same way — as one found on a job board.
    """
    db = SessionLocal()
    try:
        gated = gate_pending(db)
        companies = reconcile_companies(db)
        queued = queue_candidate_fit(db)
    finally:
        db.close()
    return {**gated, **companies, "queued": queued}


async def run_discovery(force: bool = False) -> str:
    """One full discovery cycle. Returns the one-line summary the run history shows."""
    db = SessionLocal()
    try:
        criteria = P.active_criteria(db)
        prefs = criteria[0]
        current = planner.plan(criteria)
    finally:
        db.close()

    if not current["queries"]:
        return "No job preferences set — nothing to discover"

    totals = await collect(current, prefs)
    stage = post_collection()
    gated = {"passed": stage["passed"], "rejected": stage["rejected"]}
    companies = {"companies_monitored": stage["companies_monitored"]}
    queued = stage["queued"]

    parts = [f"{totals['queries']} queries", f"+{totals['new_jobs']} new"]
    if gated["passed"] or gated["rejected"]:
        parts.append(f"{gated['passed']} match, {gated['rejected']} filtered")
    if companies["companies_monitored"]:
        parts.append(f"{companies['companies_monitored']} companies now monitored")
    if queued:
        parts.append(f"{queued} queued for Candidate Fit")
    if totals["failed_queries"]:
        parts.append(f"{totals['failed_queries']} failed")
    return " · ".join(parts)


def status(db) -> dict:
    """Everything the developer diagnostics screen shows about discovery."""
    from backend.scraper.orchestrator import source_errors

    current = planner.plan_for(db)
    since = utcnow() - timedelta(hours=24)
    logs = (db.query(ScrapeLog).filter(ScrapeLog.source == "discovery")
            .order_by(ScrapeLog.ran_at.desc()).limit(40).all())
    boards: dict[str, dict] = {}
    for row in logs:
        for key, value in (row.source_breakdown or {}).items():
            entry = boards.setdefault(key, {"seen": 0, "new": 0, "errors": []})
            if isinstance(value, dict):
                entry["seen"] += int(value.get("seen") or 0)
                entry["new"] += int(value.get("new") or 0)
                if value.get("error"):
                    entry["errors"].append(str(value["error"]))

    gate_rejects = (db.query(Job)
                    .filter(Job.gate_ok.is_(False))
                    .order_by(Job.discovered_at.desc()).limit(25).all())

    import backend.job_monitor as monitor
    in_flight = [r.job_type for r in monitor._running.values()]

    return {
        "plan": current,
        "preferences": P.load(db),
        "saved_filters": P.load_filters(db),
        "auto_analysis_enabled": auto_analysis_enabled(db),
        "last_run_at": logs[0].ran_at.isoformat() if logs and logs[0].ran_at else None,
        "runs_24h": sum(1 for r in logs if r.ran_at and r.ran_at.replace(
            tzinfo=r.ran_at.tzinfo or timezone.utc) >= since),
        "boards": boards,
        "source_errors": [dict(source=k, error=e) for row in logs
                          for k, e in source_errors(row.source_breakdown)][:20],
        "counts": {
            "total": db.query(Job).count(),
            "gate_passed": db.query(Job).filter(Job.gate_ok.is_(True)).count(),
            "gate_rejected": db.query(Job).filter(Job.gate_ok.is_(False)).count(),
            "ungated": db.query(Job).filter(Job.gate_ok.is_(None)).count(),
            "analyzed": db.query(JobAnalysisRecord.job_id).distinct().count(),
            "monitored_companies": db.query(Company).filter(
                Company.direct_monitor_status == careers.MONITORED).count(),
            "unresolved_companies": db.query(Company).filter(
                Company.direct_monitor_status == careers.UNRESOLVED).count(),
        },
        "analysis_in_flight": sum(1 for t in in_flight if t.startswith("copilot_")),
        "gate_rejects": [
            {"id": str(j.id), "title": j.title, "company": j.company,
             "reasons": (j.gate_reasons or {}).get("reasons") or []}
            for j in gate_rejects
        ],
    }
