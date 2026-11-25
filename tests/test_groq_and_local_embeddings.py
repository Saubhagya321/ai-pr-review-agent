# tests/test_groq_and_local_embeddings.py
#
# Covers the Groq fallback provider and the local (fastembed) embedder.
# All HTTP is served by httpx.MockTransport — nothing leaves the machine.
#
# RUN:
#   python -m pytest tests/test_groq_and_local_embeddings.py -v

import importlib
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.config import get_settings
from backend.core.exceptions import AgentError
from backend.memory import embedder
from backend.tools.llm_client import LLMClient, MistralRateLimitError

# backend.tools re-exports the `llm_client` singleton, so import the module by path.
llm_mod = importlib.import_module("backend.tools.llm_client")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"


def _chat_ok(content: dict, prompt_tokens: int = 120, completion_tokens: int = 30):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": json.dumps(content)}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    )


@pytest.fixture
def http(monkeypatch):
    """
    Routes every httpx.AsyncClient created inside llm_client through a
    handler list. Each test appends (url, response) pairs in call order.
    """
    calls: list[httpx.Request] = []
    queue: list[tuple[str, httpx.Response]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert queue, f"unexpected request to {request.url}"
        url, resp = queue.pop(0)
        assert str(request.url) == url, f"expected {url}, got {request.url}"
        return resp

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    # Patch only the names llm_client sees, not the global httpx/asyncio modules.
    monkeypatch.setattr(llm_mod, "httpx", SimpleNamespace(
        AsyncClient=client_factory, HTTPError=httpx.HTTPError, Response=httpx.Response,
    ))
    monkeypatch.setattr(llm_mod, "asyncio", SimpleNamespace(sleep=fake_sleep))
    monkeypatch.setattr(llm_mod, "_persist_call_log", AsyncMock())
    return {"calls": calls, "queue": queue, "sleeps": sleeps}


@pytest.fixture
def keys(monkeypatch):
    def set_keys(mistral: str = "", groq: str = ""):
        monkeypatch.setenv("MISTRAL_API_KEY", mistral)
        monkeypatch.setenv("GROQ_API_KEY", groq)
        get_settings.cache_clear()

    yield set_keys
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Groq provider
# ---------------------------------------------------------------------------

async def test_call_groq_sends_openai_style_request(http, keys):
    keys(groq="gsk_test")
    http["queue"].append((GROQ_URL, _chat_ok({"findings": []})))

    resp = await LLMClient().call_groq(
        messages=[{"role": "user", "content": "review this diff"}],
        system_prompt="Respond only with JSON.",
        max_tokens=512,
    )

    req = http["calls"][0]
    body = json.loads(req.content)
    assert req.headers["authorization"] == "Bearer gsk_test"
    assert body["model"] == "llama-3.3-70b-versatile"
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_completion_tokens"] == 512
    assert body["messages"][0] == {"role": "system", "content": "Respond only with JSON."}
    assert resp.content == {"findings": []}
    assert resp.model_used == "llama-3.3-70b-versatile"
    assert (resp.input_tokens, resp.output_tokens) == (120, 30)
    assert resp.estimated_cost_usd > 0  # list price, for the cost dashboard


async def test_call_groq_waits_for_retry_after_on_429(http, keys):
    keys(groq="gsk_test")
    http["queue"].append((GROQ_URL, httpx.Response(429, headers={"retry-after": "7"}, text="slow down")))
    http["queue"].append((GROQ_URL, _chat_ok({"findings": [1]})))

    resp = await LLMClient().call_groq(messages=[{"role": "user", "content": "x"}])

    assert resp.content == {"findings": [1]}
    assert http["sleeps"] == [7.0]
    assert len(http["calls"]) == 2


async def test_call_groq_does_not_retry_request_too_large(http, keys):
    keys(groq="gsk_test")
    http["queue"].append((GROQ_URL, httpx.Response(413, text="Request too large for TPM")))

    with pytest.raises(AgentError, match="tokens-per-minute"):
        await LLMClient().call_groq(messages=[{"role": "user", "content": "x"}])
    assert len(http["calls"]) == 1


async def test_call_groq_gives_up_after_repeated_5xx(http, keys):
    keys(groq="gsk_test")
    for _ in range(LLMClient.MAX_RETRIES + 1):
        http["queue"].append((GROQ_URL, httpx.Response(503, text="busy")))

    with pytest.raises(AgentError, match="failed after"):
        await LLMClient().call_groq(messages=[{"role": "user", "content": "x"}])
    assert len(http["calls"]) == LLMClient.MAX_RETRIES + 1


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------

async def test_mistral_rate_limit_falls_back_to_groq(http, keys):
    keys(mistral="m_key", groq="gsk_test")
    http["queue"].append((MISTRAL_URL, httpx.Response(429, text="rate limited")))
    http["queue"].append((GROQ_URL, _chat_ok({"findings": ["from groq"]})))

    resp = await LLMClient().call_with_fallback(messages=[{"role": "user", "content": "x"}])

    assert resp.content == {"findings": ["from groq"]}
    assert resp.model_used == "llama-3.3-70b-versatile"


async def test_groq_only_when_no_mistral_key(http, keys):
    keys(mistral="", groq="gsk_test")
    http["queue"].append((GROQ_URL, _chat_ok({"findings": []})))

    resp = await LLMClient().call_with_fallback(messages=[{"role": "user", "content": "x"}])

    assert len(http["calls"]) == 1
    assert resp.model_used == "llama-3.3-70b-versatile"


async def test_fallback_model_comes_from_settings(http, keys, monkeypatch):
    monkeypatch.setenv("FALLBACK_LLM_MODEL", "llama-3.1-8b-instant")
    keys(mistral="", groq="gsk_test")
    http["queue"].append((GROQ_URL, _chat_ok({"findings": []})))

    resp = await LLMClient().call_with_fallback(messages=[{"role": "user", "content": "x"}])

    assert json.loads(http["calls"][0].content)["model"] == "llama-3.1-8b-instant"
    assert resp.model_used == "llama-3.1-8b-instant"


async def test_mistral_rate_limit_without_groq_key_raises(http, keys):
    keys(mistral="m_key", groq="")
    http["queue"].append((MISTRAL_URL, httpx.Response(429, text="rate limited")))

    with pytest.raises(MistralRateLimitError):
        await LLMClient().call_with_fallback(messages=[{"role": "user", "content": "x"}])


# ---------------------------------------------------------------------------
# Local embeddings (fake model from conftest unless RUN_REAL_EMBEDDINGS=1)
# ---------------------------------------------------------------------------

async def test_embed_text_returns_768_dims():
    vec = await embedder.embed_text("def add(a, b):\n    return a + b")
    assert len(vec) == embedder.EMBEDDING_DIMENSIONS == 768
    assert all(isinstance(x, float) for x in vec)


async def test_embed_text_empty_input_is_zero_vector():
    assert await embedder.embed_text("   ") == [0.0] * 768


async def test_embed_batch_keeps_order_and_zero_fills_empties():
    texts = ["alpha", "", "beta"]
    batch = await embedder.embed_batch(texts)
    assert len(batch) == 3
    assert batch[1] == [0.0] * 768
    assert batch[0] == await embedder.embed_text("alpha")
    assert batch[2] == await embedder.embed_text("beta")
    assert batch[0] != batch[2]


async def test_wrong_dimension_model_raises_embedding_error(monkeypatch):
    from tests.conftest import FakeEmbeddingModel

    monkeypatch.setattr(embedder, "_get_model", lambda: FakeEmbeddingModel(dims=384))
    with pytest.raises(embedder.EmbeddingError, match="expects 768"):
        await embedder.embed_text("some code")


@pytest.mark.skipif(
    os.environ.get("RUN_REAL_EMBEDDINGS") != "1",
    reason="downloads the real model (~0.6 GB); set RUN_REAL_EMBEDDINGS=1",
)
async def test_real_model_ranks_relevant_code_first():
    import numpy as np

    docs = [
        'cur.execute("SELECT * FROM users WHERE name = %s" % user)',
        "def add(a, b):\n    return a + b",
    ]
    query = await embedder.embed_text("SQL injection via string formatting")
    vecs = await embedder.embed_batch(docs)
    sims = [float(np.dot(query, v)) for v in vecs]
    assert sims[0] > sims[1]
