"""Application settings, loaded from environment variables / the repo-root .env file."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent
load_dotenv(REPO_ROOT / ".env")


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _int(name: str, default: int, minimum: int) -> int:
    return max(minimum, int(os.getenv(name, str(default))))


def normalize_database_url(url: str) -> str:
    """Accept the URLs hosts hand out (postgres://, postgresql://) and use the async drivers.

    asyncpg does not understand libpq's `sslmode`, so `sslmode=require` becomes `ssl=require`.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("sslmode=", "ssl=")
    if url.startswith("sqlite:///"):
        url = "sqlite+aiosqlite:///" + url[len("sqlite:///"):]
    return url


def _default_database_url() -> str:
    if os.getenv("DATABASE_URL"):
        return normalize_database_url(os.environ["DATABASE_URL"])
    # Absolute path, so starting the app from another folder doesn't create a second database.
    return f"sqlite+aiosqlite:///{(BACKEND_ROOT / 'local.db').as_posix()}"


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=_default_database_url)

    # Which LLM service answers questions: "gemini" or "openrouter".
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "gemini").strip().lower())
    # Keys are stripped: a newline pasted into a hosting dashboard would otherwise be rejected as invalid.
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", "").strip())
    # Gemma on the Gemini API answered reliably while the Flash models were overloaded.
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemma-4-26b-a4b-it"))
    # Tried in order when the main model is overloaded or unavailable.
    gemini_fallback_models: list[str] = field(
        default_factory=lambda: _csv(os.getenv("GEMINI_FALLBACK_MODELS", "gemini-flash-latest"))
    )
    openrouter_api_key: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", "").strip())
    openrouter_models: list[str] = field(
        default_factory=lambda: _csv(os.getenv(
            "OPENROUTER_MODELS", "google/gemma-4-31b-it:free,google/gemma-4-26b-a4b-it:free"
        ))
    )
    # Per HTTP call to the LLM, and total for one question (all agent steps together).
    llm_timeout_ms: int = field(default_factory=lambda: _int("LLM_TIMEOUT_MS", 30_000, 1000))
    agent_deadline_ms: int = field(default_factory=lambda: _int("AGENT_DEADLINE_MS", 90_000, 1000))
    # Agent loop bounds: model turns per question, and how many invalid replies it may fix.
    max_agent_steps: int = field(default_factory=lambda: _int("MAX_AGENT_STEPS", 6, 1))
    max_repairs: int = field(default_factory=lambda: _int("MAX_REPAIRS", 2, 0))

    # Upload limits: protect the free-tier instance's memory and the database.
    max_upload_bytes: int = field(default_factory=lambda: _int("MAX_UPLOAD_BYTES", 5_000_000, 1000))
    max_upload_lines: int = field(default_factory=lambda: _int("MAX_UPLOAD_LINES", 100_000, 10))
    # Parsed entries kept in memory (across uploads) so questions don't reload every row.
    entry_cache_entries: int = field(default_factory=lambda: _int("ENTRY_CACHE_ENTRIES", 200_000, 0))

    # Limits on /ask (spends LLM quota) and on uploads (spends storage). 0 disables a limit.
    ask_limit_per_minute: int = field(default_factory=lambda: _int("ASK_LIMIT_PER_MINUTE", 6, 0))
    ask_limit_per_day: int = field(default_factory=lambda: _int("ASK_LIMIT_PER_DAY", 300, 0))
    upload_limit_per_minute: int = field(default_factory=lambda: _int("UPLOAD_LIMIT_PER_MINUTE", 10, 0))
    upload_limit_per_day: int = field(default_factory=lambda: _int("UPLOAD_LIMIT_PER_DAY", 500, 0))
    # Behind a reverse proxy (Render), the client address is the last X-Forwarded-For entry,
    # the one the proxy appended. Only enable behind a proxy, or callers could pick their identity.
    trust_proxy: bool = field(
        default_factory=lambda: os.getenv("TRUST_PROXY", "false").strip().lower() in ("1", "true", "yes")
    )
    # Built React app, served at "/" when present.
    static_dir: Path = field(default_factory=lambda: Path(os.getenv("STATIC_DIR", str(BACKEND_ROOT / "static"))))
