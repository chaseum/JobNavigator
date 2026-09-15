"""Role Match: evidence integrity, deterministic scoring, gap categories, structured LLM calls."""
import json

import pytest

from backend.copilot import matching as M

REQS = [
    {"id": "r1", "text": "Python", "category": "technology", "required": True, "importance": 3},
    {"id": "r2", "text": "Kubernetes", "category": "technology", "required": True, "importance": 2},
    {"id": "r3", "text": "Must be authorized to work in the US without sponsorship", "category": "work_authorization", "required": True, "importance": 3},
    {"id": "r4", "text": "Build data pipelines", "category": "responsibility", "required": True, "importance": 2},
]
FACTS = {
    "project_1": {"kind": "project", "data": {"name": "EEG pipeline"}},
    "internship_2": {"kind": "internship", "data": {"employer": "CED", "title": "Intern"}},
    "skill_3": {"kind": "skill", "data": {"name": "Go", "evidence_ids": []}},
}


def _status(evidence, rid):
    return next(e for e in evidence if e["requirement_id"] == rid)


def test_keyword_claim_without_a_cited_fact_earns_nothing():
    ev = M.sanitize_evidence(REQS[:1], [{"requirement_id": "r1", "status": "MATCHED", "source_fact_ids": []}], FACTS, {})
    assert ev[0]["status"] == "MISSING"
    assert M.score(REQS[:1], ev)["score"] == 0


def test_citing_a_fact_that_does_not_exist_is_dropped_not_trusted():
    ev = M.sanitize_evidence(REQS[:1], [{"requirement_id": "r1", "status": "MATCHED", "source_fact_ids": ["project_999", "Python"]}], FACTS, {})
    assert ev[0]["status"] == "MISSING" and ev[0]["dropped_ids"] == ["project_999", "Python"]


def test_a_listed_skill_without_usage_evidence_is_partial_at_most():
    reqs = [{"id": "r1", "text": "Go", "category": "technology", "required": True, "importance": 2}]
    ev = M.sanitize_evidence(reqs, [{"requirement_id": "r1", "status": "MATCHED", "source_fact_ids": ["skill_3"]}], FACTS, {})
    assert ev[0]["status"] == "PARTIAL"


def test_work_authorization_comes_from_the_users_answers_never_the_model():
    llm_says_yes = [{"requirement_id": "r3", "status": "MATCHED", "source_fact_ids": ["internship_2"]}]
    ev = M.sanitize_evidence(REQS[2:3], llm_says_yes, FACTS, {"identity.requires_sponsorship_future": True, "identity.authorized_us": True})
    assert ev[0]["status"] == "MISSING"
    assert M.score(REQS[2:3], ev)["hard_blockers"] == [REQS[2]["text"]]

    unanswered = M.sanitize_evidence(REQS[2:3], llm_says_yes, FACTS, {})
    assert unanswered[0]["status"] == "UNKNOWN" and unanswered[0]["source_fact_ids"] == []

    ok = M.sanitize_evidence(REQS[2:3], [], FACTS, {"identity.authorized_us": True, "identity.requires_sponsorship_now": False,
                                                    "identity.requires_sponsorship_future": False})
    assert ok[0]["status"] == "MATCHED" and "identity.authorized_us" in ok[0]["source_fact_ids"]


def test_every_requirement_gets_a_row_and_unmentioned_ones_are_unknown():
    ev = M.sanitize_evidence(REQS, [], FACTS, {})
    assert [e["requirement_id"] for e in ev] == ["r1", "r2", "r3", "r4"]
    assert {e["status"] for e in ev} == {"UNKNOWN"}


def test_score_exposes_components_and_reweights_around_empty_ones():
    ev = M.sanitize_evidence(REQS, [
        {"requirement_id": "r1", "status": "MATCHED", "source_fact_ids": ["project_1"]},
        {"requirement_id": "r2", "status": "MISSING"},
        {"requirement_id": "r4", "status": "PARTIAL", "source_fact_ids": ["internship_2"]},
    ], FACTS, {"identity.authorized_us": True, "identity.requires_sponsorship_now": False, "identity.requires_sponsorship_future": False})
    result = M.score(REQS, ev)
    comps = {c["name"]: c for c in result["components"]}
    # required: r1 (3, matched) + r2 (2, missing) -> 3/5; technology the same; eligibility 1; experience 0.5
    assert comps["required"]["coverage"] == 0.6 and comps["technology"]["coverage"] == 0.6
    assert comps["eligibility"]["coverage"] == 1.0 and comps["experience"]["coverage"] == 0.5
    assert comps["preferred"]["coverage"] is None and comps["parser_health"]["coverage"] is None
    expected = (30 * 1.0 + 30 * 0.6 + 15 * 0.5 + 10 * 0.6) / 85 * 100
    assert result["score"] == round(expected)
    assert result["counts"] == {"MATCHED": 2, "PARTIAL": 1, "UNKNOWN": 0, "MISSING": 1}

    heavy_eligibility = M.score(REQS, ev, weights={"eligibility": 100, "required": 0, "technology": 0, "experience": 0})
    assert heavy_eligibility["score"] == 100
    assert M.score(REQS, ev, parser_health=0)["score"] < result["score"]


def test_gaps_never_offer_a_missing_requirement_and_split_supported_ones():
    ev = M.sanitize_evidence(REQS[:2] + REQS[3:], [
        {"requirement_id": "r1", "status": "MATCHED", "source_fact_ids": ["project_1"]},
        {"requirement_id": "r2", "status": "MISSING", "source_fact_ids": []},
        {"requirement_id": "r4", "status": "MATCHED", "source_fact_ids": ["internship_2"]},
    ], FACTS, {})
    reqs = REQS[:2] + REQS[3:]
    by = {g["requirement_id"]: g for g in M.gaps(reqs, ev, "Intern at CED. Wrote ETL jobs.", {"internship_2"})}
    assert by["r2"]["kind"] == "cannot_claim" and by["r2"]["source_fact_ids"] == []
    assert by["r1"]["kind"] == "safe_to_add"            # project_1 is not on the résumé
    assert by["r4"]["kind"] == "safe_to_rephrase"       # on the résumé, but not in the job's words
    assert all(g["kind"] == "safe_to_add" for g in M.gaps(reqs, ev, None, None) if g["requirement_id"] != "r2")


# ── structured output ────────────────────────────────────────────────────────

def _settings(db, **kv):
    from backend.models.db import Setting
    for k, v in kv.items():
        db.add(Setting(key=k, value=v))
    db.commit()


async def test_ollama_structured_call_sends_the_schema_and_temperature_zero(test_db, mock_httpx):
    from backend.analyzer.llm_client import call_structured
    from backend.copilot.schemas import EvidenceMapping
    _settings(test_db, llm_provider="ollama", llm_model="any-local-model", ollama_base_url="http://ollama.test:11434")
    mock_httpx["response"].json.return_value = {"message": {"content": json.dumps(
        {"matches": [{"requirement_id": "r1", "status": "MATCHED", "source_fact_ids": ["project_1"]}]})}}

    out, provider, model = await call_structured(EvidenceMapping, "p", "s", feature="copilot")
    assert provider == "ollama" and model == "any-local-model"
    assert out.matches[0].source_fact_ids == ["project_1"]
    url, = mock_httpx["client"].post.call_args.args
    body = mock_httpx["client"].post.call_args.kwargs["json"]
    assert url == "http://ollama.test:11434/api/chat"
    assert body["format"] == EvidenceMapping.model_json_schema()
    assert body["options"]["temperature"] == 0
    assert body["think"] is False


async def test_structured_output_that_fails_the_schema_is_an_error_not_a_guess(test_db, mock_httpx):
    from backend.analyzer.llm_client import StructuredOutputError, call_structured
    from backend.copilot.schemas import EvidenceMapping
    _settings(test_db, llm_provider="ollama", llm_model="m")
    mock_httpx["response"].json.return_value = {"message": {"content": json.dumps({"matches": [{"status": "PROBABLY"}]})}}
    with pytest.raises(StructuredOutputError):
        await call_structured(EvidenceMapping, "p", "s")
    assert mock_httpx["client"].post.call_count == 2


async def test_ollama_without_a_model_is_a_configuration_error(test_db, mock_httpx):
    from backend.analyzer.llm_client import NonRetryableLLMError, call_structured
    from backend.copilot.schemas import EvidenceMapping
    _settings(test_db, llm_provider="ollama")
    with pytest.raises(NonRetryableLLMError, match="model"):
        await call_structured(EvidenceMapping, "p", "s")


# ── end to end through the DB and the workspace endpoint ─────────────────────

async def test_analysis_and_match_persist_and_render_the_requirement_matrix(api_client, test_db, monkeypatch):
    import backend.copilot.analysis as A
    from backend.copilot.schemas import EvidenceMapping, JobAnalysis
    from backend.models.db import CandidateFact, Job, Persona

    test_db.add(Persona(id=1, contact={}, work_auth={"authorized_us": True, "requires_sponsorship_now": False,
                                                    "requires_sponsorship_future": False}, preferences={}))
    job = Job(external_id="x1", company="Acme", title="Data Engineer", url="https://acme.test/1",
              description="""Data Engineer builds reliable data services for product teams and customers.
Responsibilities include designing, implementing, testing, and operating scalable pipelines.
The team needs Python for production data processing and Python and Kubernetes experience.
Must be authorized to work in the US. Engineers communicate clearly and document decisions.
The full-time role works across analytics, infrastructure, security, and application teams.
Candidates maintain reliable systems, review code, and improve service quality over time.""")
    test_db.add(job)
    proj = CandidateFact(kind="project", data={"name": "EEG pipeline", "technologies": ["Python"]}, verified=True)
    unverified = CandidateFact(kind="project", data={"name": "K8s homelab"}, verified=False)
    test_db.add_all([proj, unverified])
    test_db.commit()
    job_id, proj_ref, unverified_ref = str(job.id), f"project_{proj.id}", f"project_{unverified.id}"

    async def fake_structured(schema, prompt, system, **kw):
        if schema is JobAnalysis:
            return JobAnalysis(company="Acme", title="Data Engineer", requirements=[
                {"text": "Python", "source_quote": "The team needs Python for production data processing", "category": "technology", "required": True, "importance": 3},
                {"text": "python", "source_quote": "The team needs Python for production data processing", "category": "technology", "required": True},  # duplicate, dropped
                {"text": "Kubernetes", "source_quote": "Python and Kubernetes experience", "category": "technology", "required": True},
                {"text": "Authorized to work in the US", "source_quote": "Must be authorized to work in the US", "category": "work_authorization", "required": True},
            ]), "ollama", "m"
        assert unverified_ref not in prompt, "unverified facts must never reach the matcher"
        return EvidenceMapping(matches=[
            {"requirement_id": "r1", "status": "MATCHED", "source_fact_ids": [proj_ref]},
            {"requirement_id": "r2", "status": "MATCHED", "source_fact_ids": [unverified_ref]},
        ]), "ollama", "m"

    monkeypatch.setattr(A, "call_structured", fake_structured)
    summary = await A.run_analysis(job_id)
    assert "3 requirements" in summary

    ws = api_client.get(f"/api/copilot/jobs/{job_id}").json()
    by_text = {r["text"]: r for r in ws["requirements"]}
    assert by_text["Python"]["status"] == "MATCHED" and by_text["Python"]["evidence"][0]["headline"].startswith("Project: EEG")
    assert by_text["Kubernetes"]["status"] == "MISSING" and by_text["Kubernetes"]["evidence"] == []
    assert by_text["Authorized to work in the US"]["status"] == "MATCHED"
    assert by_text["Authorized to work in the US"]["evidence"][0]["headline"] == "Your answer: authorized to work in the US — yes"
    assert any(g["requirement"] == "Kubernetes" and g["kind"] == "cannot_claim" for g in ws["gaps"])
    assert ws["stale"] is False and ws["match"]["score"] > 0

    test_db.expire_all()
    assert test_db.get(Job, job.id).cv_scores["Role Match"] == ws["match"]["score"]

    # new context from the gap screen becomes a verified fact first, then the match reruns against it
    r = api_client.post(f"/api/copilot/jobs/{job_id}/context", json={"kind": "project", "data": {"name": "Helm charts at work"}})
    assert r.status_code == 201 and r.json()["fact"]["verified"] is True and r.json()["fact"]["source"] == "gap"
    assert r.json()["rematch"]["run_id"]
    await A.run_match(job_id)
    assert api_client.get(f"/api/copilot/jobs/{job_id}").json()["stale"] is False


def test_privacy_marks_every_non_ollama_feature_external(api_client, test_db):
    _settings(test_db, llm_provider="ollama", llm_model="m", autofill_llm_provider="openai")
    rows = {r["feature"]: r for r in api_client.get("/api/copilot/privacy").json()["features"]}
    assert rows["copilot"]["external"] is False and rows["autofill"]["external"] is True


def test_weights_setting_is_validated(api_client, test_db):
    from backend.seed import invalid_setting_values
    assert invalid_setting_values({"role_match_weights": '{"required": 40}'}) == []
    assert invalid_setting_values({"role_match_weights": '{"vibes": 40}'})
    assert invalid_setting_values({"role_match_weights": '{"required": -1}'})
