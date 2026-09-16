"""The Preference Gate: does this posting plausibly satisfy the user's criteria?

Cheap, deterministic, and run on every discovered posting before anything
expensive touches it. It exists so a Candidate Fit analysis — which costs an LLM
call — is only ever spent on a job the user could actually want.

The one rule that matters: **missing metadata is UNKNOWN, not false.** A posting
is rejected on evidence ("5+ years required", "Senior Staff Engineer", "Berlin"),
never on the absence of it. Everything the gate could not decide comes back in
`uncertain` so the diagnostics screen can show why a source is producing thin
metadata, and the posting still passes.
"""
from backend.discovery import taxonomy as T


def _get(job, name, default=None):
    value = getattr(job, name, default)
    return default if value is None else value


def derive_job_metadata(job) -> dict:
    """The gate's five derived fields for one posting, from title + description only.

    Stored on the Job row (so the feed can filter in SQL) but recomputed here
    whenever a column is still empty — a description that arrived later via
    enrichment must not be stuck with the metadata of the stub that preceded it.
    """
    title = _get(job, "title", "") or ""
    description = _get(job, "description", "") or ""
    min_years, max_years = T.parse_years(description)
    return {
        "job_function": T.classify_function(title, description),
        "experience_levels": T.classify_levels(title, description),
        "min_years_experience": min_years,
        "max_years_experience": max_years,
        "job_type": T.normalize_job_type(_get(job, "employment_type"), title),
    }


def apply_derived_metadata(job, force: bool = False) -> dict:
    """Fill the derived columns on a Job.

    `job_function` is always written (`other` is a real verdict, not a gap) —
    it is what marks the row as derived. The rest is only filled while still
    empty, so a later enrichment pass that found a real description can be
    re-run with `force=True` without discarding better data in between.
    """
    if not force and getattr(job, "job_function", None):
        return {"job_function": job.job_function,
                "experience_levels": T.unpack_levels(getattr(job, "experience_levels", None)),
                "job_type": getattr(job, "job_type", None),
                "min_years_experience": getattr(job, "min_years_experience", None),
                "max_years_experience": getattr(job, "max_years_experience", None)}
    derived = derive_job_metadata(job)
    job.job_function = derived["job_function"]
    if force or not getattr(job, "experience_levels", None):
        job.experience_levels = T.pack_levels(derived["experience_levels"])
    if force or not getattr(job, "job_type", None):
        job.job_type = derived["job_type"]
    if force or getattr(job, "min_years_experience", None) is None:
        job.min_years_experience = derived["min_years_experience"]
    if force or getattr(job, "max_years_experience", None) is None:
        job.max_years_experience = derived["max_years_experience"]
    return derived


def _metadata(job) -> dict:
    """Derived fields, preferring what is already stored on the row.

    `job_function` is the marker: derivation always writes it, so a row that has
    one has been through this once and its NULLs are real answers ("the posting
    states no years"), not gaps. Trusting it keeps a whole-feed re-gate off the
    description-scanning path — that is the difference between re-gating 20 000
    postings in a moment and in a minute.
    """
    title = _get(job, "title", "") or ""
    if getattr(job, "job_function", None):
        return {
            "job_function": job.job_function,
            "experience_levels": T.unpack_levels(getattr(job, "experience_levels", None)),
            "min_years_experience": getattr(job, "min_years_experience", None),
            "job_type": getattr(job, "job_type", None)
            or T.normalize_job_type(_get(job, "employment_type"), title),
        }
    derived = derive_job_metadata(job)
    return {
        "job_function": derived["job_function"],
        "experience_levels": derived["experience_levels"],
        "min_years_experience": derived["min_years_experience"],
        "job_type": derived["job_type"],
    }


def _work_models(job) -> list[str]:
    """The work models the posting actually claims; empty means no source resolved it."""
    return [key for key in T.WORK_MODEL_IDS if getattr(job, f"arr_{key}", None) is True]


def _company_names(job) -> set[str]:
    name = str(_get(job, "company", "") or "").strip().casefold()
    return {name} if name else set()


def evaluate(job, prefs: dict) -> dict:
    """``{"eligible": bool, "reasons": [...], "uncertain": [...]}`` for one posting.

    `reasons` are the criteria that REJECTED it (empty when eligible);
    `uncertain` are the criteria the posting carried no evidence for.
    """
    reasons, uncertain = [], []
    meta = _metadata(job)

    # ── company include / exclude ────────────────────────────────────────────
    names = _company_names(job)
    excluded = {c.strip().casefold() for c in prefs.get("excluded_companies") or [] if str(c).strip()}
    wanted = {c.strip().casefold() for c in prefs.get("companies") or [] if str(c).strip()}
    if names and excluded & names:
        reasons.append(f"company is on your excluded list ({_get(job, 'company')})")
    if wanted:
        if not names:
            uncertain.append("company is unknown, and you filtered to specific companies")
        elif not (wanted & names):
            reasons.append(f"company is not one of the {len(wanted)} you filtered to")

    # ── country ──────────────────────────────────────────────────────────────
    countries = [c.upper() for c in prefs.get("countries") or []]
    job_country = str(_get(job, "loc_country", "") or "").upper()
    if countries:
        if not job_country:
            uncertain.append("country could not be parsed from the location")
        elif job_country not in countries:
            reasons.append(f"located in {job_country}, not {'/'.join(countries)}")

    # ── location (state / region / city), free text ──────────────────────────
    places = [p.strip().casefold() for p in prefs.get("locations") or [] if str(p).strip()]
    if places:
        haystack = " ".join(str(_get(job, f, "") or "").casefold()
                            for f in ("location", "loc_region", "loc_city"))
        if not haystack.strip():
            uncertain.append("no location on the posting")
        elif not any(p in haystack or haystack in p for p in places):
            # A remote posting answers any place inside a country the user wants.
            if getattr(job, "arr_remote", None) is not True:
                reasons.append(f"not in {', '.join(prefs['locations'][:3])}")

    # ── job function ─────────────────────────────────────────────────────────
    functions = prefs.get("job_functions") or []
    if functions:
        wanted = ", ".join(T.FUNCTION_LABEL.get(f, f) for f in functions)
        if meta["job_function"] == "other" and "other" not in functions:
            # `other` is a verdict, not a gap: a title WAS read and matched none of
            # the functions asked for. Treating it as UNKNOWN let every sales, HR
            # and finance posting that leaks into a keyword result reach the
            # recommended feed — and start a company monitor behind it.
            reasons.append(f"the title does not read as {wanted}")
        elif meta["job_function"] not in functions:
            reasons.append(f"reads as {T.FUNCTION_LABEL.get(meta['job_function'], meta['job_function'])}")

    # ── employment level ─────────────────────────────────────────────────────
    levels = prefs.get("levels") or []
    if levels:
        if not meta["experience_levels"]:
            uncertain.append("the posting does not state a seniority")
        elif not set(meta["experience_levels"]) & set(levels):
            found = ", ".join(T.LEVEL_LABEL.get(lv, lv) for lv in meta["experience_levels"])
            reasons.append(f"seniority is {found}")

    # ── job type ─────────────────────────────────────────────────────────────
    types = prefs.get("job_types") or []
    if types:
        if not meta["job_type"]:
            uncertain.append("the posting does not state an employment type")
        elif meta["job_type"] not in types:
            reasons.append(f"is {T.JOB_TYPE_LABEL.get(meta['job_type'], meta['job_type'])}")

    # ── work model ───────────────────────────────────────────────────────────
    models = prefs.get("work_models") or []
    # All three selected is "any" — do not reject a posting for a fourth state.
    if models and len(models) < len(T.WORK_MODEL_IDS):
        claimed = _work_models(job)
        if not claimed:
            uncertain.append("on-site/hybrid/remote was not resolved")
        elif not set(claimed) & set(models):
            found = ", ".join(T.WORK_MODEL_LABEL.get(m, m) for m in claimed)
            reasons.append(f"is {found}")

    # ── years of experience ──────────────────────────────────────────────────
    ceiling = prefs.get("max_years_experience")
    if ceiling is not None:
        stated = meta["min_years_experience"]
        if stated is None:
            uncertain.append("the posting does not state years of experience")
        elif stated > ceiling:
            reasons.append(f"asks for {stated}+ years, you set a ceiling of {ceiling}")

    # ── salary ───────────────────────────────────────────────────────────────
    floor = prefs.get("minimum_salary")
    if floor:
        top = _get(job, "salary_max") or _get(job, "salary_min")
        if not top:
            uncertain.append("no compensation on the posting")
        elif str(_get(job, "salary_period", "yearly") or "yearly").lower() in ("yearly", "annual") and top < floor:
            reasons.append(f"pays up to {top}, below your {floor} floor")

    # ── sponsorship ──────────────────────────────────────────────────────────
    if prefs.get("sponsorship") == "required":
        verdict = str(_get(job, "h1b_verdict", "") or "").lower()
        if verdict == "unlikely":
            reasons.append("employer looks unlikely to sponsor")
        elif verdict != "likely":
            uncertain.append("sponsorship history is unknown for this employer")

    return {"eligible": not reasons, "reasons": reasons, "uncertain": uncertain}


def passes(job, prefs: dict) -> bool:
    return evaluate(job, prefs)["eligible"]


def evaluate_any(job, criteria_list) -> dict:
    """Eligible if the posting satisfies ANY of the active criteria documents.

    Saved filters are alternatives, not additional constraints: "Backend — New
    Grad — Seattle" OR "Data/ML — United States" must both fill the feed.
    """
    results = [evaluate(job, prefs) for prefs in (criteria_list or [])]
    if not results:
        return {"eligible": True, "reasons": [], "uncertain": []}
    for r in results:
        if r["eligible"]:
            return r
    # All rejected — report the narrowest explanation (the fewest complaints).
    return min(results, key=lambda r: len(r["reasons"]))
