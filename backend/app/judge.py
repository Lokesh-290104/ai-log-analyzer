"""Code as judge: verify a final answer against the tool results it cites.

The model is a witness. It may only repeat numbers and times that deterministic tools
produced (or that the user wrote in the question). Anything else means the model computed
or invented it, and the answer is rejected.

Times are checked as whole units: "10:42" must match a timestamp in the evidence. They are
not split into 10 and 42, which would let any small number pass as "part of a timestamp".

Known limits: numbers spelled as words ("three") are not checked, and a number that appears
in log text (e.g. "retry 3 of 5") counts as evidence when that entry is cited.
"""

import json
import math
import re
from dataclasses import dataclass, field

# A number not glued to a preceding digit or dot: "1,052", "74.5", "5000" in "5000ms".
_NUMBER = re.compile(r"(?<![\d.])\d[\d,]*(?:\.\d+)?")
# Timestamps in tool results: 2026-09-28T10:42:04.910Z
_EVIDENCE_TS = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?Z?")
# Dates/times as a model might write them: 2026-09-28, 2026-09-28 10:42, 2026-09-28T10:42:04Z, 10:42:04, 9:05
_ANSWER_DATETIME = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})(?:[T ](?P<dt>\d{1,2}:\d{2}(?::\d{2})?)(?:\.\d+)?)?Z?"
    r"|(?<![\d:.])(?P<time>\d{1,2}:\d{2}(?::\d{2})?)(?:\.\d+)?(?![\d:])"
)


def extract_numbers(text: str) -> list[str]:
    """Numeric tokens as written, with thousands separators and trailing commas removed."""
    out = []
    for token in _NUMBER.findall(text):
        cleaned = token.rstrip(",").replace(",", "")
        if cleaned:
            out.append(cleaned)
    return out


def _norm_time(value: str) -> str:
    hours, _, rest = value.partition(":")
    return f"{int(hours):02d}:{rest}"


def extract_datetimes(text: str) -> tuple[list[str], str]:
    """Date/time tokens (normalized) and the text with them removed."""
    tokens = []

    def take(match: re.Match) -> str:
        if match.group("date"):
            tokens.append(match.group("date") + (f"T{_norm_time(match.group('dt'))}" if match.group("dt") else ""))
        else:
            tokens.append(_norm_time(match.group("time")))
        return " "

    return tokens, _ANSWER_DATETIME.sub(take, text)


@dataclass
class _Evidence:
    numbers: set[float] = field(default_factory=set)
    times: set[str] = field(default_factory=set)

    def add_text(self, text: str) -> None:
        def take(match: re.Match) -> str:
            date, hh, mm, ss = match.groups()
            self.times.update({date, f"{hh}:{mm}", f"{hh}:{mm}:{ss}", f"{date}T{hh}:{mm}", f"{date}T{hh}:{mm}:{ss}"})
            return " "

        rest = _EVIDENCE_TS.sub(take, text)
        # The question and log text may hold times in other shapes ("between 10:00 and 11:00").
        times, rest = extract_datetimes(rest)
        self.times.update(times)
        self.numbers.update(float(n) for n in extract_numbers(rest))

    def add(self, value) -> None:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            self.numbers.add(float(value))
        elif isinstance(value, str):
            self.add_text(value)
        elif isinstance(value, dict):
            for k, v in value.items():
                self.add(k)
                self.add(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                self.add(v)


def _number_supported(token: str, allowed: set[float]) -> bool:
    value = float(token)
    if value in allowed:
        return True
    # Rounding a tool's fractional value to the precision written is fine (74.53 -> "74.5" or
    # "75"); integer counts must match exactly, so "about 140" for 143 is rejected.
    decimals = len(token.partition(".")[2])
    step = 10.0**-decimals
    # is_integer() rather than int(y): a 400-digit number in a log line parses to inf.
    return any(not y.is_integer() and abs(value - y) < step for y in allowed if math.isfinite(y))


@dataclass
class Verdict:
    ok: bool
    checked_numbers: list[str] = field(default_factory=list)
    unsupported_numbers: list[str] = field(default_factory=list)
    unknown_evidence: list[int] = field(default_factory=list)

    def problem(self) -> str:
        parts = []
        if self.unknown_evidence:
            parts.append(f"evidence ids {self.unknown_evidence} do not match any tool call you made")
        if self.unsupported_numbers:
            parts.append(
                "these numbers/times are not in the tool results you cited: "
                + ", ".join(self.unsupported_numbers)
                + ". Do not compute or estimate; call a tool that returns the figure, or leave it out"
            )
        return "; ".join(parts)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checked_numbers": self.checked_numbers,
            "unsupported_numbers": self.unsupported_numbers,
            "unknown_evidence": self.unknown_evidence,
        }


def judge_answer(answer: str, evidence: list[int], results_by_id: dict[int, dict], question: str) -> Verdict:
    unknown = sorted({i for i in evidence if i not in results_by_id})
    allowed = _Evidence()
    for call_id in dict.fromkeys(evidence):
        if call_id in results_by_id:
            allowed.add(results_by_id[call_id])
    allowed.add_text(question)

    times, rest = extract_datetimes(answer)
    numbers = extract_numbers(rest)
    checked = list(dict.fromkeys(times + numbers))
    unsupported = [t for t in dict.fromkeys(times) if t not in allowed.times]
    unsupported += [n for n in dict.fromkeys(numbers) if not _number_supported(n, allowed.numbers)]
    return Verdict(ok=not unknown and not unsupported, checked_numbers=checked,
                   unsupported_numbers=unsupported, unknown_evidence=unknown)


def result_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
