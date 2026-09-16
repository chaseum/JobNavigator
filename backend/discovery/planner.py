"""Discovery Planner: Job Preferences in, internal search work out.

Deterministic and side-effect free — the same preferences always produce the
same plan, which is what makes it testable and what stops the scheduler from
drifting into a different set of queries every half hour.

The plan is INTERNAL. Nothing here is a product concept: the user never sees a
query, never names a board, and never learns that "Software Engineering" became
five search terms. They said what they want; this decides how to go and get it.
"""
import logging

from backend.discovery import taxonomy as T

logger = logging.getLogger("jobnavigator.discovery.planner")

# A preferences document with four functions and six cities would otherwise
# expand to well over a hundred board calls per cycle, each of which is a
# subprocess. The cap is a bound on cost, not a quality knob.
MAX_QUERIES = 24
RESULTS_PER_QUERY = 50
DEFAULT_BOARDS = ["linkedin", "indeed", "zip_recruiter", "google"]

# Board-facing place names. A country the user picks that is not in here still
# works — the code itself is a location string every board accepts.
COUNTRY_PLACES = {
    "US": ("United States", "USA"),
    "CA": ("Canada", "Canada"),
    "GB": ("United Kingdom", "UK"),
    "UK": ("United Kingdom", "UK"),
    "DE": ("Germany", "Germany"),
    "IN": ("India", "India"),
    "AU": ("Australia", "Australia"),
    "IE": ("Ireland", "Ireland"),
    "NL": ("Netherlands", "Netherlands"),
    "SG": ("Singapore", "Singapore"),
}


def _places(prefs: dict) -> list[tuple[str, str, str]]:
    """[(country_code, board location string, indeed country), ...] for one document.

    Explicit locations win; otherwise the whole of each selected country. A
    location the user typed is used verbatim — inventing "Seattle, WA, USA" out
    of "Seattle" is how a board ends up returning nothing.
    """
    countries = prefs.get("countries") or ["US"]
    locations = [str(p).strip() for p in prefs.get("locations") or [] if str(p).strip()]
    out = []
    for code in countries:
        code = str(code).upper()[:2]
        place, indeed = COUNTRY_PLACES.get(code, (code, code))
        if locations:
            out.extend((code, loc, indeed) for loc in locations)
        else:
            out.append((code, place, indeed))
    return out


def _job_type(prefs: dict) -> str | None:
    """The board's single job_type slot, or None for "any".

    Boards take one value. With several selected, asking for none of them and
    letting the Preference Gate sort the results out returns strictly more of
    what the user asked for than picking one arbitrarily — that arbitrary pick
    is exactly what used to make "Full-time + Internship" return no internships.
    """
    types = prefs.get("job_types") or []
    return types[0] if len(types) == 1 else None


def _is_remote(prefs: dict) -> bool | None:
    models = set(prefs.get("work_models") or [])
    if models == {"remote"}:
        return True
    if models and "remote" not in models:
        return False
    return None      # any


def plan(criteria_list) -> dict:
    """``{"queries": [...], "boards": [...], "truncated": bool}``.

    `criteria_list` is the main preferences document plus every active saved
    filter. Overlapping filters are deduplicated here, once, so two filters that
    both want US software-engineering roles cost one set of queries and not two.
    """
    queries, seen = [], set()
    truncated = False
    for prefs in criteria_list or []:
        functions = prefs.get("job_functions") or []
        terms = T.function_search_terms(functions)
        # A committed search-box term is a discovery intent of its own, and the
        # most specific thing the user has said — it leads.
        title_query = str(prefs.get("title_query") or "").strip()
        if title_query:
            terms = [title_query] + [t for t in terms if t.casefold() != title_query.casefold()]
        if not terms:
            continue
        job_type = _job_type(prefs)
        remote = _is_remote(prefs)
        days = int(prefs.get("date_posted_days") or 7)
        for country, location, indeed_country in _places(prefs):
            for term in terms:
                key = (term.casefold(), location.casefold(), country, job_type, remote)
                if key in seen:
                    continue
                if len(queries) >= MAX_QUERIES:
                    truncated = True
                    break
                seen.add(key)
                queries.append({
                    "job_function": next((f for f in functions
                                          if term in T.function_search_terms([f])), None),
                    "search_term": term,
                    "country": country,
                    "location": location,
                    "indeed_country": indeed_country,
                    "levels": list(prefs.get("levels") or []),
                    "job_type": job_type,
                    "is_remote": remote,
                    "hours_old": max(1, days) * 24,
                    "results_wanted": RESULTS_PER_QUERY,
                })
            if truncated:
                break
        if truncated:
            break
    if truncated:
        logger.info("Discovery plan capped at %d queries", MAX_QUERIES)
    return {"queries": queries, "boards": list(DEFAULT_BOARDS), "truncated": truncated}


def plan_for(db) -> dict:
    """The current plan, from whatever the user has saved."""
    from backend.discovery import preferences as P
    return plan(P.active_criteria(db))


def to_search(query: dict, boards=None, excluded_companies=None, companies=None):
    """One plan query as a transient, UNPERSISTED ``Search``.

    The collectors take a Search because that is the interface every source
    already speaks. Building one in memory — never added to a session, id left
    None — reuses all of them without the user ever owning a Search row, and
    without this feature growing a second copy of the JobSpy call.
    """
    from backend.models.db import Search
    search = Search(
        name=f"Discovery: {query['search_term']} · {query['location']}",
        active=True,
        search_mode="keyword",
        sources=list(boards or DEFAULT_BOARDS),
        search_term=query["search_term"],
        location=query["location"],
        is_remote=query.get("is_remote"),
        job_type=query.get("job_type"),
        hours_old=query.get("hours_old") or 168,
        results_wanted=query.get("results_wanted") or RESULTS_PER_QUERY,
        title_include_keywords=[],
        # Deliberately empty. Seniority is a semantic criterion the Preference
        # Gate decides; a blanket "intern, junior, associate" title exclusion is
        # what used to throw away the whole new-grad feed before it was scored.
        title_exclude_keywords=[],
        company_filter=list(companies or []),
        company_exclude=list(excluded_companies or []),
        exclude_active_companies=False,
        auto_scoring_depth="off",
    )
    # Not a column: boards want the country for their own regional index, and a
    # transient object is free to carry it.
    search.country_indeed = query.get("indeed_country") or "USA"
    return search
