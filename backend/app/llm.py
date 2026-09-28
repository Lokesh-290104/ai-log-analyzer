"""LLM clients. The agent depends only on the LLMClient protocol, so providers are swappable
and tests use FakeLLM (scripted replies, no network).

Async all the way: a sync SDK call inside FastAPI would block every other request.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from app.config import Settings

logger = logging.getLogger("log_analyzer")


class LLMError(RuntimeError):
    """The model could not be reached. Maps to HTTP 503: nothing is answered."""


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str


class LLMClient(Protocol):
    @property
    def model_name(self) -> str: ...

    async def generate(self, system: str, messages: list[ChatMessage], timeout_s: float) -> str: ...


class ProviderError(Exception):
    """An HTTP-level failure from an LLM provider (status code in .code)."""

    def __init__(self, code: int, message: str = ""):
        super().__init__(f"HTTP {code} {message}".strip())
        self.code = code


class FailoverClient:
    """Shared retry/failover policy for every provider.

    - 500/502/503/504 (overloaded): retry the same model once with backoff, then move on.
    - 429 (quota), 402 (no credits), 404 (model gone), network errors/timeouts: next model.
    - The first model that answers becomes the preferred one for later calls.
    - `timeout_s` bounds the whole generate() call, across retries and fallbacks.
    Subclasses implement _call() for one request to one model.
    """

    provider = "LLM"
    RETRY_SAME_MODEL_CODES = {500, 502, 503, 504}
    RETRIES_PER_MODEL = 1
    # Free tiers limit tokens per minute; waiting briefly usually clears a 429.
    QUOTA_WAIT_S = 12

    def __init__(self, models: list[str], call_timeout_ms: int):
        if not models:
            raise LLMError(f"No {self.provider} models configured.")
        self.models = list(dict.fromkeys(models))  # de-duplicate, keep order
        self._preferred = self.models[0]
        self._call_timeout_s = call_timeout_ms / 1000

    @property
    def model_name(self) -> str:
        return self._preferred

    async def _call(self, model: str, system: str, messages: list[ChatMessage], timeout_s: float) -> str:
        raise NotImplementedError

    async def generate(
        self, system: str, messages: list[ChatMessage], timeout_s: float, quota_wait: bool = True
    ) -> str:
        """Try every model; when all of them hit a per-minute quota (429) and time remains,
        wait for the quota window to reset and try once more."""
        deadline = time.monotonic() + timeout_s
        failures: list[str] = []
        text = await self._try_models(system, messages, deadline, failures)
        if text is None and quota_wait and self._all_quota(failures):
            if deadline - time.monotonic() > self.QUOTA_WAIT_S + 10:
                await asyncio.sleep(self.QUOTA_WAIT_S)
                retry_failures: list[str] = []
                text = await self._try_models(system, messages, deadline, retry_failures)
                failures += retry_failures
        if text is None:
            timed_out = deadline - time.monotonic() < 1
            raise LLMError(self._failure_message(failures, timed_out=timed_out))
        return text

    @staticmethod
    def _all_quota(failures: list[str]) -> bool:
        return bool(failures) and all(f.endswith(": 429") for f in failures)

    async def _try_models(
        self, system: str, messages: list[ChatMessage], deadline: float, failures: list[str]
    ) -> str | None:
        ordered = [self._preferred] + [m for m in self.models if m != self._preferred]
        for model in ordered:
            for attempt in range(self.RETRIES_PER_MODEL + 1):
                remaining = deadline - time.monotonic()
                if remaining < 1:
                    return None
                budget = min(self._call_timeout_s, remaining)
                try:
                    text = await asyncio.wait_for(self._call(model, system, messages, budget), budget)
                except ProviderError as e:
                    failures.append(f"{model}: {e.code}")
                    if e.code in self.RETRY_SAME_MODEL_CODES and attempt < self.RETRIES_PER_MODEL:
                        await asyncio.sleep(min(2**attempt, max(0.0, deadline - time.monotonic() - 1)))
                        continue
                    break
                except Exception as e:  # timeouts and network errors: try the next model
                    failures.append(f"{model}: {type(e).__name__}")
                    break
                self._preferred = model
                return text
        return None

    def _failure_message(self, failures: list[str], timed_out: bool = False) -> str:
        codes = {f.rsplit(": ", 1)[-1] for f in failures}
        if "402" in codes:
            reason = f"the {self.provider} account is out of credits"
        elif "429" in codes:
            reason = f"the {self.provider} usage limit has been reached for now"
        elif "401" in codes or "403" in codes:
            reason = f"the {self.provider} API key was rejected"
        elif timed_out:
            reason = f"{self.provider} took too long to respond"
        else:
            reason = f"{self.provider} is unavailable right now"
        logger.warning("%s failed (%s): %s", self.provider, reason, ", ".join(failures))
        return f"The AI service couldn't answer because {reason}. Please try again later."


class GeminiClient(FailoverClient):
    provider = "Gemini"

    def __init__(self, settings: Settings):
        from google import genai

        if not settings.gemini_api_key:
            raise LLMError("GEMINI_API_KEY is not set. Add it to your .env file.")
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._types = genai.types
        self._api_error = genai.errors.APIError
        super().__init__([settings.gemini_model, *settings.gemini_fallback_models], settings.llm_timeout_ms)

    async def _call(self, model: str, system: str, messages: list[ChatMessage], timeout_s: float) -> str:
        t = self._types
        contents = [
            t.Content(role="model" if m.role == "assistant" else "user", parts=[t.Part.from_text(text=m.content)])
            for m in messages
        ]
        config = t.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            response_mime_type="application/json",
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
            http_options=t.HttpOptions(timeout=int(timeout_s * 1000)),
        )
        try:
            response = await self._client.aio.models.generate_content(model=model, contents=contents, config=config)
        except self._api_error as e:
            raise ProviderError(e.code) from e
        return response.text or ""


class OpenRouterClient(FailoverClient):
    """OpenRouter's OpenAI-compatible chat API: one key, many models."""

    provider = "OpenRouter"
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        if not settings.openrouter_api_key:
            raise LLMError("OPENROUTER_API_KEY is not set. Add it to your .env file.")
        self._http = http or httpx.AsyncClient()
        self._headers = {"Authorization": f"Bearer {settings.openrouter_api_key}", "X-Title": "AI Log Analyzer"}
        super().__init__(settings.openrouter_models, settings.llm_timeout_ms)

    async def _call(self, model: str, system: str, messages: list[ChatMessage], timeout_s: float) -> str:
        response = await self._http.post(
            self.URL,
            headers=self._headers,
            timeout=timeout_s,
            json={
                "model": model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}]
                + [{"role": m.role, "content": m.content} for m in messages],
            },
        )
        if response.status_code != 200:
            raise ProviderError(response.status_code, response.text[:200])
        data = response.json()
        if "error" in data:  # some upstream failures arrive as 200 with an error body
            raise ProviderError(int(data["error"].get("code") or 502), str(data["error"].get("message", ""))[:200])
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ProviderError(502, "unexpected response shape") from None


class ChainClient:
    """Several providers in order (LLM_PROVIDER=gemini,openrouter): when one is out of quota
    or down, the next answers. Only the last provider waits out a per-minute quota."""

    def __init__(self, clients: list[FailoverClient]):
        self.clients = clients
        self._active = clients[0]

    @property
    def model_name(self) -> str:
        return self._active.model_name

    async def generate(self, system: str, messages: list[ChatMessage], timeout_s: float) -> str:
        deadline = time.monotonic() + timeout_s
        errors = []
        for i, client in enumerate(self.clients):
            remaining = deadline - time.monotonic()
            if remaining < 1:
                break
            try:
                text = await client.generate(system, messages, remaining, quota_wait=i == len(self.clients) - 1)
            except LLMError as e:
                errors.append(str(e))
                continue
            self._active = client
            return text
        raise LLMError(errors[-1] if errors else "The AI service took too long to respond. Please try again later.")


_PROVIDERS = {"gemini": GeminiClient, "openrouter": OpenRouterClient}


def create_llm_client(settings: Settings) -> LLMClient:
    """The provider(s) named by LLM_PROVIDER, comma-separated in fallback order.

    Raises LLMError when none is configured. With several providers, ones missing a key are
    skipped so a partial setup still works."""
    names = [n.strip() for n in settings.llm_provider.split(",") if n.strip()]
    unknown = [n for n in names if n not in _PROVIDERS]
    if unknown or not names:
        raise LLMError(f"Unknown LLM_PROVIDER '{settings.llm_provider}'. Use 'gemini', 'openrouter' or both.")
    clients, errors = [], []
    for name in names:
        try:
            clients.append(_PROVIDERS[name](settings))
        except LLMError as e:
            errors.append(str(e))
    if not clients:
        raise LLMError(" ".join(errors))
    return clients[0] if len(clients) == 1 else ChainClient(clients)


class FakeLLM:
    """Scripted model for tests: returns the queued replies in order and records every call.

    A queued Exception is raised instead of returned, to simulate provider failures.
    """

    def __init__(self, replies: list[str | Exception], model_name: str = "fake-llm"):
        self.replies = list(replies)
        self.calls: list[tuple[str, list[ChatMessage]]] = []
        self._model_name = model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    async def generate(self, system: str, messages: list[ChatMessage], timeout_s: float) -> str:
        self.calls.append((system, list(messages)))
        if not self.replies:
            raise AssertionError("FakeLLM ran out of scripted replies")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
