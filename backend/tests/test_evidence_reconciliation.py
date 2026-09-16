"""Historical résumés are evidence, not truth: the four reconciliation outcomes.

A duplicate attaches a source, more evidence unions in, a disagreement becomes a
review conflict the user resolves, and anything genuinely new arrives unverified.
Nothing here ever overwrites a stored value, and no LLM decides any of it.
"""
import pytest

from backend.copilot import evidence as EV
from backend.models.db import CandidateFact, CandidateFactSource, FactConflict, ResumeSource


def _source(db, filename):
    row = ResumeSource(filename=filename, sha256=filename.ljust(64, "0")[:64], status="imported")
    db.add(row)
    db.flush()
    return row


def _intern(employer="CED Engineering, Inc.", title="Software Engineering Intern",
            start="2025-05", end="2025-08", bullets=()):
    return {"kind": "internship", "locator": "experience[0]",
            "data": {"employer": employer, "title": title, "start_date": start, "end_date": end},
            "children": [{"kind": "achievement", "data": {"text": b}, "raw": b} for b in bullets]}


# ── normalisation: identity is not raw text ──────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("CED Engineering, Inc.", "ced engineering"),
    ("The Acme Co.", "Acme"),
    ("Texas A&M University", "texas a m university"),
])
def test_employers_normalise_to_the_same_organisation(a, b):
    assert EV.norm_org(a) == EV.norm_org(b)


def test_acme_technologies_is_not_folded_into_acme():
    assert EV.norm_org("Acme Technologies") != EV.norm_org("Acme")


@pytest.mark.parametrize("a,b", [
    ("Sr. Software Engineer", "Senior Software Engineer"),
    ("Software Engineering Intern", "Software Engineer Intern"),
    ("SWE Intern", "Software Engineering Internship"),
])
def test_titles_normalise_to_the_same_role(a, b):
    assert EV.norm_title(a) == EV.norm_title(b)


def test_levels_stay_distinct_roles():
    assert EV.norm_title("Software Engineer II") != EV.norm_title("Software Engineer")


@pytest.mark.parametrize("a,b", [("Node.js", "NodeJS"), ("node js", "nodejs"), ("PostgreSQL", "postgresql")])
def test_skill_names_normalise(a, b):
    assert EV.norm_skill(a) == EV.norm_skill(b)


def test_c_plus_plus_and_c_sharp_stay_distinct_skills():
    assert len({EV.norm_skill("C++"), EV.norm_skill("C#"), EV.norm_skill("C")}) == 3


def test_degrees_normalise():
    assert EV.norm_degree("B.S.") == EV.norm_degree("BS") == EV.norm_degree("Bachelor of Science")


# ── A: compatible duplicate ──────────────────────────────────────────────────

def test_duplicate_attaches_the_new_document_and_never_replaces_a_value(test_db):
    old, new = _source(test_db, "resume-2024.pdf"), _source(test_db, "resume-2025.pdf")
    EV.reconcile(test_db, [_intern()], old)
    EV.reconcile(test_db, [_intern(employer="ced engineering", title="SWE Intern")], new)

    rows = test_db.query(CandidateFact).filter(CandidateFact.kind == "internship").all()
    assert len(rows) == 1, "one canonical internship, whatever each résumé called it"
    assert rows[0].data["employer"] == "CED Engineering, Inc.", "the stored spelling is never overwritten"
    assert {l.resume_source_id for l in test_db.query(CandidateFactSource).all()} == {old.id, new.id}


def test_a_missing_field_is_filled_but_a_present_one_is_not(test_db):
    a, b = _source(test_db, "a.pdf"), _source(test_db, "b.pdf")
    EV.reconcile(test_db, [_intern(start="", end="")], a)
    EV.reconcile(test_db, [_intern(start="2025-05", end="2025-08")], b)

    row = test_db.query(CandidateFact).filter(CandidateFact.kind == "internship").one()
    assert (row.data["start_date"], row.data["end_date"]) == ("2025-05", "2025-08")
    assert test_db.query(FactConflict).count() == 0, "filling an empty field is not a conflict"


# ── B: the same entity with more evidence ────────────────────────────────────

def test_a_newer_resume_adds_its_extra_bullets_to_one_canonical_internship(test_db):
    old, new = _source(test_db, "resume-2024.pdf"), _source(test_db, "resume-2025.pdf")
    EV.reconcile(test_db, [_intern(bullets=["Built the ingest pipeline", "Wrote the deploy runbook"])], old)
    EV.reconcile(test_db, [_intern(bullets=["Built the ingest pipeline", "Wrote the deploy runbook",
                                            "Cut report time by 30%", "Mentored two interns"])], new)

    parents = test_db.query(CandidateFact).filter(CandidateFact.kind == "internship").all()
    assert len(parents) == 1
    kids = test_db.query(CandidateFact).filter(CandidateFact.parent_id == parents[0].id).all()
    assert sorted(k.data["text"] for k in kids) == [
        "Built the ingest pipeline", "Cut report time by 30%", "Mentored two interns", "Wrote the deploy runbook"]

    shared = next(k for k in kids if k.data["text"] == "Built the ingest pipeline")
    only_new = next(k for k in kids if k.data["text"] == "Mentored two interns")
    links = lambda f: {l.resume_source_id for l in test_db.query(CandidateFactSource)
                       .filter(CandidateFactSource.candidate_fact_id == f.id).all()}
    assert links(shared) == {old.id, new.id}, "provenance is kept per achievement"
    assert links(only_new) == {new.id}


def test_lists_are_unioned_not_replaced(test_db):
    a, b = _source(test_db, "a.pdf"), _source(test_db, "b.pdf")
    item = {"kind": "project", "data": {"name": "Ledgerly", "technologies": ["Python", "Flask"]}}
    EV.reconcile(test_db, [item], a)
    EV.reconcile(test_db, [{"kind": "project", "data": {"name": "ledgerly", "technologies": ["flask", "Docker"]}}], b)

    row = test_db.query(CandidateFact).filter(CandidateFact.kind == "project").one()
    assert row.data["technologies"] == ["Python", "Flask", "Docker"]


def test_the_same_bullet_under_two_different_jobs_stays_two_facts(test_db):
    src = _source(test_db, "resume.pdf")
    EV.reconcile(test_db, [
        _intern(employer="Acme", title="Intern", bullets=["Wrote unit tests"]),
        _intern(employer="Globex", title="Intern", bullets=["Wrote unit tests"]),
    ], src)
    kids = test_db.query(CandidateFact).filter(CandidateFact.kind == "achievement").all()
    assert len(kids) == 2 and len({k.parent_id for k in kids}) == 2


# ── C: conflict ──────────────────────────────────────────────────────────────

def test_disagreeing_graduation_dates_become_a_review_conflict_not_a_choice(test_db):
    a, b = _source(test_db, "resume-a.pdf"), _source(test_db, "resume-b.pdf")
    grad = lambda d: [{"kind": "education", "data": {"institution": "State U", "degree": "B.S.",
                                                     "major": "Computer Science", "graduation_date": d}}]
    EV.reconcile(test_db, grad("2027-05"), a)
    tally = EV.reconcile(test_db, grad("2027-12"), b)

    assert tally[EV.Outcome.CONFLICT] == 1
    rows = test_db.query(CandidateFact).filter(CandidateFact.kind == "education").all()
    assert len(rows) == 1, "a date disagreement is one entity, not two degrees"
    assert rows[0].data["graduation_date"] == "2027-05", "the stored value stands until the user chooses"

    conflict = test_db.query(FactConflict).one()
    assert (conflict.field, conflict.current_value, conflict.proposed_value) == ("graduation_date", "2027-05", "2027-12")
    assert conflict.status == "open" and conflict.resume_source_id == b.id


def test_the_same_conflict_is_not_raised_twice(test_db):
    a, b, c = _source(test_db, "a.pdf"), _source(test_db, "b.pdf"), _source(test_db, "c.pdf")
    grad = lambda d: [{"kind": "education", "data": {"institution": "State U", "degree": "B.S.", "graduation_date": d}}]
    EV.reconcile(test_db, grad("2027-05"), a)
    EV.reconcile(test_db, grad("2027-12"), b)
    EV.reconcile(test_db, grad("2027-12"), c)
    assert test_db.query(FactConflict).count() == 1


def test_resolving_a_conflict_writes_the_users_choice(test_db):
    a, b = _source(test_db, "a.pdf"), _source(test_db, "b.pdf")
    grad = lambda d: [{"kind": "education", "data": {"institution": "State U", "degree": "B.S.", "graduation_date": d}}]
    EV.reconcile(test_db, grad("2027-05"), a)
    EV.reconcile(test_db, grad("2027-12"), b)

    conflict = test_db.query(FactConflict).one()
    fact = EV.resolve_conflict(test_db, conflict, "2027-12")
    test_db.flush()
    assert fact.data["graduation_date"] == "2027-12"
    assert conflict.status == "resolved" and conflict.resolved_value == "2027-12"


def test_a_resolved_conflict_does_not_reopen_on_the_next_import(test_db):
    a, b, c = _source(test_db, "a.pdf"), _source(test_db, "b.pdf"), _source(test_db, "c.pdf")
    grad = lambda d: [{"kind": "education", "data": {"institution": "State U", "degree": "B.S.", "graduation_date": d}}]
    EV.reconcile(test_db, grad("2027-05"), a)
    EV.reconcile(test_db, grad("2027-12"), b)
    EV.resolve_conflict(test_db, test_db.query(FactConflict).one(), "2027-12")
    test_db.flush()

    EV.reconcile(test_db, grad("2027-12"), c)
    assert test_db.query(FactConflict).filter(FactConflict.status == "open").count() == 0


def test_a_different_spelling_of_an_identity_field_is_not_a_conflict(test_db):
    """Normalisation decided these are the same employer, role and degree — it must not then ask the user to arbitrate the spelling."""
    a, b = _source(test_db, "a.pdf"), _source(test_db, "b.pdf")
    EV.reconcile(test_db, [_intern(employer="CED Engineering", title="Software Engineering Intern"),
                           {"kind": "education", "data": {"institution": "State U", "degree": "B.S. Computer Science"}}], a)
    EV.reconcile(test_db, [_intern(employer="CED Engineering, Inc.", title="SWE Intern"),
                           {"kind": "education", "data": {"institution": "State U", "degree": "BS Computer Science"}}], b)

    assert test_db.query(FactConflict).count() == 0
    assert test_db.query(CandidateFact).filter(CandidateFact.kind == "internship").one().data["employer"] == "CED Engineering"


def test_differently_worded_prose_is_not_a_conflict(test_db):
    a, b = _source(test_db, "a.pdf"), _source(test_db, "b.pdf")
    EV.reconcile(test_db, [{"kind": "project", "data": {"name": "Ledgerly", "description": "A budgeting app."}}], a)
    EV.reconcile(test_db, [{"kind": "project", "data": {"name": "Ledgerly", "description": "Budget tracker, open source."}}], b)

    assert test_db.query(FactConflict).count() == 0
    assert test_db.query(CandidateFact).filter(CandidateFact.kind == "project").one().data["description"] == "A budgeting app."


# ── D: novel ─────────────────────────────────────────────────────────────────

def test_a_new_fact_arrives_unverified_and_names_its_document(test_db):
    src = _source(test_db, "resume-swe-2025.pdf")
    tally = EV.reconcile(test_db, [_intern()], src)

    assert tally[EV.Outcome.NOVEL] == 1
    row = test_db.query(CandidateFact).filter(CandidateFact.kind == "internship").one()
    assert row.verified is False and row.source == "import:resume-swe-2025.pdf"


def test_invalid_items_are_dropped_not_stored(test_db):
    src = _source(test_db, "junk.pdf")
    tally = EV.reconcile(test_db, [{"kind": "education", "data": {"institution": ""}},
                                   {"kind": "nonsense", "data": {}}], src)
    assert tally["dropped"] == 2 and test_db.query(CandidateFact).count() == 0


# ── extraction carries provenance ────────────────────────────────────────────

def test_extraction_records_where_in_the_document_each_claim_came_from():
    items = EV.facts_from_resume_json({
        "experience": [{"company": "CED", "title": "Intern", "date": "May 2025 - Aug 2025",
                        "bullets": ["Built the pipeline"]}],
        "skills": {"Languages": "Python, C++"},
    })
    exp = next(i for i in items if i["kind"] == "internship")
    assert exp["locator"] == "experience[0]"
    assert exp["children"][0]["locator"] == "experience[0].bullets[0]"
    assert exp["children"][0]["raw"] == "Built the pipeline"
    assert [i["data"]["name"] for i in items if i["kind"] == "skill"] == ["Python", "C++"]
