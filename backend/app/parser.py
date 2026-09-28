"""Turn raw log text into structured entries. Deterministic: no model is involved.

Recognized line shapes (timestamp first, ISO-8601-ish, optional brackets):
    {"timestamp": ..., "level": ..., "service": ..., "message": ...}     JSON lines
    ts=... level=error service=api msg="..."                             logfmt
    2026-09-28T10:00:00Z ERROR [payments] Card declined                  level [service]
    2026-09-28 10:00:00,123 [ERROR] payments: Card declined              level service:
    2026-09-28T10:00:00Z payments ERROR Card declined                    service level
    2026-09-28T10:00:00Z ERROR Card declined                             level only

Indented lines and stack-trace lines attach to the entry above them as `detail`.
Anything else is counted as unparsed and reported, never guessed.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

LEVELS = ("DEBUG", "INFO", "WARN", "ERROR", "FATAL")
ERROR_LEVELS = ("ERROR", "FATAL")
_LEVEL_ALIASES = {
    "TRACE": "DEBUG", "DEBUG": "DEBUG", "DBG": "DEBUG",
    "INFO": "INFO", "NOTICE": "INFO", "INF": "INFO",
    "WARN": "WARN", "WARNING": "WARN", "WRN": "WARN",
    "ERROR": "ERROR", "ERR": "ERROR",
    "FATAL": "FATAL", "CRITICAL": "FATAL", "CRIT": "FATAL", "PANIC": "FATAL", "EMERG": "FATAL",
}
UNKNOWN_SERVICE = "unknown"
MAX_SERVICE_LEN = 100
MAX_MESSAGE_LEN = 4000
MAX_DETAIL_LEN = 8000
UNPARSED_SAMPLES = 5

_LEVEL = r"(?P<level>" + "|".join(sorted(_LEVEL_ALIASES, key=len, reverse=True)) + r")"
_TS = r"(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?(?:\s?Z|\s?[+-]\d{2}:?\d{2})?)"
_SERVICE = r"(?P<service>[A-Za-z0-9_.\-/]{1,100})"
_HEAD = re.compile(r"^\[?" + _TS + r"\]?\s*(?:[-|]\s*)?(?P<rest>.*)$")
_REST_PATTERNS = [
    # ERROR [payments] message    /    [ERROR] [payments] message
    re.compile(r"^\[?" + _LEVEL + r"\]?\s*[-|]?\s*\[" + _SERVICE + r"\]\s*[:\-|]?\s*(?P<msg>.*)$", re.I),
    # ERROR payments: message    /    [ERROR] payments - message
    re.compile(r"^\[?" + _LEVEL + r"\]?\s+" + _SERVICE + r"\s*(?::|\s-|\s\|)\s*(?P<msg>.*)$", re.I),
    # payments ERROR message    /    [payments] [ERROR] message
    re.compile(r"^\[?" + _SERVICE + r"\]?\s+\[?" + _LEVEL + r"\]?\s*[:\-|]?\s+(?P<msg>.*)$", re.I),
    # ERROR message
    re.compile(r"^\[?" + _LEVEL + r"\]?\s*[:\-|]?\s+(?P<msg>.*)$", re.I),
]
_LOGFMT_PAIR = re.compile(r'(\w[\w.\-]*)=("(?:[^"\\]|\\.)*"|\S+)')
_CONTINUATION = re.compile(
    r"^(\s|Traceback \(|Caused by|at\s|\.\.\.\s*\d*\s*more|[\w.$]+(Error|Exception|Warning)(:|$))"
)

_JSON_KEYS = {
    "ts": ("timestamp", "ts", "time", "@timestamp", "datetime", "date"),
    "level": ("level", "severity", "lvl", "log.level", "levelname", "loglevel"),
    "service": ("service", "service.name", "app", "application", "component", "logger", "source", "name"),
    "msg": ("message", "msg", "event", "text", "log"),
}


class ParseError(ValueError):
    """The input can't be analyzed at all (empty, too large)."""


@dataclass(frozen=True, slots=True)
class LogEntry:
    line_no: int
    ts: datetime  # always timezone-aware UTC
    level: str  # one of LEVELS
    service: str
    message: str
    detail: str = ""


@dataclass
class ParseResult:
    entries: list[LogEntry]
    total_lines: int
    blank_lines: int
    continuation_lines: int
    unparsed_lines: int
    unparsed_samples: list[dict] = field(default_factory=list)

    def stats(self) -> dict:
        return {
            "total_lines": self.total_lines,
            "parsed_entries": len(self.entries),
            "blank_lines": self.blank_lines,
            "continuation_lines": self.continuation_lines,
            "unparsed_lines": self.unparsed_lines,
            "unparsed_samples": self.unparsed_samples,
        }


def normalize_level(value: str) -> str | None:
    return _LEVEL_ALIASES.get(value.strip().upper())


def parse_timestamp(value: str) -> datetime | None:
    """ISO-8601-ish text -> aware UTC datetime. Naive timestamps are taken as UTC."""
    text = value.strip().replace(",", ".").replace(" Z", "Z")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # "+0000" -> "+00:00"; also allow a space before the offset.
    text = re.sub(r"\s?([+-]\d{2}):?(\d{2})$", r"\1:\2", text)
    # Python accepts at most 6 fractional digits.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _clean_service(value: str | None) -> str:
    service = (value or "").strip().strip("[]:")
    return service[:MAX_SERVICE_LEN] if service else UNKNOWN_SERVICE


def _first_key(data: dict, keys: tuple[str, ...]):
    for key in keys:
        # Only plain values: {"service": {"name": "api"}} is handled by the dotted key below.
        if key in data and data[key] not in (None, "") and not isinstance(data[key], (dict, list)):
            return data[key]
        # Nested keys such as {"service": {"name": "api"}} written as "service.name".
        if "." in key:
            head, _, tail = key.partition(".")
            inner = data.get(head)
            if isinstance(inner, dict) and inner.get(tail) not in (None, ""):
                return inner[tail]
    return None


def _from_mapping(data: dict) -> tuple[datetime, str, str, str] | None:
    raw_ts = _first_key(data, _JSON_KEYS["ts"])
    raw_level = _first_key(data, _JSON_KEYS["level"])
    raw_msg = _first_key(data, _JSON_KEYS["msg"])
    if raw_ts is None or raw_level is None or raw_msg is None:
        return None
    ts = parse_timestamp(str(raw_ts))
    level = normalize_level(str(raw_level))
    if ts is None or level is None:
        return None
    service = _first_key(data, _JSON_KEYS["service"])
    return ts, level, _clean_service(str(service) if service is not None else None), str(raw_msg)


def _parse_json(line: str):
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, RecursionError):
        return None
    return _from_mapping(data) if isinstance(data, dict) else None


def _parse_logfmt(line: str):
    pairs = {}
    for key, value in _LOGFMT_PAIR.findall(line):
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        pairs[key.lower()] = value
    if len(pairs) < 3:
        return None
    return _from_mapping(pairs)


def _parse_text(line: str):
    head = _HEAD.match(line)
    if not head:
        return None
    ts = parse_timestamp(head.group("ts"))
    if ts is None:
        return None
    rest = head.group("rest").strip()
    for pattern in _REST_PATTERNS:
        match = pattern.match(rest)
        if not match:
            continue
        level = normalize_level(match.group("level"))
        if level is None:
            continue
        service = match.groupdict().get("service")
        # "ERROR message" would otherwise read the level word itself as a service name.
        if service and normalize_level(service):
            continue
        return ts, level, _clean_service(service), match.group("msg").strip()
    return None


def parse_line(line: str):
    """One line -> (ts, level, service, message), or None if it isn't a log entry header."""
    stripped = line.strip()
    if stripped.startswith("{"):
        return _parse_json(stripped)
    if "=" in stripped and re.search(r"\blevel=|\blvl=|\bseverity=", stripped, re.I):
        parsed = _parse_logfmt(stripped)
        if parsed:
            return parsed
    return _parse_text(stripped)


def parse_logs(text: str, max_lines: int = 100_000) -> ParseResult:
    if not text or not text.strip():
        raise ParseError("The log is empty.")
    lines = text.splitlines()
    if len(lines) > max_lines:
        raise ParseError(f"The log has {len(lines):,} lines; the limit is {max_lines:,}.")

    # Entries are built as lists first because continuation lines may extend the last one.
    pending: list[list] = []
    blank = continuation = unparsed = 0
    samples: list[dict] = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            blank += 1
            continue
        parsed = parse_line(line)
        if parsed:
            ts, level, service, message = parsed
            pending.append([line_no, ts, level, service, message[:MAX_MESSAGE_LEN], []])
            continue
        if pending and _CONTINUATION.match(line):
            pending[-1][5].append(line.rstrip())
            continuation += 1
            continue
        unparsed += 1
        if len(samples) < UNPARSED_SAMPLES:
            samples.append({"line_no": line_no, "text": line[:200]})

    entries = [
        LogEntry(line_no, ts, level, service, message, "\n".join(detail)[:MAX_DETAIL_LEN])
        for line_no, ts, level, service, message, detail in pending
    ]
    return ParseResult(entries, len(lines), blank, continuation, unparsed, samples)
