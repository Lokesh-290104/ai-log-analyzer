from datetime import UTC, datetime

import pytest

from app.parser import ParseError, parse_line, parse_logs, parse_timestamp
from app.sample import generate_sample_logs

T0 = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "line, level, service, message",
    [
        ("2026-09-28T10:00:00Z ERROR [payments] Card declined", "ERROR", "payments", "Card declined"),
        ("2026-09-28 10:00:00 [ERROR] [payments] Card declined", "ERROR", "payments", "Card declined"),
        ("2026-09-28 10:00:00 [ERROR] payments: Card declined", "ERROR", "payments", "Card declined"),
        ("2026-09-28 10:00:00 ERROR payments - Card declined", "ERROR", "payments", "Card declined"),
        ("2026-09-28T10:00:00Z payments ERROR Card declined", "ERROR", "payments", "Card declined"),
        ("[2026-09-28T10:00:00Z] [payments] [ERROR] Card declined", "ERROR", "payments", "Card declined"),
        ("2026-09-28T10:00:00Z ERROR Card declined", "ERROR", "unknown", "Card declined"),
        ("2026-09-28T10:00:00Z warning [api] slow", "WARN", "api", "slow"),
        ("2026-09-28T10:00:00Z CRITICAL [db] down", "FATAL", "db", "down"),
        ('{"timestamp": "2026-09-28T10:00:00Z", "level": "error", "service": "payments", "message": "Card declined"}',
         "ERROR", "payments", "Card declined"),
        ('{"@timestamp": "2026-09-28T10:00:00Z", "log.level": "ERROR", "service": {"name": "payments"}, "msg": "x"}',
         "ERROR", "payments", "x"),
        ('{"time": "2026-09-28T10:00:00Z", "severity": "INFO", "message": "no service"}', "INFO", "unknown", "no service"),
        ('ts=2026-09-28T10:00:00Z level=error service=payments msg="Card declined"', "ERROR", "payments", "Card declined"),
    ],
)
def test_recognized_formats(line, level, service, message):
    ts, got_level, got_service, got_message = parse_line(line)
    assert ts == T0
    assert (got_level, got_service, got_message) == (level, service, message)


@pytest.mark.parametrize(
    "line",
    [
        "hello world",
        "2026-09-28T10:00:00Z just text without a level",
        "2026-13-45T10:00:00Z ERROR [x] impossible date",
        '{"level": "error", "message": "no timestamp"}',
        '{"timestamp": "2026-09-28T10:00:00Z", "level": "loud", "message": "unknown level"}',
        "[1, 2, 3]",
        "{not json",
    ],
)
def test_unrecognized_lines(line):
    assert parse_line(line) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2026-09-28T10:00:00Z", T0),
        ("2026-09-28 10:00:00", T0),  # naive -> UTC
        ("2026-09-28T15:30:00+05:30", T0),
        ("2026-09-28T15:30:00+0530", T0),
        ("2026-09-28 10:00:00,000", T0),
        ("2026-09-28T10:00:00.000000000Z", T0),  # nanoseconds trimmed
        ("garbage", None),
    ],
)
def test_timestamps_normalize_to_utc(text, expected):
    assert parse_timestamp(text) == expected


def test_stack_trace_lines_attach_to_previous_entry():
    text = (
        "2026-09-28T10:00:00Z ERROR [api] Unhandled error\n"
        "Traceback (most recent call last):\n"
        '  File "app.py", line 1, in <module>\n'
        "ValueError: bad\n"
        "2026-09-28T10:00:01Z INFO [api] recovered\n"
    )
    result = parse_logs(text)
    assert len(result.entries) == 2
    assert result.entries[0].detail.endswith("ValueError: bad")
    assert result.continuation_lines == 3
    assert result.unparsed_lines == 0


def test_unparsed_lines_are_counted_with_samples_not_guessed():
    text = "junk one\n2026-09-28T10:00:00Z INFO [api] ok\n\njunk two\n"
    result = parse_logs(text)
    assert len(result.entries) == 1
    assert result.unparsed_lines == 2
    assert result.blank_lines == 1
    assert result.unparsed_samples == [{"line_no": 1, "text": "junk one"}, {"line_no": 4, "text": "junk two"}]


def test_continuation_before_any_entry_is_unparsed():
    result = parse_logs("  indented first line\n2026-09-28T10:00:00Z INFO [api] ok\n")
    assert result.unparsed_lines == 1


@pytest.mark.parametrize("text", ["", "   \n\n  "])
def test_empty_input_rejected(text):
    with pytest.raises(ParseError, match="empty"):
        parse_logs(text)


def test_line_limit():
    with pytest.raises(ParseError, match="limit"):
        parse_logs("2026-09-28T10:00:00Z INFO [a] x\n" * 11, max_lines=10)


def test_long_fields_are_truncated():
    entry = parse_logs(f"2026-09-28T10:00:00Z INFO [{'s' * 150}] {'m' * 5000}").entries[0]
    assert len(entry.service) <= 100 and len(entry.message) == 4000


def test_sample_log_is_deterministic_and_fully_parsed():
    text = generate_sample_logs()
    assert text == generate_sample_logs()
    result = parse_logs(text)
    assert result.unparsed_lines == 1  # the "log rotated" marker, on purpose
    assert result.continuation_lines == 4  # the traceback
    assert {e.service for e in result.entries} == {
        "api-gateway", "auth", "payments", "orders", "inventory", "notifications"
    }
    assert [e.ts for e in result.entries] == sorted(e.ts for e in result.entries)
