import pytest
from pydantic import ValidationError

from app.parser import parse_logs
from app.sample import generate_sample_logs
from app.tools import TOOLS, message_signature


def run(entries, tool, **args):
    return TOOLS[tool].run(entries, args)[1]


def test_summary(small_entries):
    r = run(small_entries, "summary")
    assert r["total_entries"] == 6
    assert r["error_entries"] == 3
    assert r["error_share_pct"] == 50.0
    assert r["by_level"] == {"INFO": 2, "WARN": 1, "ERROR": 3}
    assert r["by_service"] == {"payments": 2, "api": 2, "orders": 2}
    assert (r["first_ts"], r["last_ts"]) == ("2026-09-28T10:00:00Z", "2026-09-28T10:05:00Z")


def test_summary_of_nothing():
    r = TOOLS["summary"].fn([], TOOLS["summary"].args_model())
    assert r["total_entries"] == 0 and r["first_ts"] is None and r["error_share_pct"] == 0.0


def test_count_by_service_for_errors(small_entries):
    r = run(small_entries, "count_by", field="service", level="error")
    assert r["total_matching"] == 3
    assert r["rows"] == [
        {"value": "payments", "count": 2, "share_pct": 66.7},
        {"value": "orders", "count": 1, "share_pct": 33.3},
    ]
    assert r["filters"] == {"level": ["ERROR"]}


def test_count_by_with_time_window(small_entries):
    r = run(small_entries, "count_by", field="level", start="2026-09-28T10:01:00", end="2026-09-28T10:03:00Z")
    # start inclusive, end exclusive; naive start is UTC
    assert r["total_matching"] == 2
    assert r["rows"] == [{"value": "ERROR", "count": 2, "share_pct": 100.0}]


def test_service_filter_is_case_insensitive(small_entries):
    assert run(small_entries, "count_by", field="level", service="PAYMENTS")["total_matching"] == 2


def test_top_messages_defaults_to_errors_and_groups_numbers(small_entries):
    r = run(small_entries, "top_messages")
    assert r["filters"] == {"level": ["ERROR", "FATAL"]}
    top = r["rows"][0]
    assert top["count"] == 2 and top["signature"] == "Database timeout after <n>ms"
    assert top["first_ts"] == "2026-09-28T10:01:00Z" and top["last_ts"] == "2026-09-28T10:02:00Z"
    assert r["distinct_messages"] == 2


def test_top_messages_limit(small_entries):
    r = run(small_entries, "top_messages", level=["INFO", "WARN", "ERROR"], limit=1)
    assert r["rows_returned"] == 1 and r["distinct_messages"] == 5


def test_find_occurrence_first_and_last(small_entries):
    first = run(small_entries, "find_occurrence", contains="timeout")
    last = run(small_entries, "find_occurrence", contains="TIMEOUT", which="last")
    assert first["entry"]["line"] == 2 and last["entry"]["line"] == 3
    assert first["total_matching"] == 2


def test_find_occurrence_no_match(small_entries):
    r = run(small_entries, "find_occurrence", contains="nope")
    assert r["entry"] is None and r["total_matching"] == 0


def test_entries_in_window(small_entries):
    r = run(small_entries, "entries_in_window", start="2026-09-28T10:01:00Z", end="2026-09-28T10:05:00Z", limit=2)
    assert r["total_matching"] == 4
    assert r["by_level"] == {"WARN": 1, "ERROR": 3}
    assert r["samples_returned"] == 2


def test_search_includes_stack_traces():
    entries = parse_logs(
        "2026-09-28T10:00:00Z ERROR [api] crash\n  File x\nKeyError: user_id\n"
        "2026-09-28T10:00:01Z INFO [api] user_id looked up\n"
    ).entries
    r = run(entries, "search", contains="keyerror")
    assert r["total_matching"] == 1 and r["matches"][0]["detail"].endswith("KeyError: user_id")


def test_timeline_buckets_and_peak(small_entries):
    r = run(small_entries, "timeline", bucket_minutes=5)
    assert r["buckets"] == [
        {"start": "2026-09-28T10:00:00Z", "count": 5, "errors": 3},
        {"start": "2026-09-28T10:05:00Z", "count": 1, "errors": 0},
    ]
    assert r["peak"]["start"] == "2026-09-28T10:00:00Z"


def test_timeline_auto_bucket_and_empty(small_entries):
    assert run(small_entries, "timeline")["bucket_minutes"] == 1
    empty = run(small_entries, "timeline", service="nobody")
    assert empty["buckets"] == [] and empty["peak"] is None


@pytest.mark.parametrize(
    "tool, args",
    [
        ("count_by", {"field": "host"}),
        ("count_by", {"field": "level", "level": "LOUD"}),
        ("count_by", {"field": "level", "bogus": 1}),  # unknown args are rejected, not ignored
        ("top_messages", {"limit": 0}),
        ("top_messages", {"limit": 500}),
        ("entries_in_window", {"start": "2026-09-28T10:00:00Z"}),
        ("entries_in_window", {"start": "2026-09-28T11:00:00Z", "end": "2026-09-28T10:00:00Z"}),
        ("search", {"contains": ""}),
        ("timeline", {"bucket_minutes": 7}),
        ("summary", {"anything": True}),
        ("find_occurrence", {"which": "middle"}),
        ("count_by", {"field": "level", "start": "yesterday"}),
    ],
)
def test_invalid_args_rejected(small_entries, tool, args):
    with pytest.raises(ValidationError):
        TOOLS[tool].run(small_entries, args)


def test_message_signature():
    assert message_signature("Order ORD-123 shipped in 4.5s") == "Order ORD-<n> shipped in <n>s"
    assert message_signature("conn to 10.0.0.1:5432 id 3f2a9c1e-1b2c-4d5e-8f90-1234567890ab") == \
        "conn to <ip> id <uuid>"


def test_sample_incident_numbers():
    """The demo story the README tells must be what the tools actually compute."""
    entries = parse_logs(generate_sample_logs()).entries
    top = run(entries, "top_messages", limit=1)["rows"][0]
    assert top["services"] == ["payments"] and top["signature"].startswith("Database timeout")
    assert top["first_ts"].startswith("2026-09-28T10:42")
    by_service = run(entries, "count_by", field="service", level=["ERROR", "FATAL"])["rows"]
    assert by_service[0]["value"] == "payments"
    peak = run(entries, "timeline", level=["ERROR", "FATAL"], bucket_minutes=5)["peak"]
    assert peak["start"].startswith("2026-09-28T10:4") or peak["start"].startswith("2026-09-28T10:5")
