"""Structured PDF résumé extraction and its Resume.json_data compatibility."""

import pytest
from fastapi import HTTPException

from backend.analyzer.resume_schema import ResumeImport


RESUME_TEXT = "Software engineer with experience building reliable distributed systems. " * 2


def _mock_pdf_text(monkeypatch, text=RESUME_TEXT):
    import pdfplumber

    class Page:
        def extract_text(self):
            return text

    class Pdf:
        pages = [Page()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(pdfplumber, "open", lambda _stream: Pdf())


@pytest.mark.asyncio
async def test_parse_resume_pdf_uses_typed_structured_result_and_profile_contract(monkeypatch):
    _mock_pdf_text(monkeypatch)
    parsed = ResumeImport.model_validate({
        "header": {
            "name": "Mary Watson",
            "contact_items": [
                {"text": "mary@example.com", "url": "mailto:mary@example.com"},
                {"text": "LinkedIn", "url": "linkedin.com/in/mary"},
                {"text": "Boston, MA"},
            ],
        },
        "summary": "Software engineer focused on distributed systems.",
        "experience": [{
            "company": "Acme",
            "title": "Software Engineer",
            "location": "Boston, MA",
            "date": "2020–Present",
            "description": "Built backend services.",
            "bullets": ["Shipped the service.", "Reduced latency by 30%."],
        }],
        "skills": {"Languages": ["Python", "Go"], "Cloud": ["AWS"]},
        "education": [{
            "school": "State University",
            "location": "Boston, MA",
            "degree": "BS Computer Science",
            "years": "2018–2022",
            "year": "2022",
        }],
        "projects": [{
            "name": "Telemetry Toolkit",
            "description": "A service monitoring toolkit.",
            "bullets": ["Added tracing."],
        }],
        "publications": [{
            "title": "Reliable Services",
            "description": "A practical systems paper.",
            "venue": "Systems Journal",
        }],
    })
    seen = {}

    async def fake_call_structured(schema, **kwargs):
        seen["schema"] = schema
        seen["kwargs"] = kwargs
        return parsed, "ollama", "qwen3:8b"

    import backend.analyzer.llm_client as llm_client
    monkeypatch.setattr(llm_client, "call_structured", fake_call_structured)

    from backend.api.routes_resumes import parse_resume_pdf
    result = await parse_resume_pdf(b"pdf bytes", db=None)

    assert isinstance(result, dict)
    assert result["experience"][0]["bullets"] == ["Shipped the service.", "Reduced latency by 30%."]
    assert result["skills"] == {"Languages": ["Python", "Go"], "Cloud": ["AWS"]}
    assert result["education"][0]["years"] == "2018–2022"
    assert seen["schema"] is ResumeImport
    assert seen["kwargs"]["feature"] == "pdf"
    assert seen["kwargs"]["temperature"] == 0
    assert seen["kwargs"]["max_tokens"] >= 4000

    from backend.api.routes_profile import facts_from_resume_json
    facts = facts_from_resume_json(result)
    kinds = [fact["kind"] for fact in facts]
    assert "experience" in kinds
    assert "project" in kinds
    assert "education" in kinds
    assert kinds.count("skill") == 3
    assert "publication" in kinds
    experience = next(f for f in facts if f["kind"] == "experience")
    assert [child["data"]["text"] for child in experience["children"]] == [
        "Shipped the service.", "Reduced latency by 30%.",
    ]
    project = next(f for f in facts if f["kind"] == "project")
    assert [child["data"]["text"] for child in project["children"]] == ["Added tracing."]
    education = next(f["data"] for f in facts if f["kind"] == "education")
    assert education["graduation_date"] == "2022"
    publication = next(f["data"] for f in facts if f["kind"] == "publication")
    assert publication["venue"] == "A practical systems paper."

    from backend.api.routes_persona import _contact_from_header
    contact = _contact_from_header(result["header"])
    assert contact["first_name"] == "Mary"
    assert contact["last_name"] == "Watson"
    assert contact["email"] == "mary@example.com"
    assert contact["linkedin"] == "linkedin.com/in/mary"
    assert contact["city"] == "Boston"


@pytest.mark.asyncio
async def test_parse_resume_pdf_defaults_missing_and_empty_optional_fields(monkeypatch):
    _mock_pdf_text(monkeypatch)
    parsed = ResumeImport.model_validate({})

    async def fake_call_structured(*_args, **_kwargs):
        return parsed, "ollama", "qwen3:8b"

    import backend.analyzer.llm_client as llm_client
    monkeypatch.setattr(llm_client, "call_structured", fake_call_structured)

    from backend.api.routes_resumes import parse_resume_pdf
    result = await parse_resume_pdf(b"pdf bytes", db=None)

    assert result == {
        "header": {"name": "", "contact_items": []},
        "summary": "",
        "experience": [],
        "skills": {},
        "education": [],
        "projects": [],
        "publications": [],
    }


@pytest.mark.asyncio
async def test_parse_resume_pdf_maps_structured_validation_failure_to_422(monkeypatch):
    _mock_pdf_text(monkeypatch)

    from backend.analyzer.llm_client import StructuredOutputError

    async def fake_call_structured(*_args, **_kwargs):
        raise StructuredOutputError("ResumeImport failed validation")

    import backend.analyzer.llm_client as llm_client
    monkeypatch.setattr(llm_client, "call_structured", fake_call_structured)

    from backend.api.routes_resumes import parse_resume_pdf
    with pytest.raises(HTTPException) as error:
        await parse_resume_pdf(b"pdf bytes", db=None)
    assert error.value.status_code == 422
    assert "expected format" in error.value.detail


@pytest.mark.asyncio
async def test_parse_resume_pdf_maps_provider_failure_to_useful_500(monkeypatch):
    _mock_pdf_text(monkeypatch)

    async def fake_call_structured(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    import backend.analyzer.llm_client as llm_client
    monkeypatch.setattr(llm_client, "call_structured", fake_call_structured)

    from backend.api.routes_resumes import parse_resume_pdf
    with pytest.raises(HTTPException) as error:
        await parse_resume_pdf(b"pdf bytes", db=None)
    assert error.value.status_code == 500
    assert "provider" in error.value.detail
    assert "provider unavailable" not in error.value.detail


@pytest.mark.asyncio
async def test_parse_resume_pdf_keeps_scanned_pdf_text_threshold(monkeypatch):
    _mock_pdf_text(monkeypatch, "too short")

    from backend.api.routes_resumes import parse_resume_pdf
    with pytest.raises(HTTPException) as error:
        await parse_resume_pdf(b"pdf bytes", db=None)
    assert error.value.status_code == 422
    assert "image-based" in error.value.detail


@pytest.mark.asyncio
async def test_call_structured_ollama_passes_schema_and_disables_thinking(monkeypatch):
    import httpx
    import backend.analyzer.llm_client as llm_client
    import backend.analyzer.llm_logger as llm_logger

    monkeypatch.setattr(llm_client, "resolve_llm_config", lambda _feature: {
        "provider": "ollama", "model": "qwen3:8b", "api_key": "",
    })
    monkeypatch.setattr(llm_client, "ollama_base_url", lambda: "http://ollama")

    class Tracker:
        def record(self, _result):
            pass

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_track(*_args, **_kwargs):
        yield Tracker()

    monkeypatch.setattr(llm_logger, "track_llm_call", fake_track)
    request = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "{}"}, "prompt_eval_count": 10, "eval_count": 2}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, json):
            request["url"] = url
            request["body"] = json
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)

    parsed, provider, model = await llm_client.call_structured(
        ResumeImport,
        prompt="Parse the resume.",
        system="Extract only explicit facts.",
        feature="pdf",
        max_tokens=6000,
        temperature=0,
    )

    assert isinstance(parsed, ResumeImport)
    assert provider == "ollama"
    assert model == "qwen3:8b"
    assert request["url"] == "http://ollama/api/chat"
    assert request["body"]["format"] == ResumeImport.model_json_schema()
    assert request["body"]["think"] is False
    assert request["body"]["options"]["temperature"] == 0
    assert request["body"]["messages"] == [
        {"role": "system", "content": "Extract only explicit facts."},
        {"role": "user", "content": "Parse the resume."},
    ]


@pytest.mark.asyncio
async def test_freeform_ollama_call_keeps_generate_path_without_think_flag(monkeypatch):
    import httpx
    import backend.analyzer.llm_client as llm_client

    monkeypatch.setattr(llm_client, "ollama_base_url", lambda: "http://ollama")
    request = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": "freeform", "prompt_eval_count": 3, "eval_count": 1}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, json):
            request["url"] = url
            request["body"] = json
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    result = await llm_client._call_ollama("p", "s", "qwen3:8b", 128)

    assert result["text"] == "freeform"
    assert request["url"] == "http://ollama/api/generate"
    assert "think" not in request["body"]
    assert "format" not in request["body"]
