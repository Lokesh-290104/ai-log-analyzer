"""Deterministic analysis tools. These compute every number the user sees.

Each tool has a Pydantic args model (the contract the LLM must satisfy; unknown fields are
rejected) and a pure function over parsed entries. Results are JSON-safe dicts with
bounded list sizes, so a huge log can't blow up the model prompt.
"""

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

from app.parser import ERROR_LEVELS, LEVELS, LogEntry, normalize_level

MAX_LIST = 20


def _to_levels(value):
    if value is None:
        return None
    items = [value] if isinstance(value, str) else value
    levels = []
    for item in items:
        level = normalize_level(str(item))
        if level is None:
            raise ValueError(f"unknown level {item!r}; use one of {', '.join(LEVELS)}")
        levels.append(level)
    return levels or None


def _to_utc(value):
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _utc_after(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


Levels = Annotated[list[str] | None, BeforeValidator(_to_levels)]


class Filters(BaseModel):
    """Optional filters shared by most tools."""

    model_config = ConfigDict(extra="forbid")

    level: Levels = Field(None, description="Only these levels, e.g. [\"ERROR\", \"FATAL\"].")
    service: str | None = Field(None, description="Only this service (exact name, case-insensitive).")
    start: datetime | None = Field(None, description="Inclusive ISO-8601 start time (UTC if no offset).")
    end: datetime | None = Field(None, description="Exclusive ISO-8601 end time (UTC if no offset).")

    @model_validator(mode="after")
    def _check_window(self):
        if self.start is not None:
            self.start = _utc_after(self.start)
        if self.end is not None:
            self.end = _utc_after(self.end)
        if self.start and self.end and self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CountByArgs(Filters):
    field: Literal["level", "service"] = Field(description="Group entries by this field.")


class TopMessagesArgs(Filters):
    limit: int = Field(5, ge=1, le=MAX_LIST, description="How many message groups to return.")


class FindOccurrenceArgs(Filters):
    which: Literal["first", "last"] = Field("first", description="Earliest or latest match.")
    contains: str | None = Field(None, max_length=200, description="Case-insensitive text the message must contain.")


class WindowArgs(Filters):
    # start and end are required here, unlike the shared filters.
    start: datetime = Field(description="Inclusive ISO-8601 start time.")
    end: datetime = Field(description="Exclusive ISO-8601 end time.")
    limit: int = Field(10, ge=0, le=MAX_LIST, description="How many sample entries to include.")


class SearchArgs(Filters):
    contains: str = Field(min_length=1, max_length=200, description="Case-insensitive text to search for.")
    limit: int = Field(10, ge=1, le=MAX_LIST, description="How many matches to include.")


class ChangeEventsArgs(Filters):
    limit: int = Field(20, ge=1, le=MAX_LIST, description="How many events to return.")


class TimelineArgs(Filters):
    bucket_minutes: Literal[1, 5, 15, 60] | None = Field(
        None, description="Bucket size in minutes; omitted = chosen from the time range."
    )


# ---------------------------------------------------------------- helpers

def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S") + (
        f".{ts.microsecond // 1000:03d}Z" if ts.microsecond else "Z"
    )


def _pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def _select(entries: list[LogEntry], f: Filters, contains: str | None = None) -> list[LogEntry]:
    levels = set(f.level) if f.level else None
    service = f.service.lower() if f.service else None
    needle = contains.lower() if contains else None
    out = []
    for e in entries:
        if levels and e.level not in levels:
            continue
        if service and e.service.lower() != service:
            continue
        if f.start and e.ts < f.start:
            continue
        if f.end and e.ts >= f.end:
            continue
        if needle and needle not in e.message.lower() and needle not in e.detail.lower():
            continue
        out.append(e)
    return out


def _filters_echo(f: Filters) -> dict:
    """The filters actually applied, so the trace shows exactly what was counted."""
    applied = {}
    if f.level:
        applied["level"] = f.level
    if f.service:
        applied["service"] = f.service
    if f.start:
        applied["start"] = iso(f.start)
    if f.end:
        applied["end"] = iso(f.end)
    return applied


def entry_dict(e: LogEntry) -> dict:
    d = {"line": e.line_no, "ts": iso(e.ts), "level": e.level, "service": e.service, "message": e.message}
    if e.detail:
        d["detail"] = e.detail[:500]
    return d


_SIGNATURE_RULES = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
]


def message_signature(message: str) -> str:
    """Group messages that differ only in ids and numbers: 'Order ORD-123 shipped' -> 'Order ORD-<n> shipped'."""
    sig = message
    for pattern, token in _SIGNATURE_RULES:
        sig = pattern.sub(token, sig)
    return sig[:300]


# ---------------------------------------------------------------- tools

def summary(entries: list[LogEntry], _args: NoArgs) -> dict:
    levels = Counter(e.level for e in entries)
    services = Counter(e.service for e in entries)
    errors = sum(levels[l] for l in ERROR_LEVELS)
    return {
        "total_entries": len(entries),
        "first_ts": iso(entries[0].ts) if entries else None,
        "last_ts": iso(entries[-1].ts) if entries else None,
        "error_entries": errors,
        "error_share_pct": _pct(errors, len(entries)),
        "by_level": {l: levels[l] for l in LEVELS if levels[l]},
        "by_service": dict(services.most_common()),
        "service_count": len(services),
    }


def count_by(entries: list[LogEntry], args: CountByArgs) -> dict:
    selected = _select(entries, args)
    counts = Counter(getattr(e, args.field) for e in selected)
    rows = [{"value": v, "count": c, "share_pct": _pct(c, len(selected))} for v, c in counts.most_common(MAX_LIST)]
    return {
        "field": args.field,
        "filters": _filters_echo(args),
        "total_matching": len(selected),
        "distinct_values": len(counts),
        "rows": rows,
        "rows_returned": len(rows),
    }


def top_messages(entries: list[LogEntry], args: TopMessagesArgs) -> dict:
    # Default to errors: "top messages" almost always means "top error messages".
    f = args if args.level else args.model_copy(update={"level": list(ERROR_LEVELS)})
    selected = _select(entries, f)
    groups: dict[str, list[LogEntry]] = {}
    for e in selected:
        groups.setdefault(message_signature(e.message), []).append(e)
    ranked = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))[: args.limit]
    rows = [
        {
            "rank": i,
            "signature": sig,
            "count": len(items),
            "share_pct": _pct(len(items), len(selected)),
            "example": items[0].message,
            "services": sorted({e.service for e in items}),
            "first_ts": iso(items[0].ts),
            "last_ts": iso(items[-1].ts),
        }
        for i, (sig, items) in enumerate(ranked, start=1)
    ]
    return {
        "filters": _filters_echo(f),
        "total_matching": len(selected),
        "distinct_messages": len(groups),
        "rows": rows,
        "rows_returned": len(rows),
    }


def find_occurrence(entries: list[LogEntry], args: FindOccurrenceArgs) -> dict:
    selected = _select(entries, args, args.contains)
    hit = (selected[0] if args.which == "first" else selected[-1]) if selected else None
    return {
        "which": args.which,
        "filters": _filters_echo(args) | ({"contains": args.contains} if args.contains else {}),
        "total_matching": len(selected),
        "entry": entry_dict(hit) if hit else None,
    }


def entries_in_window(entries: list[LogEntry], args: WindowArgs) -> dict:
    selected = _select(entries, args)
    levels = Counter(e.level for e in selected)
    services = Counter(e.service for e in selected)
    samples = [entry_dict(e) for e in selected[: args.limit]]
    return {
        "filters": _filters_echo(args),
        "total_matching": len(selected),
        "by_level": {l: levels[l] for l in LEVELS if levels[l]},
        "by_service": dict(services.most_common(MAX_LIST)),
        "samples": samples,
        "samples_returned": len(samples),
    }


def search(entries: list[LogEntry], args: SearchArgs) -> dict:
    selected = _select(entries, args, args.contains)
    matches = [entry_dict(e) for e in selected[: args.limit]]
    return {
        "filters": _filters_echo(args) | {"contains": args.contains},
        "total_matching": len(selected),
        "matches": matches,
        "matches_returned": len(matches),
    }


MAX_BUCKETS = 200
_BUCKET_LADDER = (1, 5, 15, 60, 360, 1440, 10080)


def _bucket_size(span: timedelta, requested: int | None) -> int:
    """Requested size, or one giving ~60 buckets; widened so there are never more than
    MAX_BUCKETS (a log spanning years must not allocate millions of buckets)."""
    minutes = span.total_seconds() / 60
    if requested is None:
        requested = next((s for s in _BUCKET_LADDER[:4] if minutes / s <= 60), 60)
    if minutes / requested < MAX_BUCKETS:
        return requested
    for size in _BUCKET_LADDER:
        if size >= requested and minutes / size < MAX_BUCKETS:
            return size
    return 1440 * (int(minutes / 1440 / MAX_BUCKETS) + 1)  # whole days


def _align(ts: datetime, size: int) -> datetime:
    ts = ts.replace(second=0, microsecond=0)
    if size < 60:
        return ts - timedelta(minutes=ts.minute % size)
    ts = ts.replace(minute=0)
    if size < 1440:
        return ts - timedelta(hours=ts.hour % (size // 60))
    return ts.replace(hour=0)


def timeline(entries: list[LogEntry], args: TimelineArgs) -> dict:
    selected = _select(entries, args)
    if not selected:
        return {"filters": _filters_echo(args), "total_matching": 0, "bucket_minutes": args.bucket_minutes,
                "buckets": [], "peak": None}
    size = _bucket_size(selected[-1].ts - selected[0].ts, args.bucket_minutes)
    step = timedelta(minutes=size)
    origin = _align(selected[0].ts, size)
    counts: Counter[int] = Counter()
    errors: Counter[int] = Counter()
    for e in selected:
        index = int((e.ts - origin) / step)
        counts[index] += 1
        if e.level in ERROR_LEVELS:
            errors[index] += 1
    last = max(counts)
    buckets = [
        {"start": iso(origin + i * step), "count": counts[i], "errors": errors[i]}
        for i in range(last + 1)
    ]
    peak = max(buckets, key=lambda b: b["count"])  # earliest bucket wins a tie
    return {
        "filters": _filters_echo(args),
        "total_matching": len(selected),
        "bucket_minutes": size,
        "buckets": buckets,
        "peak": peak,
    }


# Words that mark a change to the system or its recovery. Matched case-insensitively in messages.
_CHANGE_WORDS = re.compile(
    r"deploy|roll(?:ed|ing)?[ -]?back|release|version|restart|reboot|config|migrat|upgrade|downgrade|"
    r"scal(?:e|ed|ing)|feature flag|failover|recover|resolved|restored",
    re.I,
)


def change_events(entries: list[LogEntry], args: ChangeEventsArgs) -> dict:
    """Deploys, rollbacks, restarts, config changes and recoveries, each labeled relative to
    the errors (before / during / after) so the model never has to order timestamps itself."""
    # The level filter is ignored on purpose: deploys and rollbacks are INFO/WARN lines, so
    # "level: ERROR" (a natural model choice) would hide exactly the events being looked for.
    args = args.model_copy(update={"level": None})
    selected = _select(entries, args)
    errors = [e for e in selected if e.level in ERROR_LEVELS]
    first_err = errors[0].ts if errors else None
    last_err = errors[-1].ts if errors else None
    events = []
    for e in selected:
        if not _CHANGE_WORDS.search(e.message):
            continue
        if first_err is None:
            when = "no_errors"
        elif e.ts < first_err:
            when = "before_first_error"
        elif e.ts > last_err:
            when = "after_last_error"
        else:
            when = "during_errors"
        events.append(entry_dict(e) | {"relative_to_errors": when})
    shown = events[: args.limit]
    return {
        "filters": _filters_echo(args),
        "first_error_ts": iso(first_err) if first_err else None,
        "last_error_ts": iso(last_err) if last_err else None,
        "total_matching": len(events),
        "events": shown,
        "events_returned": len(shown),
    }


# ---------------------------------------------------------------- registry

@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[[list[LogEntry], BaseModel], dict]

    def run(self, entries: list[LogEntry], raw_args: dict) -> tuple[BaseModel, dict]:
        """Validate args (raises ValidationError) and compute the result."""
        args = self.args_model.model_validate(raw_args or {})
        return args, self.fn(entries, args)


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool("summary", "Overall totals: entry count, time range, counts per level and per service.", NoArgs, summary),
        Tool("count_by", "Count entries grouped by level or service, with share %, after optional filters.",
             CountByArgs, count_by),
        Tool("top_messages", "Most frequent messages (ids/numbers normalized), with counts, share %, first/last "
             "time. Defaults to ERROR+FATAL when no level filter is given.", TopMessagesArgs, top_messages),
        Tool("find_occurrence", "First or last entry matching filters and optional text, plus how many match.",
             FindOccurrenceArgs, find_occurrence),
        Tool("entries_in_window", "Entries between start and end: counts by level/service and sample lines.",
             WindowArgs, entries_in_window),
        Tool("search", "Case-insensitive text search in messages and stack traces; count plus matching lines.",
             SearchArgs, search),
        Tool("timeline", "Entry and error counts per time bucket, and the peak bucket.", TimelineArgs, timeline),
        Tool("change_events", "Deploys, rollbacks, restarts, config/version changes and recoveries, each labeled "
             "before_first_error / during_errors / after_last_error. Use for root-cause and recovery questions. "
             "Ignores the level filter (changes are usually INFO/WARN lines).",
             ChangeEventsArgs, change_events),
    ]
}

__all__ = ["TOOLS", "Tool", "ValidationError", "iso", "message_signature", "entry_dict"]
