"""Candidate fact database: validation, provenance, import-needs-review, evidence integrity."""
import pytest

from backend.copilot import facts as F
from backend.api.routes_profile import facts_from_resume_json, parse_date_range


def test_validate_rejects_unknown_keys_bad_dates_and_missing_required():
    with pytest.raises(ValueError, match="employer"):
        F.validate_data("experience", {"title": "Engineer"})
    with pytest.raises(ValueError, match="YYYY"):
        F.validate_data("experience", {"employer": "A", "title": "B", "start_date": "May 2021"})
    with pytest.raises(ValueError, match="Extra"):
        F.validate_data("project", {"name": "X", "stars": 5})
    with pytest.raises(ValueError, match="unknown fact kind"):
        F.validate_data("hobby", {})
    ok = F.validate_data("experience", {"employer": " CED ", "title": "Intern", "end_date": "Present"})
    assert ok["employer"] == "CED" and ok["end_date"] == "present"


def test_provenance_refs_round_trip_and_reject_garbage():
    class Row:
        kind, id = "achievement", 12
    assert F.fact_ref(Row) == "achievement_12"
    assert F.parse_ref("achievement_12") == ("achievement", 12)
    for bad in ("fact", "kubernetes_3", "achievement_", "identity.authorized_us", None):
        assert F.parse_ref(bad) is None


@pytest.mark.parametrize("text,expected", [
    ("May 2021 – Present", ("2021-05", "present")),
    ("Jan 2019 - Dec 2020", ("2019-01", "2020-12")),
    ("2018-2020", ("2018", "2020")),
    ("Summer internship", ("", "")),
])
def test_parse_date_range(text, expected):
    assert parse_date_range(text) == expected


def test_resume_json_becomes_facts_with_bullets_as_child_achievements():
    items = facts_from_resume_json({
        "experience": [{"company": "CED", "title": "Software Intern", "date": "Jun 2024 – Aug 2024",
                        "bullets": ["Built EEG pipeline in Python", " "]}],
        "education": [{"school": "State U", "degree": "BS CS", "years": "2021 – 2025"}],
        "skills": {"Languages": "Python, C++", "Tools": ["Git"]},
    })
    exp = items[0]
    assert exp["kind"] == "internship" and exp["data"]["start_date"] == "2024-06"
    assert [c["data"]["text"] for c in exp["children"]] == ["Built EEG pipeline in Python"]
    assert {i["data"]["name"] for i in items if i["kind"] == "skill"} == {"Python", "C++", "Git"}
    assert any(i["kind"] == "education" and i["data"]["graduation_date"] == "2025" for i in items)


def test_manual_fact_is_verified_import_is_not_and_version_tracks_only_verified(api_client, test_db):
    import uuid
    from backend.models.db import Persona, Resume
    rid = str(uuid.uuid4())  # an all-digit id reads back as a float under SQLite's NUMERIC affinity
    test_db.add(Persona(id=1, contact={}, work_auth={}, preferences={}, compensation={}, demographics={}, resume_content={}, qa_bank=[]))
    test_db.add(Resume(id=rid, name="Base", is_base=True, json_data={
        "header": {"name": "Ada Lovelace", "contact_items": [{"text": "ada@example.com"}]},
        "experience": [{"company": "Analytical Co", "title": "Engineer", "date": "2020 – 2022", "bullets": ["Wrote the first program"]}],
    }))
    test_db.commit()

    v0 = api_client.get("/api/profile").json()["profile_version"]
    r = api_client.post("/api/profile/facts", json={"kind": "project", "data": {"name": "Notes on the Engine"}})
    assert r.status_code == 201 and r.json()["verified"] is True and r.json()["ref"].startswith("project_")
    v1 = api_client.get("/api/profile").json()["profile_version"]
    assert v1 != v0

    imp = api_client.post("/api/profile/import", json={"resume_id": rid}).json()
    assert imp["created"] == 2 and imp["contact_filled"] >= 1
    prof = api_client.get("/api/profile").json()
    imported = [f for f in prof["facts"] if f["source"].startswith("import:")]
    assert imported and not any(f["verified"] for f in imported)
    assert prof["profile_version"] == v1, "unverified facts must not change what the pipeline may cite"

    # re-import is idempotent
    assert api_client.post("/api/profile/import", json={"resume_id": rid}).json()["created"] == 0

    api_client.post("/api/profile/facts/verify", json={"ids": [f["id"] for f in imported]})
    assert api_client.get("/api/profile").json()["profile_version"] != v1


def test_skill_evidence_must_exist_and_is_dropped_when_the_fact_goes(api_client, test_db):
    bad = api_client.post("/api/profile/facts", json={"kind": "skill", "data": {"name": "Kubernetes", "evidence_ids": ["project_999"]}})
    assert bad.status_code == 400

    proj = api_client.post("/api/profile/facts", json={"kind": "project", "data": {"name": "EEG pipeline"}}).json()
    skill = api_client.post("/api/profile/facts", json={"kind": "skill", "data": {"name": "Python", "evidence_ids": [proj["ref"]]}}).json()
    assert skill["data"]["evidence_ids"] == [proj["ref"]]

    api_client.delete(f"/api/profile/facts/{proj['id']}")
    after = next(f for f in api_client.get("/api/profile").json()["facts"] if f["id"] == skill["id"])
    assert after["data"]["evidence_ids"] == []


def test_only_achievements_take_a_parent(api_client, test_db):
    exp = api_client.post("/api/profile/facts", json={"kind": "experience", "data": {"employer": "A", "title": "B"}}).json()
    ok = api_client.post("/api/profile/facts", json={"kind": "achievement", "parent_id": exp["id"], "data": {"text": "Cut costs 10%"}})
    assert ok.status_code == 201
    assert api_client.post("/api/profile/facts", json={"kind": "skill", "parent_id": exp["id"], "data": {"name": "x"}}).status_code == 400
