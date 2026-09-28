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
        generate(client)


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
