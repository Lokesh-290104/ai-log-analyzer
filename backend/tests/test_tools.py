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


OUT_OF_ORDER = (
    "2026-09-28T10:05:00Z ERROR [a] late line first in the file\n"
    "2026-09-28T10:00:00Z ERROR [a] earliest\n"
    "2026-09-28T10:02:00Z INFO [a] middle\n"
)


def test_out_of_order_logs_are_analyzed_in_time_order():
    entries = parse_logs(OUT_OF_ORDER).entries
    assert [e.line_no for e in entries] == [2, 3, 1]
    s = run(entries, "summary")
    assert (s["first_ts"], s["last_ts"]) == ("2026-09-28T10:00:00Z", "2026-09-28T10:05:00Z")
    assert run(entries, "find_occurrence", level="ERROR")["entry"]["line"] == 2
    t = run(entries, "timeline", bucket_minutes=1)
    assert sum(b["count"] for b in t["buckets"]) == 3  # nothing dropped


def test_timeline_widens_buckets_for_huge_spans():
    entries = parse_logs(
        "0001-01-01T00:00:00Z ERROR [a] ancient\n9999-12-31T23:59:00Z ERROR [a] far future\n"
    ).entries
    t = run(entries, "timeline", bucket_minutes=1)
    assert len(t["buckets"]) <= 200 and t["bucket_minutes"] > 10080
    assert sum(b["count"] for b in t["buckets"]) == 2


def test_timeline_widens_a_requested_size_that_would_be_too_fine():
    entries = parse_logs("2026-09-01T00:00:00Z INFO [a] x\n2026-09-28T00:00:00Z INFO [a] y\n").entries
    t = run(entries, "timeline", bucket_minutes=1)
    assert t["bucket_minutes"] == 360 and len(t["buckets"]) <= 200
    assert t["buckets"][0]["start"] == "2026-09-01T00:00:00Z"


INCIDENT = """2026-09-29 14:00:00 INFO [deploy] Starting deployment version=2.4.0
2026-09-29 14:02:00 WARN [database] Connection pool utilization=85%
2026-09-29 14:04:00 ERROR [database] Connection pool exhausted
2026-09-29 14:04:05 ERROR [api] POST /api/orders status=500
2026-09-29 14:04:10 INFO [kubelet] Restarting container
2026-09-29 14:04:30 CRITICAL [monitor] Error rate exceeded threshold
2026-09-29 14:05:00 WARN [deploy] Initiating rollback to version=2.3.9
2026-09-29 14:08:00 INFO [monitor] Service recovered
"""


def test_change_events_are_labeled_relative_to_the_errors():
    r = run(parse_logs(INCIDENT).entries, "change_events")
    assert (r["first_error_ts"], r["last_error_ts"]) == ("2026-09-29T14:04:00Z", "2026-09-29T14:04:30Z")
    labels = [(e["message"][:22], e["relative_to_errors"]) for e in r["events"]]
    assert labels == [
        ("Starting deployment ve", "before_first_error"),
        ("Restarting container", "during_errors"),
        ("Initiating rollback to", "after_last_error"),
        ("Service recovered", "after_last_error"),
    ]
    assert r["total_matching"] == 4  # the pool warning and plain errors are not change events


def test_change_events_without_errors(small_entries):
    entries = parse_logs("2026-09-29T10:00:00Z INFO [deploy] Deployed v2\n").entries
    r = run(entries, "change_events")
    assert r["first_error_ts"] is None and r["events"][0]["relative_to_errors"] == "no_errors"
    assert run(small_entries, "change_events")["total_matching"] == 0
