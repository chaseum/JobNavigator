"""JobSpy-backed keyword search source — LinkedIn, Indeed, ZipRecruiter and Google Jobs, one killable subprocess per board.

One `scrape_jobs(site_name=[...])` call for every board gives JobNavigator no kill
boundary: python-jobspy 1.1.82 can hang paginating Indeed, and a wedged thread
cannot be killed from the event loop, so one stuck board stranded the whole
search. Each board now runs as `python -m backend.scraper.sources.jobspy` (see
`_worker_main` at the foot of this file), reached through `subprocess.run(timeout=)`
which kills the child when it overruns. A board that hangs, crashes or 403s costs
that board only; the rows every other board returned are still stored.
"""
import asyncio
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models.db import SessionLocal, Search, Job, Setting, get_existing_external_ids
from backend.scraper._shared.dedup import make_external_id, make_content_hash

logger = logging.getLogger("jobnavigator.scraper.sources.jobspy")


def _clean(v):
    """Null-safe scalar → clean string, or None when empty; a bare str(cell) on a pandas NaN yields the literal text 'nan', which passes emptiness checks downstream and broke resume tailoring."""
    if v is None:
        return None
    try:
        if math.isnan(v):
            return None
    except (TypeError, ValueError):
        pass  # non-scalar (list/array) — fall through and stringify
    s = str(v).strip()
    return None if not s or s.casefold() in {"nan", "none", "<na>", "nat"} else s


def _row_urls(row) -> tuple[str | None, str | None, str | None]:
    """(apply_url, source_url, canonical_url), preserving the original board URL."""
    source_url = _clean(row.get("job_url"))
    canonical_url = _clean(row.get("job_url_direct"))
    return canonical_url or source_url, source_url, canonical_url


def _row_salary(row) -> dict:
    """Keep JobSpy amounts in the returned currency and interval; do not annualize implicitly."""
    values = {}
    for key, field in (("salary_min", "min_amount"), ("salary_max", "max_amount")):
        raw = _clean(row.get(field))
        if raw:
            try:
                values[key] = round(float(raw))
            except (ValueError, TypeError):
                pass
    if not values:
        return {}
    origin = (_clean(row.get("salary_source")) or "").lower()
    values["salary_source"] = "jobspy_description" if origin == "description" else "jobspy_posting"
    values["salary_currency"] = _clean(row.get("currency"))
    values["salary_period"] = _clean(row.get("interval"))
    return values


def _apply_h1b_inline(job, db=None, company_lookup=None, phrases=None, loop=None) -> None:
    """Sync-safe H-1B JD scan: runs check_job_h1b in an event loop for asyncio.to_thread() workers; batch callers should pass a shared `loop` plus `company_lookup`/`phrases` to skip per-job DB lookups."""
    import asyncio as _asyncio
    from backend.analyzer.h1b_checker import check_job_h1b

    try:
        own_loop = loop is None
        if own_loop:
            loop = _asyncio.new_event_loop()
        try:
            loop.run_until_complete(check_job_h1b(job, db=db, company_lookup=company_lookup, phrases=phrases))
        finally:
            if own_loop:
                loop.close()
    except Exception as e:
        logger.warning(f"_apply_h1b_inline failed for job {getattr(job, 'id', '?')}: {e}")


# jobspy swallows per-board failures into its own loggers instead of the return
# value, so a hard-failed board looks identical to one that found nothing; capture those records here.

_JOBSPY_SITE_KEYS = {
    "linkedin": "linkedin",
    "indeed": "indeed",
    "ziprecruiter": "zip_recruiter",
    "google": "google",
    "glassdoor": "glassdoor",
    "bayt": "bayt",
    "naukri": "naukri",
    "bdjobs": "bdjobs",
}


# jobspy logs each board under two spellings (e.g. "LinkedIn" vs "Linkedin"); _site_key()
# lowercases both to the same key, so this map only needs the site values we configure.
_JOBSPY_LOGGER_DISPLAY = {
    "linkedin": "LinkedIn",
    "indeed": "Indeed",
    "zip_recruiter": "ZipRecruiter",
    "google": "Google",
    "glassdoor": "Glassdoor",
    "bayt": "Bayt",
    "naukri": "Naukri",
    "bdjobs": "BDJobs",
}


def _site_key(logger_name: str):
    """"JobSpy:ZipRecruiter" -> "zip_recruiter". None for non-site loggers."""
    name = str(logger_name or "")
    if ":" not in name:
        return None
    suffix = name.split(":", 1)[1].strip()
    if not suffix:
        return None
    flat = suffix.lower().replace("_", "").replace("-", "")
    return _JOBSPY_SITE_KEYS.get(flat, suffix.lower())


def _condense_error(msg: str) -> str:
    """"ZipRecruiter response status code 403" -> "403"; anything else trimmed."""
    text = " ".join(str(msg).split())
    m = re.search(r"\b([45]\d\d)\b", text)
    if m:
        return m.group(1)
    return text[:120]


class _SourceLogCapture(logging.Handler):
    """Keeps the first WARNING+ record each JobSpy board logger emits."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.errors = {}

    def emit(self, record):
        try:
            if not str(record.name).startswith("JobSpy"):
                return
            key = _site_key(record.name)
            if not key or key in self.errors:
                return
            self.errors[key] = _condense_error(record.getMessage())
        except Exception:  # a logging handler must never break the scrape
            pass


def _capture_targets(sites=None):
    """Every logger the capture handler must attach to for one scrape_jobs() call; jobspy's create_logger() sets propagate=False, so a handler on the root logger alone would miss board records."""
    names = set()
    for raw in list(logging.Logger.manager.loggerDict):
        if str(raw).startswith("JobSpy"):
            names.add(str(raw))
    for site in (sites or []):
        display = _JOBSPY_LOGGER_DISPLAY.get(str(site).lower())
        if display:
            names.add(f"JobSpy:{display}")
    return [logging.getLogger()] + [logging.getLogger(n) for n in sorted(names)]


@contextmanager
def _capture_source_errors(sites=None):
    """Attach the capture handler to every JobSpy board logger for one call, without touching level/propagate since that would leak into other requests sharing the process."""
    handler = _SourceLogCapture()
    targets = _capture_targets(sites)
    for lg in targets:
        lg.addHandler(handler)
    try:
        yield handler
    finally:
        for lg in targets:
            lg.removeHandler(handler)


def _merge_source_errors(breakdown: dict, errors: dict) -> dict:
    """Fold captured per-board errors into the seen/new breakdown."""
    for key, err in (errors or {}).items():
        breakdown.setdefault(key, {"seen": 0, "new": 0})["error"] = err
    return breakdown


# ── per-board isolation ──────────────────────────────────────────────────────

DEFAULT_BOARD_TIMEOUT = 150.0
# jobspy's own site spelling, so a board's error and its `seen` count land on the
# same breakdown key ("ziprecruiter" is configured, but rows come back as "zip_recruiter").
def board_key(board: str) -> str:
    flat = str(board or "").lower().replace("_", "").replace("-", "")
    return _JOBSPY_SITE_KEYS.get(flat, str(board or "").lower())


def board_timeout(db) -> float:
    """Seconds one board may run before it is killed. Setting `jobspy_board_timeout`."""
    try:
        value = float(get_setting_value(db, "jobspy_board_timeout", "") or DEFAULT_BOARD_TIMEOUT)
    except (TypeError, ValueError):
        return DEFAULT_BOARD_TIMEOUT
    return value if value > 0 else DEFAULT_BOARD_TIMEOUT


def _scrape_board_sync(board: str, kwargs: dict, timeout: float) -> tuple[list, str | None]:
    """(rows, error) for one board, run in a subprocess that is killed if it overruns."""
    payload = json.dumps({"board": board, "kwargs": kwargs})
    # backend/scraper/sources/jobspy.py -> the repo root, so `-m backend...` resolves in the child
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in (root, os.environ.get("PYTHONPATH", "")) if p)}
    try:
        proc = subprocess.run([sys.executable, "-m", __name__], input=payload.encode(),
                              capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        # subprocess.run has already killed the child; a wedged board stops here
        logger.warning(f"JobSpy board {board} timed out after {timeout:.0f}s and was killed")
        return [], f"timed out after {int(timeout)}s"
    except Exception as e:
        return [], _condense_error(f"worker failed: {e}")
    if proc.returncode != 0:
        tail = " ".join(proc.stderr.decode(errors="replace").split())[-200:]
        return [], _condense_error(tail or f"worker exited {proc.returncode}")
    try:
        out = json.loads(proc.stdout.decode(errors="replace") or "{}")
    except ValueError:
        return [], "worker returned no usable result"
    return out.get("rows") or [], out.get("error")


def scrape_boards(boards: list, kwargs: dict, timeout: float) -> tuple[list, dict]:
    """(all rows, {board_key: error}) with every board run independently and concurrently.

    Threads, not the event loop: each one only waits on its own subprocess, and
    `_run_sync` is plain synchronous code that a caller may already be running
    inside a loop (asyncio.run would raise there).
    """
    with ThreadPoolExecutor(max_workers=max(1, len(boards))) as pool:
        results = list(pool.map(lambda b: _scrape_board_sync(b, kwargs, timeout), boards))
    rows, errors = [], {}
    for board, (board_rows, error) in zip(boards, results):
        rows.extend(board_rows)
        if error:
            errors[board_key(board)] = error
    return rows, errors


def get_setting_value(db: Session, key: str, default: str = "") -> str:
    """Read a single Setting row's value by key, returning ``default`` if not set."""
    row = db.query(Setting).filter(Setting.key == key).first()
    return row.value if row else default


def apply_title_filters(jobs_df, include_keywords: list, exclude_keywords: list):
    """Filter jobs by title include/exclude keywords (whole-word matching); returns (kept_df, rejected_df)."""
    import pandas as pd

    if jobs_df is None or jobs_df.empty:
        return jobs_df, pd.DataFrame()

    mask = pd.Series(True, index=jobs_df.index)

    if include_keywords:
        pattern = "|".join(include_keywords)
        mask &= jobs_df["title"].str.contains(pattern, case=False, na=False)

    if exclude_keywords:
        pattern = "|".join(r'\b' + re.escape(kw) + r'\b' for kw in exclude_keywords)
        mask &= ~jobs_df["title"].str.contains(pattern, case=False, na=False, regex=True)

    return jobs_df[mask], jobs_df[~mask]


def apply_company_filter(jobs_df, company_filter: list):
    """Filter to specific companies if filter is non-empty (exact match, case-insensitive)."""
    if not company_filter or jobs_df is None or jobs_df.empty:
        return jobs_df
    cf_set = {cf.lower() for cf in company_filter}
    return jobs_df[jobs_df["company"].str.lower().isin(cf_set)]


def _run_sync(search, proxy_url: str = None) -> dict:
    """Execute a single JobSpy search and return results dict."""
    start_time = time.time()
    # One entry per configured board so a failure is visible next to boards that worked;
    # seeded here (not after the call) so failure paths can still say what was asked for.
    breakdown = {}

    try:
        import pandas as pd

        # Build source list — filter out 'direct' which is Playwright
        sources = [s for s in (search.sources or []) if s != "direct"]
        if not sources:
            return {"jobs_found": 0, "new_jobs": 0, "ignored_jobs": 0,
                    "error": "No JobSpy sources configured", "source_breakdown": {}}
        breakdown = {board_key(s): {"seen": 0, "new": 0} for s in sources}

        kwargs = {
            "search_term": search.search_term or "",
            "location": search.location or "United States",
            "results_wanted": search.results_wanted or 50,
            "hours_old": search.hours_old or 24,
            # Boards take ONE employment type. Omitting it means "any", which is
            # what a preference set naming several of them needs; forcing
            # "fulltime" here is what used to make a Full-time + Internship
            # preference return no internships at all.
            "country_indeed": getattr(search, "country_indeed", None) or "USA",
            "verbose": 2,
        }
        if search.job_type:
            kwargs["job_type"] = search.job_type

        if search.is_remote is not None:
            kwargs["is_remote"] = search.is_remote

        if proxy_url:
            kwargs["proxies"] = [proxy_url]

        db_timeout = SessionLocal()
        try:
            timeout = board_timeout(db_timeout)
        finally:
            db_timeout.close()

        logger.info(f"Running JobSpy search: {search.name} — term='{search.search_term}', sources={sources} "
                    f"(one subprocess per board, {timeout:.0f}s each)")
        rows, board_errors = scrape_boards(sources, kwargs, timeout)
        _merge_source_errors(breakdown, board_errors)
        jobs_df = pd.DataFrame(rows) if rows else None

        if jobs_df is None or jobs_df.empty:
            duration = time.time() - start_time
            return {"jobs_found": 0, "new_jobs": 0, "ignored_jobs": 0, "error": None,
                    "duration": duration, "source_breakdown": breakdown}

        # Apply filters (merge global title exclude with per-search)
        db_excl = SessionLocal()
        try:
            global_title_excl = json.loads(get_setting_value(db_excl, "title_exclude_global", "[]"))
        except Exception:
            global_title_excl = []
        finally:
            db_excl.close()
        merged_exclude = list(set((search.title_exclude_keywords or []) + global_title_excl))
        jobs_df, rejected_df = apply_title_filters(
            jobs_df,
            search.title_include_keywords or [],
            merged_exclude,
        )
        jobs_df = apply_company_filter(jobs_df, search.company_filter or [])

        # Company exclude (global=full match, per-search=full match,
        # plus active companies when search.exclude_active_companies is on)
        db_excl = SessionLocal()
        try:
            from backend.scraper._shared.filters import build_search_exclude_sets
            global_exclude_set, search_exclude_set = build_search_exclude_sets(db_excl, search)
            if (global_exclude_set or search_exclude_set) and jobs_df is not None and not jobs_df.empty:
                before = len(jobs_df)
                def _excl(name):
                    nl = str(name).lower()
                    if nl in global_exclude_set:
                        return True
                    return nl in search_exclude_set
                mask = jobs_df["company"].apply(_excl)
                jobs_df = jobs_df[~mask]
                if len(jobs_df) < before:
                    logger.info(f"Company exclude removed {before - len(jobs_df)} jobs")
        finally:
            db_excl.close()

        jobs_found = len(jobs_df)

        # Per-board `seen` counted after filtering, so sum(seen) == jobs_found.
        if jobs_df is not None and not jobs_df.empty and "site" in jobs_df.columns:
            for site_name, count in jobs_df["site"].value_counts().items():
                key = str(site_name).lower()
                breakdown.setdefault(key, {"seen": 0, "new": 0})["seen"] = int(count)

        # Save to DB, dedup via external_id
        db = SessionLocal()
        new_jobs = 0
        # Rejected rows are still written as status "ignored" so the next run dedups them;
        # count them too, since otherwise storing 8 rows could report only "6 seen, +6 new".
        ignored_jobs = 0
        # Hoisted per run (not per job): one event loop, one company lookup, one parsed phrase list —
        # avoids a fresh loop + full company scan + Settings JSON-parse thousands of times per scrape.
        import asyncio as _asyncio
        from backend.models.db import build_company_lookup
        from backend.analyzer.h1b_checker import load_exclusion_phrases
        h1b_loop = _asyncio.new_event_loop()
        try:
            existing_ids = get_existing_external_ids(db)
            company_lookup = build_company_lookup(db)
            phrases = load_exclusion_phrases(db)

            for _, row in jobs_df.iterrows():
                company = _clean(row.get("company")) or ""
                title = _clean(row.get("title")) or ""
                url, source_url, canonical_url = _row_urls(row)
                url = url or ""
                source_url = source_url or ""
                ext_id = make_external_id(company, title, source_url)

                if ext_id in existing_ids:
                    continue

                content_hash = make_content_hash(company, title)

                site = str(row.get("site", "")).lower()
                source_map = {
                    "linkedin": "jobspy_linkedin",
                    "indeed": "jobspy_indeed",
                    "zip_recruiter": "jobspy_zip_recruiter",
                    "google": "jobspy_google",
                }
                source = source_map.get(site, f"jobspy_{site}")

                job = Job(
                    external_id=ext_id,
                    content_hash=content_hash,
                    company=company,
                    title=title,
                    url=url,
                    source_url=source_url,
                    canonical_url=canonical_url,
                    source=source,
                    search_id=search.id,
                    description=_clean(row.get("description")),
                    description_source="jobspy_posting" if _clean(row.get("description")) else None,
                    location=_clean(row.get("location")),
                    # JobSpy's own `is_remote` is a substring test over the whole
                    # description, so "remote state" (Terraform) and "remote dev
                    # environments" mark a job remote. It is deliberately unused;
                    # `work_from_home_type` below is Indeed's structured field and
                    # is trustworthy. Everything else falls to the JD cascade.
                    remote=None,
                    status="new",
                    seen=False,
                    saved=False,
                )

                # Extract salary if present in JobSpy results
                for key, value in _row_salary(row).items():
                    setattr(job, key, value)

                # H-1B check + salary extraction inline, using the shared loop/lookup/phrases hoisted above.
                _apply_h1b_inline(job, db, company_lookup=company_lookup, phrases=phrases, loop=h1b_loop)
                try:
                    from backend.analyzer.salary_extractor import apply_salary_to_job
                    from backend.analyzer.work_arrangement import apply_arrangement_to_job
                    from backend.analyzer.location import apply_location_to_job
                    company_obj = company_lookup.get(company.strip().lower())
                    apply_salary_to_job(job, getattr(job, "_h1b_median", None))
                    wfh = row.get("work_from_home_type")
                    apply_arrangement_to_job(
                        job, structured=wfh if wfh and str(wfh) != "nan" else None)
                    apply_location_to_job(job)
                except Exception as analysis_err:
                    logger.warning(f"Inline salary analysis failed for {title}: {analysis_err}")

                if job.h1b_jd_flag:
                    _phrase = getattr(job, "_h1b_matched_phrase", None) or "?"
                    logger.info(f"Skipping job (body exclusion): {title} @ {company} — matched phrase: {_phrase!r}")
                    continue

                try:
                    with db.begin_nested():
                        db.add(job)
                        db.flush()
                    new_jobs += 1
                    breakdown.setdefault(site or "unknown", {"seen": 0, "new": 0})["new"] += 1
                    existing_ids.add(ext_id)
                except IntegrityError:
                    logger.debug(f"Duplicate external_id for '{title}' at {company}, skipping")
                    continue
                except Exception as e:
                    logger.warning(f"Insert failed for '{title}' at {company} ({url}): {e}")
                    continue

            # Save filtered-out jobs as "ignored" for dedup purposes
            if rejected_df is not None and not rejected_df.empty:
                for _, row in rejected_df.iterrows():
                    company = _clean(row.get("company")) or ""
                    title = _clean(row.get("title")) or ""
                    url, source_url, canonical_url = _row_urls(row)
                    url = url or ""
                    source_url = source_url or ""
                    ext_id = make_external_id(company, title, source_url)

                    if ext_id in existing_ids:
                        continue

                    site = str(row.get("site", "")).lower()
                    source_map = {
                        "linkedin": "jobspy_linkedin",
                        "indeed": "jobspy_indeed",
                        "zip_recruiter": "jobspy_zip_recruiter",
                        "google": "jobspy_google",
                    }
                    source = source_map.get(site, f"jobspy_{site}")

                    job = Job(
                        external_id=ext_id,
                        company=company,
                        title=title,
                        url=url,
                        source_url=source_url,
                        canonical_url=canonical_url,
                        source=source,
                        search_id=search.id,
                        description=_clean(row.get("description")),
                        description_source="jobspy_posting" if _clean(row.get("description")) else None,
                        location=_clean(row.get("location")),
                        status="ignored",
                        seen=False,
                        saved=False,
                    )
                    try:
                        with db.begin_nested():
                            db.add(job)
                            db.flush()
                        existing_ids.add(ext_id)
                        ignored_jobs += 1
                        entry = breakdown.setdefault(site or "unknown", {"seen": 0, "new": 0})
                        entry["filtered"] = entry.get("filtered", 0) + 1
                    except IntegrityError:
                        continue
                    except Exception as e:
                        logger.warning(f"Insert failed for ignored job '{title}' at {company} ({url}): {e}")
                        continue

            db.commit()

            search_obj = db.query(Search).filter(Search.id == search.id).first()
            if search_obj:
                search_obj.last_run_at = datetime.now(timezone.utc)
                db.commit()

        finally:
            h1b_loop.close()
            db.close()

        duration = time.time() - start_time

        from backend.activity import log_activity
        log_activity("scrape", f"JobSpy search '{search.name}': {new_jobs} new / {jobs_found} found"
                     + (f" ({ignored_jobs} filtered out)" if ignored_jobs else "")
                     + f" in {duration:.1f}s")

        return {"jobs_found": jobs_found, "new_jobs": new_jobs, "ignored_jobs": ignored_jobs,
                "error": None, "duration": duration, "source_breakdown": breakdown}

    except Exception as e:
        duration = time.time() - start_time
        logger.error(f"JobSpy search failed for '{search.name}': {e}")

        from backend.activity import log_activity
        log_activity("scrape", f"JobSpy search '{search.name}' failed: {e}")

        return {"jobs_found": 0, "new_jobs": 0, "ignored_jobs": 0, "error": str(e),
                "duration": duration, "source_breakdown": breakdown}


async def run(search, proxy_url: str = None) -> dict:
    """Async entry point — offloads the synchronous JobSpy call to a thread."""
    return await asyncio.to_thread(_run_sync, search, proxy_url)


# ── the board worker (`python -m backend.scraper.sources.jobspy`) ────────────
# Runs ONE board and writes {"rows": [...], "error": ...} to stdout. It is a
# separate process so the parent can kill it; nothing here touches the database.

def _worker_main() -> int:
    request = json.loads(sys.stdin.read() or "{}")
    board = request.get("board") or ""
    kwargs = {**(request.get("kwargs") or {}), "site_name": [board]}
    rows, error = [], None
    try:
        from jobspy import scrape_jobs
        with _capture_source_errors([board]) as capture:
            df = scrape_jobs(**kwargs)
        # jobspy swallows a hard board failure into its own logger, so "no rows"
        # and "403" look identical in the return value; the capture tells them apart.
        error = capture.errors.get(board_key(board)) or next(iter(capture.errors.values()), None)
        if df is not None and not df.empty:
            rows = [{k: _clean(v) for k, v in record.items()} for record in df.to_dict("records")]
            for r in rows:
                r.setdefault("site", board_key(board))
    except Exception as e:
        error = _condense_error(f"{type(e).__name__}: {e}")
    sys.stdout.write(json.dumps({"rows": rows, "error": error}))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker_main())
