"""
agents/rag.py

RAG over Google ADK documentation using:
  - OpenAI text-embedding-3-small  (best accuracy embedding model)
  - ChromaDB                       (local persistent vector store)

Usage:
  1. Drop adk_docs.txt in the project root (the llms-full.txt from adk.dev)
  2. On first run, build_index() chunks + embeds + persists to ./adk_chroma_db/
  3. On subsequent runs, loads existing index instantly (no re-embedding)
  4. Call retrieve(adr) to get the most relevant ADK doc sections for a given ADR

The index is built ONCE and reused across all sessions.
Re-index only needed when adk_docs.txt is updated (call build_index(force=True)).
"""

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.utils import embedding_functions

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

DOCS_FILE        = Path("adk_docs.txt")          # drop this file in project root
CHROMA_DIR       = Path("adk_chroma_db")          # persisted ChromaDB directory
COLLECTION_NAME  = "adk_docs"
HASH_FILE        = CHROMA_DIR / "docs_hash.txt"   # tracks which version is indexed

CHUNK_SIZE       = 500    # tokens (approximate — we use word count / 0.75)
CHUNK_OVERLAP    = 80     # overlapping words between chunks for context continuity
TOP_K_PER_QUERY  = 4      # chunks retrieved per query (raised from 3)
MAX_CHUNKS_TOTAL = 20     # max chunks injected into generator prompt (raised from 12)
MAX_INJECT_CHARS = 50_000 # hard cap on total injected text (~12.5k tokens, raised from 14k)

# Default embedding model — overridden by EMBEDDING_MODEL env var
_DEFAULT_EMBED_MODEL = "text-embedding-3-small"


def _embedding_config() -> dict:
    """
    Read embedding gateway config from environment variables.

    All three must be set (via .env or shell environment):
      EMBEDDING_API_KEY   — API key for your internal gateway
      EMBEDDING_BASE_URL  — gateway base URL, e.g. https://my-gateway.company.com/v1
      EMBEDDING_MODEL     — embedding model name (e.g. text-embedding-3-small)
    """
    api_key  = os.environ.get("EMBEDDING_API_KEY")
    base_url = os.environ.get("EMBEDDING_BASE_URL")
    model    = os.environ.get("EMBEDDING_MODEL", _DEFAULT_EMBED_MODEL)

    missing = [k for k, v in [("EMBEDDING_API_KEY", api_key), ("EMBEDDING_BASE_URL", base_url)] if not v]
    if missing:
        raise EnvironmentError(
            f"Missing required env vars: {', '.join(missing)}\n"
            "Set them in your .env file or shell environment."
        )
    return {"api_key": api_key, "base_url": base_url, "model": model}


def _make_embedding_function(cfg: dict):
    """Build a ChromaDB-compatible embedding function from gateway config."""
    kwargs = {
        "api_key":    cfg["api_key"],
        "model_name": cfg["model"],
    }
    if cfg["base_url"]:
        kwargs["api_base"] = cfg["base_url"]
    return embedding_functions.OpenAIEmbeddingFunction(**kwargs)


# ── Chunking ──────────────────────────────────────────────────────────────────

def _chunk_text(text: str) -> list[dict]:
    """
    Split ADK docs into overlapping chunks.

    Strategy:
    - First split on ## headings to respect document structure
    - If a section is larger than CHUNK_SIZE words, split further with overlap
    - Each chunk carries its section title as metadata for debugging

    Returns list of {"text": str, "section": str, "chunk_id": str}
    """
    raw_chunks = []

    # Split on markdown headings (## or ###)
    sections = re.split(r"\n(?=#{1,3} )", text)

    for section in sections:
        if not section.strip():
            continue

        # Extract heading as metadata
        heading_match = re.match(r"^(#{1,3})\s+(.+)", section)
        section_title = heading_match.group(2).strip() if heading_match else "General"

        words = section.split()
        word_count = len(words)
        approx_tokens = int(word_count / 0.75)

        if approx_tokens <= CHUNK_SIZE:
            chunk_text = section.strip()
            if len(chunk_text) > 50:
                raw_chunks.append({"text": chunk_text, "section": section_title})
        else:
            step = max(1, int(CHUNK_SIZE * 0.75) - CHUNK_OVERLAP)
            i = 0
            while i < word_count:
                window = words[i: i + int(CHUNK_SIZE * 0.75)]
                chunk_text = " ".join(window).strip()
                if len(chunk_text) > 50:
                    raw_chunks.append({"text": chunk_text, "section": section_title})
                i += step

    # Assign globally unique IDs using position index — avoids MD5 collisions on duplicate content
    chunks = [
        {**c, "chunk_id": f"c{idx:05d}_{hashlib.md5(c['text'].encode()).hexdigest()[:6]}"}
        for idx, c in enumerate(raw_chunks)
    ]

    log.info(f"[rag] Chunked docs into {len(chunks)} chunks")
    return chunks


# ── Index management ──────────────────────────────────────────────────────────

def _docs_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _get_client_and_collection():
    """Get ChromaDB client + collection using the configured embedding function."""
    ef     = _make_embedding_function(_embedding_config())
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )
    return client, collection


def build_index(force: bool = False) -> dict:
    """
    Build or load the ChromaDB index from adk_docs.txt.

    - If index already exists for this version of adk_docs.txt: loads instantly.
    - If docs changed or force=True: re-chunks, re-embeds, re-indexes.
    - Embeddings are batched (100 at a time) to respect OpenAI rate limits.

    Returns: {"status": "built"|"loaded", "chunks": int, "collection": str}
    """
    if not DOCS_FILE.exists():
        raise FileNotFoundError(
            f"ADK docs file not found: {DOCS_FILE.resolve()}\n"
            f"Download from https://adk.dev/llms-full.txt and save as adk_docs.txt in project root."
        )

    docs_text = DOCS_FILE.read_text(encoding="utf-8", errors="replace")
    current_hash = _docs_hash(docs_text)

    # Check if index is current
    CHROMA_DIR.mkdir(exist_ok=True)
    if not force and HASH_FILE.exists():
        stored_hash = HASH_FILE.read_text().strip()
        if stored_hash == current_hash:
            _, collection = _get_client_and_collection()
            count = collection.count()
            log.info(f"[rag] Index loaded from disk — {count} chunks, no re-embedding needed")
            return {"status": "loaded", "chunks": count, "collection": COLLECTION_NAME}

    log.info("[rag] Building index — chunking and embedding ADK docs…")
    chunks = _chunk_text(docs_text)

    client, collection = _get_client_and_collection()

    # Clear existing collection — delete it, then reinitialize the client so
    # ChromaDB's internal state is fully flushed before we recreate the collection.
    if collection.count() > 0:
        log.info(f"[rag] Clearing {collection.count()} existing chunks…")
        client.delete_collection(COLLECTION_NAME)

    # Fresh client after deletion — avoids "Collection already exists" error
    # that occurs when the old client still caches the deleted collection.
    ef     = _make_embedding_function(_embedding_config())
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )

    # Batch embed + insert (100 chunks per batch to respect rate limits)
    BATCH = 100
    total = len(chunks)
    for i in range(0, total, BATCH):
        batch = chunks[i: i + BATCH]
        collection.add(
            ids=[c["chunk_id"] for c in batch],
            documents=[c["text"] for c in batch],
            metadatas=[{"section": c["section"]} for c in batch],
        )
        log.info(f"[rag] Indexed {min(i + BATCH, total)}/{total} chunks…")

    # Save hash so next startup skips re-indexing
    HASH_FILE.write_text(current_hash)
    log.info(f"[rag] Index built — {total} chunks persisted to {CHROMA_DIR}/")
    return {"status": "built", "chunks": total, "collection": COLLECTION_NAME}


# ── Retrieval ─────────────────────────────────────────────────────────────────

def retrieve_for_queries(queries: list[str]) -> str:
    """
    Retrieve ADK doc chunks for a list of search queries.

    Queries are planned by Claude in generator.py — Claude reads the ADR and
    decides what it needs to look up before writing code. This function executes
    those queries against ChromaDB using OpenAI embeddings, deduplicates,
    ranks by relevance, and returns a formatted string for the generator prompt.
    """
    _, collection = _get_client_and_collection()

    if collection.count() == 0:
        log.warning("[rag] Collection is empty — run build_index() first")
        return ""

    seen_ids: set[str] = set()
    scored_chunks: list[tuple[float, str, str]] = []  # (score, section, text)

    for query_text in queries:
        try:
            results = collection.query(
                query_texts=[query_text],
                n_results=TOP_K_PER_QUERY,
                include=["documents", "metadatas", "distances"],
            )
            docs      = results["documents"][0]
            metas     = results["metadatas"][0]
            distances = results["distances"][0]

            for doc, meta, dist in zip(docs, metas, distances):
                chunk_id = hashlib.md5(doc.encode()).hexdigest()[:8]
                if chunk_id in seen_ids:
                    continue
                seen_ids.add(chunk_id)
                similarity = 1 - (dist / 2)
                scored_chunks.append((similarity, meta.get("section", ""), doc))
        except Exception as e:
            log.warning(f"[rag] Query failed for '{query_text[:50]}': {e}")

    if not scored_chunks:
        log.warning("[rag] No chunks retrieved")
        return ""

    scored_chunks.sort(key=lambda x: x[0], reverse=True)
    top_chunks = scored_chunks[:MAX_CHUNKS_TOTAL]

    sections: list[str] = []
    total_chars = 0
    for score, section_title, text in top_chunks:
        if total_chars >= MAX_INJECT_CHARS:
            break
        block = f"### [{section_title}] (relevance: {score:.2f})\n{text}"
        if total_chars + len(block) > MAX_INJECT_CHARS:
            remaining = MAX_INJECT_CHARS - total_chars
            block = block[:remaining] + "\n… [truncated]"
        sections.append(block)
        total_chars += len(block)

    context = "\n\n---\n\n".join(sections)
    log.info(f"[rag] Retrieved {len(sections)} chunks ({len(queries)} queries), {total_chars} chars")
    return context


# ── Status ────────────────────────────────────────────────────────────────────

def index_status() -> dict:
    """Return current index status — used by Flask /rag/status route."""
    if not CHROMA_DIR.exists() or not HASH_FILE.exists():
        return {
            "indexed": False,
            "chunks": 0,
            "docs_file_exists": DOCS_FILE.exists(),
            "docs_file_size_mb": round(DOCS_FILE.stat().st_size / 1e6, 1) if DOCS_FILE.exists() else 0,
        }
    try:
        cfg = _embedding_config()
        _, collection = _get_client_and_collection()
        return {
            "indexed": True,
            "chunks": collection.count(),
            "docs_file_exists": DOCS_FILE.exists(),
            "docs_file_size_mb": round(DOCS_FILE.stat().st_size / 1e6, 1) if DOCS_FILE.exists() else 0,
            "collection": COLLECTION_NAME,
            "embed_model": cfg["model"],
            "embed_base_url": cfg["base_url"] or "https://api.openai.com/v1 (default)",
        }
    except Exception as e:
        return {"indexed": False, "error": str(e)}
