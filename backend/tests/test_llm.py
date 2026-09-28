import asyncio
from dataclasses import replace

import httpx
import pytest

from app.config import Settings, normalize_database_url
from app.llm import ChatMessage, FailoverClient, LLMError, OpenRouterClient, ProviderError, create_llm_client
from app.ratelimit import RateLimiter

MSG = [ChatMessage("user", "hi")]


class Scripted(FailoverClient):
    provider = "Test"

    def __init__(self, models, script):
        super().__init__(models, call_timeout_ms=5000)
        self.script = script  # model -> list of results (str or Exception)
        self.calls = []

    async def _call(self, model, system, messages, timeout_s):
        self.calls.append(model)
        outcome = self.script[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def generate(client, timeout_s=30.0):
    return asyncio.run(client.generate("sys", MSG, timeout_s))


def test_first_model_answers():
    client = Scripted(["a", "b"], {"a": ["ok"]})
    assert generate(client) == "ok" and client.calls == ["a"]


def test_overloaded_model_retried_once_then_next(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr("app.llm.asyncio.sleep", lambda s: real_sleep(0))
    client = Scripted(["a", "b"], {"a": [ProviderError(503), ProviderError(503)], "b": ["from b"]})
    assert generate(client) == "from b"
    assert client.calls == ["a", "a", "b"]
    assert client.model_name == "b"  # preferred for the next call


def test_quota_error_skips_to_next_model_without_retry():
    client = Scripted(["a", "b"], {"a": [ProviderError(429)], "b": ["ok"]})
    assert generate(client) == "ok" and client.calls == ["a", "b"]


def test_network_error_skips_to_next_model():
    client = Scripted(["a", "b"], {"a": [httpx.ConnectError("down")], "b": ["ok"]})
    assert generate(client) == "ok"


@pytest.mark.parametrize(
    "code, phrase",
    [(429, "usage limit"), (402, "out of credits"), (401, "key was rejected"), (404, "unavailable")],
)
def test_all_models_fail_with_clear_message(code, phrase):
    client = Scripted(["a"], {"a": [ProviderError(code)]})
    with pytest.raises(LLMError, match=phrase):
        asyncio.run(client.generate("sys", MSG, 30, quota_wait=False))


def test_deadline_exhausted():
    client = Scripted(["a"], {"a": ["never called"]})
    with pytest.raises(LLMError, match="too long"):
        generate(client, timeout_s=0.5)
    assert client.calls == []


def test_openrouter_request_shape_and_errors():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = request.read()
        if b"bad-model" in seen["body"]:
            return httpx.Response(200, json={"error": {"code": 429, "message": "rate limited"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"type":"final_answer"}'}}]})

    settings = replace(Settings(), openrouter_api_key="k", openrouter_models=["bad-model", "good-model"])
    client = OpenRouterClient(settings, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert generate(client) == '{"type":"final_answer"}'
    assert b'"role":"system"' in seen["body"].replace(b" ", b"")


def test_create_llm_client_errors():
    with pytest.raises(LLMError, match="Unknown LLM_PROVIDER"):
        create_llm_client(replace(Settings(), llm_provider="nope"))
    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        create_llm_client(replace(Settings(), llm_provider="openrouter", openrouter_api_key=""))


@pytest.mark.parametrize(
    "url, expected",
    [
        ("postgres://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("postgresql://u:p@h/db?sslmode=require", "postgresql+asyncpg://u:p@h/db?ssl=require"),
        ("sqlite:///x.db", "sqlite+aiosqlite:///x.db"),
        ("sqlite+aiosqlite:///x.db", "sqlite+aiosqlite:///x.db"),
    ],
)
def test_normalize_database_url(url, expected):
    assert normalize_database_url(url) == expected


def test_rate_limiter_minute_and_day_windows():
    now = [0.0]
    limiter = RateLimiter(per_minute=2, per_day=3, noun="question", clock=lambda: now[0])
    assert limiter.check("a") is None and limiter.check("a") is None
    assert "per minute" in limiter.check("a")
    now[0] = 61
    assert limiter.check("a") is None
    assert "limit for today" in limiter.check("b")
    now[0] = 86_400 + 1
    assert limiter.check("b") is None


def test_quota_on_every_model_waits_then_retries(monkeypatch):
    real_sleep = asyncio.sleep
    waits = []
    monkeypatch.setattr("app.llm.asyncio.sleep", lambda s: waits.append(s) or real_sleep(0))
    client = Scripted(["a", "b"], {"a": [ProviderError(429), "after wait"], "b": [ProviderError(429)]})
    assert generate(client, timeout_s=60) == "after wait"
    assert waits == [Scripted.QUOTA_WAIT_S] and client.calls == ["a", "b", "a"]


def test_quota_wait_skipped_when_disabled_or_no_time(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr("app.llm.asyncio.sleep", lambda s: real_sleep(0))
    client = Scripted(["a"], {"a": [ProviderError(429)]})
    with pytest.raises(LLMError, match="usage limit"):
        asyncio.run(client.generate("sys", MSG, 60, quota_wait=False))
    client = Scripted(["a"], {"a": [ProviderError(429)]})
    with pytest.raises(LLMError):
        generate(client, timeout_s=15)  # not enough time left to wait
    assert client.calls == ["a"]


def test_chain_falls_back_to_next_provider():
    from app.llm import ChainClient

    first = Scripted(["g"], {"g": [ProviderError(429)]})
    second = Scripted(["o"], {"o": ["from openrouter"]})
    chain = ChainClient([first, second])
    assert generate(chain) == "from openrouter"
    assert chain.model_name == "o"


def test_chain_raises_last_error_when_all_fail():
    from app.llm import ChainClient

    chain = ChainClient([Scripted(["g"], {"g": [ProviderError(401)]}), Scripted(["o"], {"o": [ProviderError(402)]})])
    with pytest.raises(LLMError, match="out of credits"):
        generate(chain)


def test_create_chain_skips_unconfigured_providers():
    from app.llm import ChainClient, OpenRouterClient

    settings = replace(Settings(), llm_provider="gemini, openrouter", gemini_api_key="", openrouter_api_key="k")
    assert isinstance(create_llm_client(settings), OpenRouterClient)
    both = replace(settings, gemini_api_key="g")
    assert isinstance(create_llm_client(both), ChainClient)
    with pytest.raises(LLMError, match="Unknown"):
        create_llm_client(replace(settings, llm_provider="gemini,claude"))
