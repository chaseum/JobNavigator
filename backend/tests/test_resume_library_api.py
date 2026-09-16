"""The Résumé Library endpoints: batch upload, provenance, conflicts, role-family bases.

The onboarding contract these pin down: you drop in the résumés you already have,
the text is taken out of them in the request, the slow structuring happens in the
background, and nothing an import produced counts until you confirm it.
"""
import io

import pytest

from backend.copilot import evidence as EV
from backend.models.db import CandidateFact, FactConflict, ResumeSource


@pytest.fixture
def no_background(monkeypatch):
    """Upload must not actually start the import job under test."""
    launched = []
    monkeypatch.setattr("backend.api.routes_profile.launch_background",
                        lambda *a, **kw: launched.append(a) or "run-1")
    return launched


@pytest.fixture
def fake_pdf(monkeypatch):
    """Every uploaded file's "text" is its own bytes; parsing PDFs is not what these test."""
    monkeypatch.setattr("backend.api.routes_resumes.extract_pdf_text",
                        lambda pdf: pdf.decode("utf-8", "replace"))


def _files(*pairs):
    return [("files", (name, io.BytesIO(body), "application/pdf")) for name, body in pairs]


def test_several_resumes_upload_at_once_and_queue_one_import(api_client, test_db, no_background, fake_pdf):
    r = api_client.post("/api/profile/resumes", files=_files(
        ("resume-2023.pdf", b"x" * 80), ("resume-2024.pdf", b"y" * 80), ("resume-2025.pdf", b"z" * 80)))
    assert r.status_code == 202
    body = r.json()
    assert len(body["accepted"]) == 3 and body["run_id"] == "run-1"
    assert len(no_background) == 1, "one job for the whole batch, not one per file"

    lib = api_client.get("/api/profile/resumes").json()
    assert [s["filename"] for s in lib["sources"]] == ["resume-2025.pdf", "resume-2024.pdf", "resume-2023.pdf"]
    assert all(s["status"] == "pending" for s in lib["sources"])
    # the text is kept, the PDF bytes are not
    assert {c.name for c in ResumeSource.__table__.columns} .isdisjoint({"pdf", "pdf_bytes"})


def test_the_same_file_twice_is_reported_not_imported_twice(api_client, test_db, no_background, fake_pdf):
    api_client.post("/api/profile/resumes", files=_files(("a.pdf", b"same" * 40)))
    r = api_client.post("/api/profile/resumes", files=_files(("copy-of-a.pdf", b"same" * 40)))
    body = r.json()
    assert body["accepted"] == [] and len(body["skipped"]) == 1
    assert "already in the library" in body["skipped"][0]["reason"]
    assert test_db.query(ResumeSource).count() == 1


def test_a_bad_file_does_not_lose_the_rest_of_the_batch(api_client, test_db, no_background, fake_pdf):
    body = api_client.post("/api/profile/resumes", files=_files(
        ("good.pdf", b"g" * 80), ("notes.txt", b"nope"))).json()
    assert [s["filename"] for s in body["accepted"]] == ["good.pdf"]
    assert body["rejected"][0]["filename"] == "notes.txt"


def test_facts_say_which_resumes_support_them(api_client, test_db):
    older = ResumeSource(filename="resume-2024.pdf", sha256="a" * 64, status="imported")
    newer = ResumeSource(filename="resume-2025.pdf", sha256="b" * 64, status="imported")
    test_db.add_all([older, newer])
    test_db.commit()
    item = {"kind": "internship", "data": {"employer": "CED", "title": "Software Engineering Intern"},
            "children": [{"kind": "achievement", "data": {"text": "Built the ingest pipeline"}}]}
    EV.reconcile(test_db, [item], older)
    EV.reconcile(test_db, [item], newer)
    test_db.commit()

    prof = api_client.get("/api/profile").json()
    intern = next(f for f in prof["facts"] if f["kind"] == "internship")
    assert [s["filename"] for s in intern["sources"]] == ["resume-2024.pdf", "resume-2025.pdf"]
    assert prof["resume_count"] == 2 and prof["open_conflicts"] == 0
    assert intern["verified"] is False, "imported evidence waits for confirmation"


def test_bulk_confirming_selected_facts_changes_what_the_pipeline_may_cite(api_client, test_db):
    src = ResumeSource(filename="r.pdf", sha256="c" * 64, status="imported")
    test_db.add(src)
    test_db.commit()
    EV.reconcile(test_db, [{"kind": "skill", "data": {"name": "Python"}},
                           {"kind": "skill", "data": {"name": "Rust"}}], src)
    test_db.commit()

    before = api_client.get("/api/profile").json()
    ids = [f["id"] for f in before["facts"]]
    assert api_client.post("/api/profile/facts/verify", json={"ids": ids}).json()["verified"] == 2
    after = api_client.get("/api/profile").json()
    assert after["profile_version"] != before["profile_version"]
    assert all(f["verified"] for f in after["facts"])


def test_a_conflict_is_listed_and_resolved_by_the_user(api_client, test_db):
    a = ResumeSource(filename="a.pdf", sha256="d" * 64, status="imported")
    b = ResumeSource(filename="b.pdf", sha256="e" * 64, status="imported")
    test_db.add_all([a, b])
    test_db.commit()
    grad = lambda d: [{"kind": "education", "data": {"institution": "State U", "degree": "B.S.", "graduation_date": d}}]
    EV.reconcile(test_db, grad("2027-05"), a)
    EV.reconcile(test_db, grad("2027-12"), b)
    test_db.commit()

    rows = api_client.get("/api/profile/conflicts").json()
    assert len(rows) == 1 and rows[0]["field"] == "graduation_date" and rows[0]["from_resume"] == "b.pdf"
    assert rows[0]["current_value"] == "2027-05" and rows[0]["proposed_value"] == "2027-12"

    out = api_client.post(f"/api/profile/conflicts/{rows[0]['id']}/resolve", json={"choice": "proposed"})
    assert out.status_code == 200 and out.json()["fact"]["data"]["graduation_date"] == "2027-12"
    assert api_client.get("/api/profile/conflicts").json() == []
    assert api_client.post(f"/api/profile/conflicts/{rows[0]['id']}/resolve", json={"choice": "current"}).status_code == 409


def test_resolving_with_an_invalid_value_is_rejected(api_client, test_db):
    a = ResumeSource(filename="a.pdf", sha256="f" * 64, status="imported")
    b = ResumeSource(filename="b.pdf", sha256="0" * 64, status="imported")
    test_db.add_all([a, b])
    test_db.commit()
    grad = lambda d: [{"kind": "education", "data": {"institution": "State U", "degree": "B.S.", "graduation_date": d}}]
    EV.reconcile(test_db, grad("2027-05"), a)
    EV.reconcile(test_db, grad("2027-12"), b)
    test_db.commit()
    cid = api_client.get("/api/profile/conflicts").json()[0]["id"]
    assert api_client.post(f"/api/profile/conflicts/{cid}/resolve", json={"value": "sometime next spring"}).status_code == 400
    assert test_db.query(FactConflict).one().status == "open"


def test_forgetting_a_source_keeps_the_facts_it_supported(api_client, test_db):
    src = ResumeSource(filename="old.pdf", sha256="1" * 64, status="imported")
    test_db.add(src)
    test_db.commit()
    EV.reconcile(test_db, [{"kind": "project", "data": {"name": "Ledgerly"}}], src)
    test_db.commit()

    assert api_client.delete(f"/api/profile/resumes/{src.id}").status_code == 200
    assert test_db.query(ResumeSource).count() == 0
    assert test_db.query(CandidateFact).filter(CandidateFact.kind == "project").count() == 1
    assert api_client.get("/api/profile").json()["facts"][0]["sources"] == []


def test_role_family_bases_are_listed_with_their_current_base(api_client, test_db):
    out = api_client.get("/api/resume-versions/role-families").json()
    ids = [f["id"] for f in out["families"]]
    assert ids[:3] == ["software_engineering", "product", "data_ml"]
    assert all(f["base"] is None for f in out["families"]), "nothing generated yet"
    assert all(f["label"] for f in out["families"])
