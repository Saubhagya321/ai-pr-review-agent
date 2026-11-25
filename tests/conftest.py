# tests/conftest.py
#
# Keeps the test suite offline and fast: the local embedding model (~0.6 GB)
# is replaced by a tiny deterministic fake, so no test downloads or loads it.
# Tests that mock embed_text / embed_batch themselves are unaffected.
#
# To run against the real model instead:  RUN_REAL_EMBEDDINGS=1 pytest

import hashlib
import os

import pytest


class FakeEmbeddingModel:
    """Stands in for fastembed.TextEmbedding. Same text -> same unit vector."""

    def __init__(self, dims: int = 768):
        self.dims = dims

    def embed(self, texts):
        import numpy as np

        for text in texts:
            seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big")
            vec = np.random.default_rng(seed).standard_normal(self.dims)
            yield vec / np.linalg.norm(vec)


@pytest.fixture(autouse=True)
def _fake_local_embeddings(monkeypatch):
    if os.environ.get("RUN_REAL_EMBEDDINGS") == "1":
        yield
        return
    from backend.memory import embedder

    monkeypatch.setattr(embedder, "_get_model", lambda: FakeEmbeddingModel())
    yield
