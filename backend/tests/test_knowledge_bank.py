import io
import zipfile

import pytest

from backend.copilot import knowledge
from backend.models.db import CandidateFact, KnowledgeChunk, KnowledgeDocument


def _docx(text):
    body = "".join(
        f"<w:p><w:r><w:t>{part}</w:t></w:r></w:p>"
        for part in text.split("\n")
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    ).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return out.getvalue()


def test_supported_documents_extract_text_and_preserve_structure(monkeypatch):
    assert "AWS" in knowledge.extract_document_text(b"# Experience\n\n- AWS", "notes.md")
    assert knowledge.extract_document_text(b"Research notes", "research.txt") == "Research notes"
    assert "Research notes" in knowledge.extract_document_text(_docx("Research notes\nSQS"), "notes.docx")
    assert knowledge.chunk_document("# Experience\n\n- Built SQS\n- Added SNS\n\n# Education\n\nState U") == [
        "# Experience\n- Built SQS\n- Added SNS", "# Education\nState U"
    ]


@pytest.mark.asyncio
async def test_index_is_idempotent_and_searches_chunks_and_canonical_facts(test_db, monkeypatch):
    document = KnowledgeDocument(
        filename="notes.md", mime_type="text/markdown", sha256="a" * 64,
        raw_text="# Experience\n\n- Built SQS\n\n# Other\n\n- Built a UI",
    )
    test_db.add(document)
    test_db.commit()
    calls = []

    async def fake_embed(texts, db):
        calls.append(texts)
        return [[1.0, 0.0] if "SQS" in text or "AWS" in text else [0.0, 1.0] for text in texts]

    monkeypatch.setattr(knowledge, "embed_texts", fake_embed)
    assert await knowledge.index_document(test_db, document.id) == 2
    test_db.commit()
    assert len(calls) == 1
    assert await knowledge.index_document(test_db, document.id) == 2
    assert len(calls) == 1, "unchanged chunks must not be re-embedded"

    fact = CandidateFact(kind="skill", data={"name": "AWS messaging"})
    test_db.add(fact)
    test_db.commit()
    assert await knowledge.index_facts(test_db) == 1
    test_db.commit()
    assert fact.embedding and fact.embedding_text_sha256

    results = await knowledge.search(test_db, "AWS messaging", limit=3)
    assert results and "SQS" in results[0]["text"]
    assert test_db.query(KnowledgeChunk).count() == 2


def test_knowledge_upload_accepts_text_document(api_client, test_db, monkeypatch):
    monkeypatch.setattr("backend.api.routes_profile.launch_background", lambda *args, **kwargs: "run-1")
    response = api_client.post(
        "/api/profile/resumes",
        files=[("files", ("internship-notes.md", io.BytesIO(b"Built SQS and SNS"), "text/markdown"))],
    )
    assert response.status_code == 202
    assert response.json()["accepted"][0]["mime_type"] == "text/markdown"
    assert test_db.query(KnowledgeDocument).one().raw_text == "Built SQS and SNS"


@pytest.mark.asyncio
async def test_claim_extraction_keeps_quote_chunk_and_parent(monkeypatch):
    async def fake_call_structured(schema, **kwargs):
        return knowledge.KnowledgeExtraction(claims=[
            knowledge.KnowledgeClaim(
                kind="experience", entity="Acme", data={"title": "Intern"},
                source_quote="Acme Intern",
            ),
            knowledge.KnowledgeClaim(
                kind="achievement", parent_hint="Acme", data={"text": "Built SQS"},
                source_quote="Built SQS",
            ),
        ]), "ollama", "local"

    import backend.analyzer.llm_client as llm_client
    monkeypatch.setattr(llm_client, "call_structured", fake_call_structured)
    claims, provider, model = await knowledge.extract_claims("Acme Intern\n\n- Built SQS")
    assert provider == "ollama" and model == "local"
    assert claims[0]["children"][0]["data"]["text"] == "Built SQS"
    assert claims[0]["children"][0]["locator"] == "chunk:1"


@pytest.mark.asyncio
async def test_embedding_uses_legacy_endpoint_when_current_endpoint_is_missing(test_db, monkeypatch):
    import httpx
    from unittest.mock import AsyncMock, MagicMock

    current = MagicMock(status_code=404)
    legacy = MagicMock(status_code=200)
    legacy.json.return_value = {"embedding": [1.0, 0.0]}
    legacy.raise_for_status = MagicMock()
    client = MagicMock()
    client.post = AsyncMock(side_effect=[current, legacy])
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)

    assert await knowledge.embed_texts(["AWS messaging"], test_db) == [[1.0, 0.0]]
    assert client.post.call_args_list[1].args[0].endswith("/api/embeddings")
