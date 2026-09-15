"""Autofill copilot: answer-bank normalization, protected fields, job/résumé matching, application tracking."""
import base64
import uuid

import pytest

from backend.copilot import answer_bank as AB


# ── normalization ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("q,intent", [
    ("Will you now or in the future require sponsorship?", "sponsorship"),
    ("Do you require employment visa sponsorship?", "sponsorship"),
    ("Will you need H-1B sponsorship to work for us?", "sponsorship"),
    ("Are you legally authorized to work in the United States?", "work_authorization"),
    ("Are you authorized to work in the US without requiring sponsorship?", "work_authorization"),
    ("What are your salary expectations?", "salary"),
    ("How did you hear about this job?", "how_heard"),
    ("Were you referred by a current employee?", "referral"),
    ("Please describe a project you are proud of.", None),
])
def test_questions_normalize_to_intents(q, intent):
    assert AB.classify(q) == intent


def test_equivalent_wordings_share_one_entry_and_keep_the_original():
    bank, first = AB.save([], "Will you now or in the future require sponsorship?", "No")
    bank, second = AB.save(bank, "Do you require employment visa sponsorship?", "No")
    assert len(bank) == 1 and second["id"] == first["id"]
    assert bank[0]["question"] == "Will you now or in the future require sponsorship?"
    assert bank[0]["aliases"] == ["Do you require employment visa sponsorship?"]
    entry, how = AB.find_answer(bank, "Will you require H-1B visa sponsorship to work here?")
    assert entry["answer"] == "No" and how == "intent"


def test_opposite_polarity_questions_never_merge():
    bank, _ = AB.save([], "Do you require visa sponsorship?", "No")
    bank, _ = AB.save(bank, "Are you authorized to work in the US without sponsorship?", "Yes")
    bank, _ = AB.save(bank, "How did you hear about us?", "LinkedIn")
    bank, _ = AB.save(bank, "Were you referred by an employee?", "No")
    assert len(bank) == 4
    assert AB.find_answer(bank, "Are you eligible to work in the United States?")[0]["answer"] == "Yes"


def test_protected_answers_need_verification_and_never_match_loosely():
    bank, _ = AB.save([], "What is your desired salary?", "$120,000", verified=False)
    assert AB.find_answer(bank, "What is your desired salary?") == (None, None)
    assert AB.find_answer(bank, "Salary expectations for this role?") == (None, None)
    bank, _ = AB.save([], "Why do you want to work on developer tools?", "I build them daily.")
    entry, how = AB.find_answer(bank, "Why do you want to work on developer tools here?")
    assert how == "similar" and entry["answer"] == "I build them daily."


def test_legacy_rows_still_read():
    rows = AB.entries([{"question": "Why fintech?", "answer": "Payments."}, {"Why us?": "Mission."}, "junk", {}])
    assert [r["question"] for r in rows] == ["Why fintech?", "Why us?"]
    assert all(r["user_verified"] and r["id"] for r in rows)


# ── protected questions never reach the model ────────────────────────────────

def _persona(db, bank=None):
    from backend.models.db import Persona, Setting
    db.add(Setting(key="dashboard_api_key", value=""))
    db.add(Setting(key="autofill_prompt", value="{persona} {qa_bank} {company} {position} {question} {max_chars}"))
    db.add(Persona(id=1, contact={"first_name": "Ada", "last_name": "Lovelace"}, work_auth={}, preferences={}, qa_bank=bank or []))
    db.commit()


def _no_llm(monkeypatch):
    import backend.api.routes_autofill as R

    async def boom(*a, **kw):
        raise AssertionError("a protected question reached the LLM")

    async def boom_stream(*a, **kw):
        raise AssertionError("a protected question reached the LLM")
        yield  # pragma: no cover

    monkeypatch.setattr(R, "call_autofill_llm", boom)
    monkeypatch.setattr(R, "call_autofill_llm_stream", boom_stream)


def test_protected_question_is_refused_without_a_saved_answer(api_client, test_db, monkeypatch):
    _persona(test_db)
    _no_llm(monkeypatch)
    for q in ("Do you require visa sponsorship?", "What is your gender?", "What are your salary expectations?",
              "I certify that the information provided is true and complete."):
        r = api_client.post("/api/autofill/answer", json={"question": q})
        assert r.status_code == 422 and r.json()["detail"].startswith("protected"), q
    stream = api_client.post("/api/autofill/answer/stream", json={"question": "Are you a protected veteran?"})
    assert '"protected": true' in stream.text and "delta" not in stream.text


def test_protected_question_uses_only_the_users_verified_answer(api_client, test_db, monkeypatch):
    bank, _ = AB.save([], "Will you now or in the future require sponsorship?", "No")
    _persona(test_db, bank)
    _no_llm(monkeypatch)
    r = api_client.post("/api/autofill/answer", json={"question": "Do you require employment visa sponsorship?"})
    assert r.status_code == 200 and r.json()["answer"] == "No" and r.json()["from_bank"]


def test_config_lists_protected_keys(api_client, test_db):
    _persona(test_db)
    keys = set(api_client.get("/api/autofill/config").json()["protected_keys"])
    assert {"requires_sponsorship_now", "gender", "veteran_status", "disability_status", "desired_salary"} <= keys


# ── job context, accepted résumé, application tracking ───────────────────────

def _job_with_versions(db, url="https://jobs.lever.co/acme/5f1c2b7e-aaaa"):
    from backend.models.db import Job, JobAnalysisRecord, ResumeVersion
    job = Job(external_id=str(uuid.uuid4()), company="Acme", title="Engineer", url=url)
    db.add(job)
    db.flush()
    db.add(JobAnalysisRecord(job_id=job.id, analysis={"requirements": []}, evidence=[], match={"score": 77}))
    draft = ResumeVersion(job_id=job.id, kind="tailored", status="draft", resume_json={}, pdf=b"%PDF draft")
    db.add(draft)
    db.commit()
    return job, draft


@pytest.mark.parametrize("page", [
    "https://jobs.lever.co/acme/5f1c2b7e-aaaa/apply?lever-source=LinkedIn",
    "https://jobs.lever.co/acme/5f1c2b7e-aaaa",
    "https://JOBS.lever.co/acme/5f1c2b7e-aaaa/#top",
])
def test_application_pages_match_their_saved_job(test_db, page):
    from backend.api.routes_autofill import match_job
    job, _ = _job_with_versions(test_db)
    assert match_job(test_db, page).id == job.id
    assert match_job(test_db, "https://jobs.lever.co/other/123") is None


def test_greenhouse_embed_matches_by_posting_id(test_db):
    from backend.api.routes_autofill import match_job
    job, _ = _job_with_versions(test_db, url="https://boards.greenhouse.io/acme/jobs/4455667")
    assert match_job(test_db, "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=4455667").id == job.id


def test_only_an_accepted_resume_is_offered_or_uploaded(api_client, test_db):
    _persona(test_db)
    job, draft = _job_with_versions(test_db)
    ctx = api_client.post("/api/autofill/job-context", json={"url": job.url + "/apply"}).json()
    assert ctx["job"]["id"] == str(job.id) and ctx["resume_version"] is None and ctx["match_score"] == 77
    assert api_client.get(f"/api/autofill/resume/{draft.id}").status_code == 404

    from backend.models.db import ResumeVersion, utcnow
    accepted = ResumeVersion(job_id=job.id, kind="tailored", status="accepted", accepted_at=utcnow(), resume_json={}, pdf=b"%PDF-1.4 ok")
    test_db.add(accepted)
    test_db.commit()
    ctx = api_client.post("/api/autofill/job-context", json={"url": job.url}).json()
    assert ctx["resume_version"]["id"] == str(accepted.id)
    pdf = api_client.get(f"/api/autofill/resume/{accepted.id}").json()
    assert base64.b64decode(pdf["base64"]) == b"%PDF-1.4 ok" and pdf["filename"] == "Ada_Lovelace_Resume.pdf"


def test_fill_records_ready_to_apply_and_applied_moves_it_on(api_client, test_db):
    _persona(test_db)
    job, draft = _job_with_versions(test_db)
    assert api_client.post("/api/autofill/filled", json={"url": job.url, "resume_version_id": str(draft.id)}).status_code == 400

    from backend.models.db import ResumeVersion, utcnow
    accepted = ResumeVersion(job_id=job.id, kind="tailored", status="accepted", accepted_at=utcnow(), resume_json={}, pdf=b"%PDF")
    test_db.add(accepted)
    test_db.commit()
    r = api_client.post("/api/autofill/filled", json={
        "url": job.url + "/apply", "resume_version_id": str(accepted.id), "ats": "lever",
        "answers_used": [{"label": "Email", "key": "email", "source": "profile"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready_to_apply" and body["match_score_at_apply"] == 77 and body["resume_version_id"] == str(accepted.id)

    applied = api_client.post("/api/applications", json={"title": job.title, "company": job.company, "url": job.url})
    assert applied.status_code == 200 and applied.json()["status"] == "applied"
    app = api_client.get("/api/applications").json()
    row = next(a for a in (app["applications"] if isinstance(app, dict) else app) if a["job_id"] == str(job.id))
    assert [t["to"] for t in row["status_transitions"]] == ["ready_to_apply", "applied"]
    assert row["answers_used"][0]["key"] == "email" and row["resume_version_id"] == str(accepted.id)
    assert api_client.post("/api/applications", json={"title": job.title, "company": job.company, "url": job.url}).status_code == 409


def test_new_stages_are_valid(api_client, test_db):
    _persona(test_db)
    job, _ = _job_with_versions(test_db)
    r = api_client.post("/api/autofill/filled", json={"job_id": str(job.id)})
    app_id = r.json()["application_id"]
    for stage in ("applied", "oa", "recruiter_screen", "final", "withdrawn"):
        assert api_client.patch(f"/api/applications/{app_id}", json={"status": stage}).status_code == 200, stage


async def test_semantic_field_mapping_returns_known_keys_only(api_client, test_db, monkeypatch):
    import backend.analyzer.llm_client as L
    from backend.copilot.schemas import FieldMapping
    _persona(test_db)
    seen = {}

    async def fake(schema, prompt, system, **kw):
        seen["prompt"] = prompt
        return FieldMapping(mappings=[{"field_id": "f1", "key": "linkedin"}, {"field_id": "f2", "key": "favorite_color"},
                                      {"field_id": "zzz", "key": "email"}]), "ollama", "m"
    monkeypatch.setattr(L, "call_structured", fake)
    r = api_client.post("/api/autofill/map-fields", json={"fields": [
        {"id": "f1", "label": "Your LinkedIn profile URL", "kind": "text"},
        {"id": "f2", "label": "Favourite colour", "kind": "text", "value": "SECRET"},
    ]})
    assert r.json() == {"mappings": [{"field_id": "f1", "key": "linkedin"}]}
    assert "SECRET" not in seen["prompt"]
