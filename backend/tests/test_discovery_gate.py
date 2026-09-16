"""The Preference Gate. Deterministic, and conservative about what it does not know.

The whole point of these cases is the asymmetry: a posting is rejected on
evidence, never on the absence of it.
"""
import pytest

from backend.discovery import gate
from backend.discovery import preferences as P
from backend.discovery import taxonomy as T


class FakeJob:
    """A posting-shaped object; the gate never needs an ORM row."""

    def __init__(self, **kw):
        defaults = dict(
            title="Software Engineer", description="", company="Acme",
            loc_country="US", loc_region=None, loc_city=None, location="Seattle, WA",
            arr_remote=None, arr_hybrid=None, arr_onsite=None,
            employment_type=None, salary_min=None, salary_max=None, salary_period="yearly",
            h1b_verdict=None, job_function=None, experience_levels=None,
            job_type=None, min_years_experience=None, max_years_experience=None,
        )
        defaults.update(kw)
        for k, v in defaults.items():
            setattr(self, k, v)


NEW_GRAD = P.normalize({
    "countries": ["US"],
    "job_functions": ["software_engineering"],
    "levels": ["intern", "new_grad", "entry"],
    "job_types": ["fulltime", "internship"],
    "work_models": ["onsite", "hybrid", "remote"],
    "max_years_experience": 2,
})


def verdict(job, prefs=NEW_GRAD):
    return gate.evaluate(job, prefs)


# ── rejections, each on explicit evidence ────────────────────────────────────

def test_senior_title_is_rejected_for_new_grad_criteria():
    v = verdict(FakeJob(title="Senior Staff Software Engineer"))
    assert v["eligible"] is False
    assert any("seniority" in r for r in v["reasons"])


def test_explicit_five_years_is_rejected_against_a_two_year_ceiling():
    v = verdict(FakeJob(description="You have 5+ years of professional experience building services."))
    assert v["eligible"] is False
    assert any("years" in r for r in v["reasons"])


def test_unrelated_function_is_rejected():
    v = verdict(FakeJob(title="Senior Account Executive, Enterprise Sales"))
    assert v["eligible"] is False


def test_remote_only_preference_rejects_an_explicitly_onsite_job():
    prefs = P.normalize({**NEW_GRAD, "work_models": ["remote"]})
    v = verdict(FakeJob(arr_onsite=True), prefs)
    assert v["eligible"] is False
    assert any("On-site" in r for r in v["reasons"])


def test_wrong_country_is_rejected():
    v = verdict(FakeJob(loc_country="DE", location="Berlin, Germany"))
    assert v["eligible"] is False


def test_excluded_company_is_rejected():
    prefs = P.normalize({**NEW_GRAD, "excluded_companies": ["Acme"]})
    assert verdict(FakeJob(), prefs)["eligible"] is False


# ── the asymmetry: unknown is not a rejection ────────────────────────────────

def test_missing_years_stays_unknown_and_does_not_reject():
    v = verdict(FakeJob(description="We build reliable services. Great team, great benefits."))
    assert v["eligible"] is True
    assert any("years" in u for u in v["uncertain"])


def test_missing_work_model_stays_unknown():
    prefs = P.normalize({**NEW_GRAD, "work_models": ["remote", "hybrid"]})
    v = verdict(FakeJob(), prefs)
    assert v["eligible"] is True
    assert any("remote" in u for u in v["uncertain"])


def test_missing_country_stays_unknown():
    v = verdict(FakeJob(loc_country=None, location=""))
    assert v["eligible"] is True
    assert any("country" in u for u in v["uncertain"])


def test_missing_seniority_stays_unknown():
    v = verdict(FakeJob(title="Software Engineer"))
    assert v["eligible"] is True
    assert any("seniority" in u for u in v["uncertain"])


# ── acceptances ──────────────────────────────────────────────────────────────

def test_junior_software_engineer_is_not_globally_excluded():
    """The old default title filter deleted exactly this posting before scoring it."""
    v = verdict(FakeJob(title="Junior Software Engineer"))
    assert v["eligible"] is True, v["reasons"]


def test_new_grad_title_is_accepted():
    assert verdict(FakeJob(title="New Grad Software Engineer, 2026"))["eligible"] is True


def test_internship_accepted_when_internship_is_selected():
    v = verdict(FakeJob(title="Software Engineer Intern - Summer 2026", employment_type="internship"))
    assert v["eligible"] is True


def test_internship_rejected_when_only_fulltime_is_selected():
    prefs = P.normalize({**NEW_GRAD, "levels": ["new_grad", "entry"], "job_types": ["fulltime"]})
    v = verdict(FakeJob(title="Software Engineer Intern", employment_type="internship"), prefs)
    assert v["eligible"] is False


def test_hybrid_accepted_when_hybrid_selected():
    prefs = P.normalize({**NEW_GRAD, "work_models": ["hybrid"]})
    assert verdict(FakeJob(arr_hybrid=True), prefs)["eligible"] is True


def test_two_years_required_passes_a_two_year_ceiling():
    v = verdict(FakeJob(description="Requires 2+ years of experience with Python."))
    assert v["eligible"] is True


def test_lowest_stated_floor_wins():
    """"2+ years backend, 5+ preferred" has an entry bar of two, not five."""
    v = verdict(FakeJob(description=(
        "You have 2+ years of experience writing backend services. "
        "5+ years of experience with distributed systems is preferred.")))
    assert v["eligible"] is True


def test_any_active_criteria_document_may_admit_a_job():
    """Saved filters are alternatives, not extra constraints."""
    swe_us = P.normalize({"job_functions": ["software_engineering"], "countries": ["US"], "levels": []})
    data_ca = P.normalize({"job_functions": ["data_ml"], "countries": ["CA"], "levels": []})
    job = FakeJob(title="Machine Learning Engineer", loc_country="CA", location="Toronto, ON")
    assert gate.evaluate(job, swe_us)["eligible"] is False
    assert gate.evaluate_any(job, [swe_us, data_ca])["eligible"] is True


# ── the deterministic parsers underneath ─────────────────────────────────────

@pytest.mark.parametrize("title,expected", [
    ("Senior Software Engineer", "senior"),
    ("Software Engineering Intern", "intern"),
    ("New Grad Software Engineer", "new_grad"),
    ("Junior Developer", "entry"),
    ("Engineering Manager", "manager"),
    ("Director of Engineering", "director"),
])
def test_level_classification(title, expected):
    assert expected in T.classify_levels(title)


def test_a_senior_title_never_also_reads_as_entry():
    assert set(T.classify_levels("Senior Associate Software Engineer")) & {"entry", "new_grad"} == set()


@pytest.mark.parametrize("title,expected", [
    ("Backend Engineer", "software_engineering"),
    ("Machine Learning Engineer", "data_ml"),
    ("Data Analyst", "data_analytics"),
    ("Technical Product Manager", "product"),
    ("Site Reliability Engineer", "devops_cloud"),
    ("Application Security Engineer", "security"),
    ("Product Designer", "design"),
    ("QA Automation Engineer", "qa"),
    ("Regional Sales Director", "other"),
])
def test_function_classification(title, expected):
    assert T.classify_function(title) == expected


@pytest.mark.parametrize("text,expected", [
    ("5+ years of experience required", (5, None)),
    ("3-5 years of relevant experience", (3, 5)),
    ("0-2 years of experience", (0, 2)),
    ("We were founded 12 years ago.", (None, None)),   # no "experience" nearby
    ("", (None, None)),
])
def test_years_parsing(text, expected):
    assert T.parse_years(text) == expected


def test_level_pack_round_trip():
    assert T.unpack_levels(T.pack_levels(["new_grad", "entry"])) == ["new_grad", "entry"]
    assert T.pack_levels([]) is None
    assert T.unpack_levels(None) == []
    # the sentinel commas are what keep a LIKE match exact
    assert T.pack_levels(["entry"]) == ",entry,"
