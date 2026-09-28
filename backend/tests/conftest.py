import json
import os
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm import FakeLLM
from app.main import create_app
from app.parser import parse_logs

SMALL_LOG = """\
2026-09-28T10:00:00Z INFO [api] GET /health 200
2026-09-28T10:01:00Z ERROR [payments] Database timeout after 5000ms
2026-09-28T10:02:00Z ERROR [payments] Database timeout after 5000ms
2026-09-28T10:03:00Z WARN [api] Slow request took 2100ms
2026-09-28T10:04:00Z ERROR [orders] Upstream payments returned 503
2026-09-28T10:05:00Z INFO [orders] Order ORD-1 created
"""


def tool_call(tool: str, args: dict | None = None, reason: str = "") -> str:
    return json.dumps({"type": "tool_call", "tool": tool, "args": args or {}, "reason": reason})


def final(answer: str, evidence: list[int]) -> str:
    return json.dumps({"type": "final_answer", "answer": answer, "evidence": evidence})


@pytest.fixture
def small_entries():
    return parse_logs(SMALL_LOG).entries


@pytest.fixture
def settings(tmp_path):
    # CI sets TEST_DATABASE_URL to run the same API tests against real Postgres.
    url = os.getenv("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    return replace(
        Settings(),
        database_url=url,
        static_dir=tmp_path / "no-static",
        ask_limit_per_minute=100,
        ask_limit_per_day=1000,
        upload_limit_per_minute=100,
        upload_limit_per_day=1000,
        max_upload_bytes=200_000,
        max_upload_lines=5_000,
    )


@pytest.fixture
def make_client(settings):
    """make_client(replies=[...], **setting_overrides) -> (TestClient, FakeLLM)."""
    clients = []

    def _make(replies=None, **overrides):
        llm = FakeLLM(replies or [])
        client = TestClient(create_app(replace(settings, **overrides), llm=llm))
        client.__enter__()
        clients.append(client)
        return client, llm

    yield _make
    for client in clients:
        client.__exit__(None, None, None)
