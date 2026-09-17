"""Small local Knowledge Bank primitives: extraction, chunks, embeddings, search."""
import hashlib
import math
import mimetypes
import re
import zipfile
from io import BytesIO
from typing import Literal
from xml.etree import ElementTree

from pydantic import BaseModel, Field

from backend.analyzer.llm_client import ollama_base_url
from backend.models.db import CandidateFact, KnowledgeChunk, KnowledgeDocument, Setting

SUPPORTED_EXTENSIONS = {".pdf", ".md", ".txt", ".docx"}
DEFAULT_EMBEDDING_MODEL = "embeddinggemma"


class KnowledgeClaim(BaseModel):
    kind: Literal[
        "experience", "internship", "education", "research", "project",
        "achievement", "skill", "certification", "publication", "link",
    ]
    entity: str = ""
    relationship: str = ""
    data: dict = Field(default_factory=dict)
    parent_hint: str = ""
    source_quote: str = ""


class KnowledgeExtraction(BaseModel):
    claims: list[KnowledgeClaim] = Field(default_factory=list)


def mime_type_for(filename: str) -> str:
    suffix = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    return {"md": "text/markdown", "txt": "text/plain", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "pdf": "application/pdf"}.get(suffix, mimetypes.guess_type(filename)[0] or "application/octet-stream")


def extract_document_text(content: bytes, filename: str) -> str:
    """Extract supported document text without retaining the original bytes."""
    suffix = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    if f".{suffix}" not in SUPPORTED_EXTENSIONS:
        raise ValueError("supported document types are PDF, Markdown, TXT, and DOCX")
    if suffix in {"md", "txt"}:
        text = content.decode("utf-8-sig", errors="replace")
    elif suffix == "docx":
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                root = ElementTree.fromstring(archive.read("word/document.xml"))
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            paragraphs = []
            for paragraph in root.findall(".//w:p", ns):
                value = "".join(node.text or "" for node in paragraph.findall(".//w:t", ns)).strip()
                if value:
                    paragraphs.append(value)
            text = "\n\n".join(paragraphs)
        except (KeyError, ElementTree.ParseError, zipfile.BadZipFile) as exc:
            raise ValueError(f"failed to read DOCX: {exc}") from exc
    else:
        import pdfplumber
        with pdfplumber.open(BytesIO(content)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    if not text.strip():
        raise ValueError("could not extract text from document")
    return text.strip()


def chunk_document(raw_text: str) -> list[str]:
    """Group headings, paragraphs, and adjacent bullets into retrievable chunks."""
    chunks, current, bullet_mode = [], [], False
    heading = re.compile(r"^(#{1,6}\s+|[A-Z][A-Z0-9 &/+-]{2,}:?\s*$)")
    bullet = re.compile(r"^\s*(?:[-*•] |\d+[.)] )")

    def flush():
        if current:
            chunks.append("\n".join(current).strip())
            current.clear()

    for line in (raw_text or "").replace("\r\n", "\n").splitlines():
        line = line.strip()
        if not line:
            # A heading owns the structured block immediately below it.
            if current and len(current) == 1 and heading.match(current[0]):
                bullet_mode = False
                continue
            flush()
            bullet_mode = False
            continue
        is_bullet = bool(bullet.match(line))
        if heading.match(line) and current:
            flush()
        elif bullet_mode and not is_bullet:
            flush()
        current.append(line)
        bullet_mode = is_bullet
    flush()
    return chunks


def _setting(db, key: str, default: str) -> str:
    row = db.query(Setting).filter(Setting.key == key).first()
    return row.value if row and row.value else default


def _sha(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def fact_text(fact: CandidateFact) -> str:
    """Stable text representation used for canonical-claim retrieval."""
    import json
    return f"{fact.kind}: {json.dumps(fact.data or {}, sort_keys=True, ensure_ascii=False)}"


def _nested_claims(claims: list[dict]) -> list[dict]:
    """Attach achievement claims to the extracted parent they name."""
    parents = [claim for claim in claims if claim["kind"] != "achievement"]
    output = [claim for claim in claims if claim["kind"] != "achievement"]
    for claim in (claim for claim in claims if claim["kind"] == "achievement"):
        hint = (claim.get("parent_hint") or "").casefold().strip()
        parent = next((candidate for candidate in parents if hint and hint in (
            candidate.get("entity") or candidate.get("data", {}).get("employer")
            or candidate.get("data", {}).get("name") or candidate.get("data", {}).get("organization") or ""
        ).casefold()), None)
        if parent is None:
            output.append(claim)
        else:
            parent.setdefault("children", []).append(claim)
    return output


async def index_facts(db, fact_ids: list[int] | None = None) -> int:
    """Embed canonical claims only when their normalized content changed."""
    query = db.query(CandidateFact)
    if fact_ids:
        query = query.filter(CandidateFact.id.in_(fact_ids))
    rows, pending = query.all(), []
    for fact in rows:
        text = fact_text(fact)
        digest = _sha(text)
        if fact.embedding_text_sha256 != digest:
            fact.embedding = None
            fact.embedding_text_sha256 = digest
        if fact.embedding is None:
            pending.append((fact, text))
    if pending:
        vectors = await embed_texts([text for _, text in pending], db)
        for (fact, _), vector in zip(pending, vectors):
            fact.embedding = vector
    return len(rows)


async def embed_texts(texts: list[str], db) -> list[list[float]]:
    """Batch embeddings, with one legacy-endpoint fallback for older Ollama."""
    if not texts:
        return []
    import httpx
    model = _setting(db, "knowledge_embedding_model", DEFAULT_EMBEDDING_MODEL)
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{ollama_base_url(db)}/api/embed",
            json={"model": model, "input": texts},
        )
        if response.status_code == 404:
            # Older Ollama releases expose only /api/embeddings and accept one
            # prompt at a time. A model-not-found 404 is re-raised below with
            # setup guidance instead of being mistaken for a search result.
            try:
                embeddings = []
                for value in texts:
                    legacy = await client.post(
                        f"{ollama_base_url(db)}/api/embeddings",
                        json={"model": model, "prompt": value},
                    )
                    legacy.raise_for_status()
                    embedding = legacy.json().get("embedding")
                    if not embedding:
                        raise ValueError("legacy Ollama response did not include an embedding")
                    embeddings.append(embedding)
            except Exception as exc:
                raise RuntimeError(
                    f"Ollama embedding model '{model}' is unavailable at {ollama_base_url(db)}. "
                    f"Pull it with ollama pull {model} or choose an installed embedding model."
                ) from exc
        else:
            response.raise_for_status()
            embeddings = response.json().get("embeddings") or []
    if len(embeddings) != len(texts):
        raise ValueError("Ollama returned the wrong number of embeddings")
    return embeddings


async def index_document(db, document_id: int) -> int:
    """Upsert structure-aware chunks and embed only new/changed text."""
    document = db.get(KnowledgeDocument, document_id)
    if document is None:
        return 0
    texts = chunk_document(document.raw_text or "")
    existing = {row.chunk_index: row for row in db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id == document_id).all()}
    pending, rows = [], []
    for index, text in enumerate(texts):
        digest = _sha(text)
        row = existing.get(index)
        if row is None:
            row = KnowledgeChunk(document_id=document_id, chunk_index=index, text=text, text_sha256=digest)
            db.add(row)
        elif row.text_sha256 != digest:
            row.text, row.text_sha256, row.embedding = text, digest, None
        rows.append(row)
        if row.embedding is None:
            pending.append((row, text))
    stale = [row for index, row in existing.items() if index >= len(texts)]
    for row in stale:
        db.delete(row)
    db.flush()
    if pending:
        vectors = await embed_texts([text for _, text in pending], db)
        for (row, _), vector in zip(pending, vectors):
            row.embedding = vector
    return len(rows)


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    return dot / norm if norm else 0.0


async def search(db, query: str, limit: int = 20) -> list[dict]:
    """Return the nearest persisted chunks; the LLM never invents search results."""
    query = (query or "").strip()
    if not query:
        return []
    vector = (await embed_texts([query], db))[0]
    chunks = (db.query(KnowledgeChunk, KnowledgeDocument)
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
            .filter(KnowledgeChunk.embedding.isnot(None)).all())
    ranked = [(_cosine(vector, row.embedding), {"chunk_id": row.id, "document_id": document.id,
               "filename": document.filename, "text": row.text}) for row, document in chunks]
    for fact in db.query(CandidateFact).filter(CandidateFact.embedding.isnot(None)).all():
        ranked.append((_cosine(vector, fact.embedding), {"fact_id": fact.id, "kind": fact.kind,
                       "text": fact_text(fact), "verified": fact.verified}))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [{**item, "score": round(score, 6)} for score, item in ranked[:max(1, min(limit, 100))]]


async def extract_claims(raw_text: str) -> tuple[list[dict], str, str]:
    """Ask Ollama for atomic, source-grounded claims and preserve quotes."""
    from backend.analyzer.llm_client import call_structured
    result, provider, model = await call_structured(
        KnowledgeExtraction,
        prompt=("Extract atomic career claims from this document. Include only claims supported by the source; "
                 "do not infer missing values or create metrics, technologies, dates, roles, leadership, or outcomes. "
                 "Copy a short source_quote for every claim.\n\nDOCUMENT:\n" + raw_text),
        system="You extract evidence, not conclusions. Never invent information absent from the document.",
        feature="knowledge",
        max_tokens=6000,
        temperature=0,
    )
    claims = []
    chunks = chunk_document(raw_text)
    for claim in result.claims:
        item = claim.model_dump()
        quote = item.get("source_quote") or ""
        item["locator"] = f"chunk:{next((i for i, chunk in enumerate(chunks) if quote and quote in chunk), 0)}"
        item["raw"] = quote
        data = dict(item.get("data") or {})
        if claim.entity:
            data.setdefault({"experience": "employer", "internship": "employer", "project": "name", "research": "organization", "skill": "name", "education": "institution", "certification": "name", "publication": "title", "link": "label"}.get(claim.kind, ""), claim.entity)
        item["data"] = data
        claims.append(item)
    return _nested_claims(claims), provider, model
