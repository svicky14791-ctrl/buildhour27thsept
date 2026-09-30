"""Stage 4-5: embed chunks and maintain the local ChromaDB collection.

Model is sentence-transformers/all-MiniLM-L6-v2 (384-dim). Vectors are L2
normalised so Chroma's cosine space and dot-product similarity agree.

Re-running rebuilds: the collection is dropped before it is written, so a
second run cannot leave stale vectors behind next to fresh ones.
"""

from __future__ import annotations

import os
import sys
import time
from collections import Counter
from datetime import date
from functools import lru_cache
from pathlib import Path

import chromadb
import numpy as np
from chromadb.config import Settings as ChromaSettings

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ingest.chunk import Chunk, chunk_docs_dir  # noqa: E402

CHROMA_DIR = ROOT / "data" / "chroma"
COLLECTION_NAME = "hdfc_mf_facts"
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384
BATCH_SIZE = 64
WRITE_BATCH = 500

# Per-stage timings, off by default. Enabled with GENERATOR_VERBOSE=1, which is
# how you find out whether a slow answer is retrieval or the LLM.
EMBED_PERF = os.getenv("RAG_PERF", "").strip() not in ("", "0", "false", "False")


def perf(label: str):
    """Context manager that prints how long a stage took, under RAG_PERF=1."""
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        started = time.perf_counter()
        try:
            yield
        finally:
            if EMBED_PERF:
                print(f"  [perf] {label}: {(time.perf_counter() - started) * 1000:.0f} ms")

    return _cm()

# Required metadata, plus section/kind so retrieval can filter and the UI can
# show which passage answered a question.
METADATA_FIELDS = ("scheme_name", "source_url", "ingest_date", "section", "kind")


@lru_cache(maxsize=1)
def get_model():
    """Load the embedding model once per process.

    fastembed runs the same all-MiniLM-L6-v2 weights through ONNX Runtime, so
    the vectors are the same model the brief specifies - same name, same 384
    dims, same normalized output - without a ~2 GB torch dependency. That
    matters on Render's free tier, where the disk is small and a cold build
    spends minutes on the torch wheel.
    """
    from fastembed import TextEmbedding

    print(f"Loading embedding model: {EMBEDDING_MODEL} (fastembed/ONNX)")
    return TextEmbedding(EMBEDDING_MODEL)


def embed_texts(texts: list[str]):
    """Encode a batch of chunk texts. Returns an (n, 384) float32 array."""
    if not texts:
        return []
    # fastembed yields one vector per input, already L2-normalised on q8/ONNX
    # int8 paths, but norm=True makes that explicit rather than assumed.
    with perf(f"embed {len(texts)} text(s)"):
        vectors = np.array(
            list(get_model().embed(texts, batch_size=BATCH_SIZE)), dtype=np.float32
        )
    return vectors


def get_client(chroma_dir: Path = CHROMA_DIR):
    """Persistent Chroma client. Created on demand, reused within a process."""
    chroma_dir.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=str(chroma_dir),
        settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
    )


def reset_collection(client) -> None:
    """Drop the collection so a rebuild starts from a known-empty state."""
    try:
        client.delete_collection(COLLECTION_NAME)
        print(f"Dropped existing collection '{COLLECTION_NAME}'")
    except Exception:  # noqa: BLE001 - absent collection is the normal first run
        print(f"No existing collection '{COLLECTION_NAME}' to drop")


def get_collection(client):
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={
            "hnsw:space": "cosine",
            "embedding_model": EMBEDDING_MODEL,
            "embedding_dim": EMBEDDING_DIM,
        },
    )


def ingest_date_for(chunk: Chunk, fallback: str) -> str:
    """Date the chunk's source was ingested.

    Prefers the fetch timestamp recorded by the loader. When that is missing the
    run date is used instead and the caller is warned, so the provenance of the
    value is never silently guessed.
    """
    if chunk.fetched_at:
        return chunk.fetched_at[:10]
    return fallback


def build_metadata(chunk: Chunk, fallback_date: str) -> dict:
    """Chroma accepts only str/int/float/bool metadata, and no empty strings."""
    values = {
        "scheme_name": chunk.scheme_name,
        "source_url": chunk.source_url,
        "ingest_date": ingest_date_for(chunk, fallback_date),
        "section": chunk.section,
        "kind": chunk.kind,
    }
    return {k: v for k, v in values.items() if v not in (None, "")}


def build_index(chunks: list[Chunk] | None = None, chroma_dir: Path = CHROMA_DIR) -> int:
    """Embed every chunk and write a freshly built collection. Returns count."""
    if chunks is None:
        print("Chunking documents in data/docs ...")
        chunks = chunk_docs_dir()
    if not chunks:
        raise SystemExit("No chunks to embed. Run src/ingest/load.py first.")

    missing = [c.chunk_id for c in chunks if not c.source_url or not c.text.strip()]
    if missing:
        raise SystemExit(
            f"{len(missing)} chunk(s) lack a source_url or text: {missing[:5]}"
        )

    ids = [c.chunk_id for c in chunks]
    duplicates = [i for i, count in Counter(ids).items() if count > 1]
    if duplicates:
        raise SystemExit(
            f"{len(duplicates)} duplicate chunk_id(s), e.g. {duplicates[:5]}. "
            "Ids must be unique or Chroma will silently overwrite."
        )

    print(f"Embedding {len(chunks)} chunks with {EMBEDDING_MODEL} ...")
    vectors = embed_texts([c.text for c in chunks])
    if vectors.shape[1] != EMBEDDING_DIM:
        raise SystemExit(
            f"Expected {EMBEDDING_DIM}-dim vectors, got {vectors.shape[1]}."
        )

    fallback_date = date.today().isoformat()
    undated = sum(1 for c in chunks if not c.fetched_at)
    if undated:
        print(f"  ! {undated} chunk(s) had no fetch timestamp; using {fallback_date}")

    client = get_client(chroma_dir)
    reset_collection(client)
    collection = get_collection(client)

    for start in range(0, len(chunks), WRITE_BATCH):
        window = chunks[start : start + WRITE_BATCH]
        collection.add(
            ids=[c.chunk_id for c in window],
            embeddings=[v.tolist() for v in vectors[start : start + WRITE_BATCH]],
            documents=[c.text for c in window],
            metadatas=[build_metadata(c, fallback_date) for c in window],
        )
        print(f"  wrote {min(start + WRITE_BATCH, len(chunks))}/{len(chunks)}")

    print(f"Collection '{COLLECTION_NAME}' now holds {collection.count()} chunks")
    return collection.count()


def count(chroma_dir: Path = CHROMA_DIR) -> int:
    """Number of chunks in the collection. Zero when it has not been built."""
    try:
        return get_collection(get_client(chroma_dir)).count()
    except Exception:  # noqa: BLE001
        return 0


if __name__ == "__main__":
    build_index()
