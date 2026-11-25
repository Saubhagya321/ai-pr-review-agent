# backend/memory/embedder.py
#
# Local embedding client — the bridge between raw text and vector space.
#
# MODEL CHOICE: jinaai/jina-embeddings-v2-base-code (default, settings.embedding_model)
#   Runs on CPU through fastembed (ONNX runtime). No API key, no per-call cost,
#   no rate limits. Trained on source code in ~30 languages, reads up to 8k
#   tokens, and outputs 768-dimensional vectors — the same size as the
#   VECTOR(768) column in code_chunks, so no schema change is needed.
#   The model files (~0.6 GB) are downloaded once on first use and cached in
#   settings.embedding_cache_dir.
#
# CHUNKING STRATEGY: chunk by FILE, not by token count
#   For code: a FILE is the natural structural unit.
#   If a file is very large (>8000 chars), we truncate from the end
#   to preserve semantically dense headers/signatures.
#
# INPUT LENGTH GUARD: 8000 chars
#   Keeps CPU time per file bounded and matches what ingestion stores.
#
# ASYNC SAFETY:
#   Inference is CPU-bound and synchronous, so it runs in a worker thread via
#   asyncio.to_thread(). The event loop (webhooks, other agents) keeps running.
#
# GRACEFUL DEGRADATION:
#   Any failure (model download, load, inference) raises EmbeddingError so
#   context_retriever can fall back without crashing the review workflow.

import asyncio
import logging
import threading
from typing import Any

from backend.config.settings import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum input length for embedding.
MAX_EMBED_CHARS = 8_000

# Embedding vector dimensionality. Must match VECTOR(N) in the database
# migration and the configured embedding model's output size.
EMBEDDING_DIMENSIONS = 768


class EmbeddingError(Exception):
    """
    Raised when the embedding model cannot produce vectors.

    Callers (specifically context_retriever.py) catch this and return ""
    (graceful degradation: no RAG context, review still runs).
    """
    pass


# ---------------------------------------------------------------------------
# Model loading (once per process)
# ---------------------------------------------------------------------------

_model: Any = None
_model_name: str | None = None
_model_lock = threading.Lock()


def _get_model() -> Any:
    """
    Returns the loaded fastembed model, loading it on first call.

    Loading is guarded by a lock so concurrent first calls do not download
    or load the model twice. If settings.embedding_model changes (tests,
    config reload), the new model is loaded.
    """
    global _model, _model_name
    cfg = get_settings()
    name = cfg.embedding_model

    with _model_lock:
        if _model is not None and _model_name == name:
            return _model

        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise EmbeddingError(
                "fastembed is not installed. Run: pip install fastembed"
            ) from e

        kwargs: dict[str, Any] = {"model_name": name}
        if cfg.embedding_cache_dir:
            kwargs["cache_dir"] = cfg.embedding_cache_dir

        logger.info("embedder | loading model=%s (first use downloads it)", name)
        try:
            _model = TextEmbedding(**kwargs)
        except Exception as e:
            raise EmbeddingError(
                f"Could not load embedding model '{name}': {type(e).__name__}: {e}"
            ) from e
        _model_name = name
        return _model


def _embed_sync(texts: list[str]) -> list[list[float]]:
    """Runs the model over `texts` and checks the vector size. Blocking."""
    model = _get_model()
    try:
        vectors = [vec.tolist() for vec in model.embed(texts)]
    except Exception as e:
        raise EmbeddingError(
            f"Local embedding failed: {type(e).__name__}: {e}"
        ) from e

    if len(vectors) != len(texts):
        raise EmbeddingError(
            f"Embedding count mismatch: expected {len(texts)}, got {len(vectors)}"
        )
    for vec in vectors:
        if len(vec) != EMBEDDING_DIMENSIONS:
            raise EmbeddingError(
                f"Embedding model '{_model_name}' returned {len(vec)} dims, but the "
                f"database expects {EMBEDDING_DIMENSIONS}. Pick a "
                f"{EMBEDDING_DIMENSIONS}-dim model in EMBEDDING_MODEL, or change "
                f"VECTOR({EMBEDDING_DIMENSIONS}) and EMBEDDING_DIMENSIONS together."
            )
    return vectors


# ---------------------------------------------------------------------------
# Core embedding functions
# ---------------------------------------------------------------------------

async def embed_text(text: str) -> list[float]:
    """
    Embeds a single text string into a 768-dimensional vector.

    Args:
        text: The text to embed. Will be truncated if over 8000 chars.

    Returns:
        List of 768 floats (the embedding vector).

    Raises:
        EmbeddingError: on any model failure. Caller handles gracefully.
    """
    if not text or not text.strip():
        logger.debug("embed_text | empty_text | returning zero vector")
        return [0.0] * EMBEDDING_DIMENSIONS

    original_len = len(text)
    if original_len > MAX_EMBED_CHARS:
        text = text[:MAX_EMBED_CHARS]
        logger.debug(
            "embed_text | truncated | original_chars=%d truncated_to=%d",
            original_len, MAX_EMBED_CHARS,
        )

    vectors = await asyncio.to_thread(_embed_sync, [text])

    logger.debug(
        "embed_text | success | model=%s dims=%d input_chars=%d",
        _model_name, len(vectors[0]), len(text),
    )
    return vectors[0]


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """
    Embeds a list of texts in one model pass.

    Args:
        texts: List of strings to embed. Empty list returns [].

    Returns:
        List of 768-dimensional vectors, one per input text.
        Order is preserved: result[i] corresponds to texts[i].
        Empty or whitespace-only inputs get a zero vector.

    Raises:
        EmbeddingError: on any model failure.
    """
    if not texts:
        return []

    non_empty: list[tuple[int, str]] = []
    for i, text in enumerate(texts):
        if text and text.strip():
            non_empty.append((i, text[:MAX_EMBED_CHARS]))

    results: list[list[float]] = [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]
    if not non_empty:
        return results

    vectors = await asyncio.to_thread(_embed_sync, [t for _, t in non_empty])
    for (position, _), vec in zip(non_empty, vectors):
        results[position] = vec

    logger.debug(
        "embed_batch | success | model=%s batch_size=%d",
        _model_name, len(non_empty),
    )
    return results
