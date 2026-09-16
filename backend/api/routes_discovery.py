"""Job Preferences and Discovery: the endpoints that speak the user's intent.

`GET/PATCH /job-preferences` is what both the Jobs toolbar and the Settings page
write to — there is one document, so the two screens cannot disagree. The Jobs
screen never builds a `Search`; translating criteria into internal search work is
the Discovery Planner's job and stays behind `/discovery`.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from backend.discovery import engine, planner
from backend.discovery import preferences as P
from backend.job_monitor import JobAlreadyRunningError, launch_background
from backend.models.db import get_db

logger = logging.getLogger("jobnavigator.api.discovery")
router = APIRouter(tags=["discovery"])


# ── Job preferences ──────────────────────────────────────────────────────────

@router.get("/job-preferences")
def get_job_preferences(db: Session = Depends(get_db)):
    """The canonical preferences, the saved filters, and the vocabulary to render them."""
    return {
        "preferences": P.load(db),
        "saved_filters": P.load_filters(db),
        "taxonomy": P.taxonomy(),
    }


@router.patch("/job-preferences")
def patch_job_preferences(updates: dict, db: Session = Depends(get_db)):
    """Merge a partial change into the one preferences document.

    Changing a preference re-runs the cheap Preference Gate over the stored feed
    immediately, so the Recommended list agrees with what was just asked for.
    Candidate Fit is deliberately NOT recomputed: a narrower location says
    nothing new about how well the candidate's evidence matches a posting.
    """
    unknown = [k for k in (updates or {}) if k not in P.DEFAULTS]
    if unknown:
        raise HTTPException(400, f"Unknown preference: {', '.join(sorted(unknown))}")
    prefs = P.patch(db, updates)
    regated = engine.regate_all(db)
    return {"preferences": prefs, "regated": regated,
            "plan_size": len(planner.plan_for(db)["queries"])}


@router.put("/job-preferences/filters")
def put_saved_filters(body: dict, db: Session = Depends(get_db)):
    """Replace the saved-filter list. Criteria only — a filter has no notion of a
    source, a board, an interval or a scoring depth, and never will."""
    filters = body.get("filters") if isinstance(body, dict) else body
    if not isinstance(filters, list):
        raise HTTPException(400, "expected {\"filters\": [...]}")
    saved = P.save_filters(db, filters)
    regated = engine.regate_all(db)
    return {"saved_filters": saved, "regated": regated,
            "plan_size": len(planner.plan_for(db)["queries"])}


# ── Discovery ────────────────────────────────────────────────────────────────

@router.post("/discovery/refresh", status_code=202)
async def refresh_discovery():
    """"Find jobs using my current preferences", once, now.

    This is the only discovery button a normal user ever sees, and it does not
    mean "run this JobSpy configuration" — the plan is rebuilt from preferences
    every time it fires.
    """
    try:
        run_id = launch_background("discovery", engine.run_discovery, trigger="manual")
    except JobAlreadyRunningError as e:
        return JSONResponse(status_code=409,
                            content={"detail": f"discovery is already running ({e.elapsed_seconds:.0f}s)"})
    return {"run_id": run_id, "status": "running"}


@router.get("/discovery/status")
def discovery_status(db: Session = Depends(get_db)):
    """Plan, board health, gate rejects and analysis queue — the diagnostics view."""
    return engine.status(db)


@router.post("/discovery/regate")
def regate(db: Session = Depends(get_db)):
    """Re-derive every posting's metadata and re-run the Preference Gate over the
    whole stored feed. No LLM is involved.

    This is the repair action: unlike the re-gate a preference change triggers, it
    re-parses each title and description, so rows written by an older classifier
    are corrected rather than trusted.
    """
    return engine.regate_all(db, force_metadata=True)
