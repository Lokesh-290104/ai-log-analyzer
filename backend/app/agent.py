"""The agent loop: the model picks tools, code runs them and judges the final answer.

    question ─► LLM ─► JSON ─► AgentStep (Pydantic, discriminated on "type")
                 ▲       │ invalid ─► repair turn (budget: max_repairs) ─► exhausted ─► FAIL
                 │       ├─ tool_call ─► known tool? args valid? ─► no ─► repair turn
                 │       │      └─► run tool (pure Python) ─► TOOL_RESULT id=N ──┐
                 │       └─ final_answer ─► judge (evidence ids + numbers)       │
                 │                 ├─ rejected ─► repair turn                    │
                 │                 └─ verified ─► ANSWERED                       │
                 └───────────────────────────────────────────────────────────────┘
    Bounds: max_agent_steps model turns and one overall deadline; exceeding either fails closed.
"""

import json
import re
import time
from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.judge import judge_answer, result_json
from app.llm import ChatMessage, LLMClient, LLMError
from app.parser import LogEntry
from app.prompts import build_system_prompt, question_message, repair_message, tool_result_message
from app.tools import TOOLS

MAX_RESULT_CHARS = 12_000
MAX_RAW_IN_TRACE = 600


class ToolCallStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["tool_call"]
    tool: str = Field(min_length=1, max_length=50)
    args: dict = Field(default_factory=dict)
    reason: str = Field("", max_length=300)


class FinalAnswerStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["final_answer"]
    answer: str = Field(min_length=1, max_length=3000)
    evidence: list[int] = Field(default_factory=list, max_length=20)


AgentStep = Annotated[ToolCallStep | FinalAnswerStep, Field(discriminator="type")]
_STEP = TypeAdapter(AgentStep)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class StepParseError(ValueError):
    pass


def parse_step(raw: str) -> ToolCallStep | FinalAnswerStep:
    """Model text -> validated step. Tolerates code fences and prose around one JSON object."""
    text = _FENCE.sub("", (raw or "").strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise StepParseError("the reply was not JSON") from None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            raise StepParseError("the reply was not valid JSON") from None
    if not isinstance(data, dict):
        raise StepParseError("the reply must be one JSON object, not a list or a value")
    try:
        return _STEP.validate_python(data)
    except ValidationError as e:
        raise StepParseError(f"the JSON did not match the schema: {_short_errors(e)}") from None


def _short_errors(e: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or 'value'}: {err['msg']}" for err in e.errors()[:4]
    )


@dataclass
class AgentOutcome:
    status: Literal["answered", "failed"]
    answer: str | None
    error_code: str | None
    error: str | None
    trace: list[dict]
    verification: dict | None
    model: str
    llm_calls: int
    duration_ms: int = 0


@dataclass
class _Run:
    question: str
    messages: list[ChatMessage] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    results: dict[int, dict] = field(default_factory=dict)
    repairs_used: int = 0
    llm_calls: int = 0


FAIL_MESSAGES = {
    "invalid_output": "The model kept returning output that failed validation, so no answer is shown.",
    "unverified_answer": "The model's answer contained numbers the tools did not produce, so it was rejected.",
    "step_limit": "The model did not reach a verified answer within the step limit.",
    "deadline": "The analysis took too long and was stopped.",
    "llm_unavailable": "The AI service is unavailable.",
}


class Agent:
    def __init__(self, llm: LLMClient, max_steps: int = 6, max_repairs: int = 2, deadline_s: float = 90.0):
        self.llm = llm
        self.max_steps = max_steps
        self.max_repairs = max_repairs
        self.deadline_s = deadline_s

    async def answer(
        self, question: str, entries: list[LogEntry], overview: dict,
        history: list[tuple[str, str]] | None = None,
    ) -> AgentOutcome:
        """`history`: earlier (question, answer) pairs, oldest first, so follow-ups like
        "how do I fix it?" have a referent. Earlier answers are context, never evidence:
        the judge only accepts figures from this run's tool results."""
        started = time.monotonic()
        deadline = started + self.deadline_s
        system = build_system_prompt(overview)
        run = _Run(question=question, messages=[ChatMessage("user", question_message(question, history or []))])

        for _ in range(self.max_steps):
            remaining = deadline - time.monotonic()
            if remaining < 1:
                return self._fail(run, "deadline", started)
            try:
                run.llm_calls += 1
                raw = await self.llm.generate(system, run.messages, remaining)
            except LLMError as e:
                return self._fail(run, "llm_unavailable", started, detail=str(e))
            run.messages.append(ChatMessage("assistant", raw))

            try:
                step = parse_step(raw)
            except StepParseError as e:
                if not self._repair(run, "schema", str(e), raw):
                    return self._fail(run, "invalid_output", started)
                continue

            if isinstance(step, ToolCallStep):
                problem = self._run_tool(run, step, entries)
                if problem and not self._repair(run, "tool", problem, raw):
                    return self._fail(run, "invalid_output", started)
                continue

            verdict = judge_answer(step.answer, step.evidence, run.results, question)
            if verdict.ok:
                return AgentOutcome(
                    status="answered", answer=step.answer.strip(), error_code=None, error=None,
                    trace=run.trace, verification=verdict.as_dict() | {"evidence": step.evidence},
                    model=self.llm.model_name, llm_calls=run.llm_calls, duration_ms=_ms(started),
                )
            if not self._repair(run, "judge", verdict.problem(), raw, answer=step.answer):
                return self._fail(run, "unverified_answer", started)

        return self._fail(run, "step_limit", started)

    def _run_tool(self, run: _Run, step: ToolCallStep, entries: list[LogEntry]) -> str | None:
        """Run one tool call. Returns a problem description when the call itself is invalid."""
        tool = TOOLS.get(step.tool)
        if tool is None:
            return f"unknown tool '{step.tool}'. Available: {', '.join(TOOLS)}"
        t0 = time.monotonic()
        try:
            args, result = tool.run(entries, step.args)
        except ValidationError as e:
            return f"invalid args for {step.tool}: {_short_errors(e)}"
        call_id = len(run.results) + 1
        run.results[call_id] = result
        run.trace.append({
            "kind": "tool_call", "id": call_id, "tool": step.tool,
            "args": args.model_dump(mode="json", exclude_none=True), "reason": step.reason,
            "result": result, "ms": _ms(t0),
        })
        text = result_json(result)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + '..." (truncated)'
        run.messages.append(ChatMessage("user", tool_result_message(call_id, step.tool, text)))
        return None

    def _repair(self, run: _Run, stage: str, problem: str, raw: str, answer: str | None = None) -> bool:
        """Record a rejection and, if budget remains, ask the model to fix its reply."""
        item = {"kind": "rejected", "stage": stage, "problem": problem, "raw": (raw or "")[:MAX_RAW_IN_TRACE]}
        if answer is not None:
            item["answer"] = answer
        run.trace.append(item)
        if run.repairs_used >= self.max_repairs:
            return False
        run.repairs_used += 1
        run.messages.append(ChatMessage("user", repair_message(problem)))
        return True

    def _fail(self, run: _Run, code: str, started: float, detail: str | None = None) -> AgentOutcome:
        return AgentOutcome(
            status="failed", answer=None, error_code=code, error=detail or FAIL_MESSAGES[code],
            trace=run.trace, verification=None, model=self.llm.model_name,
            llm_calls=run.llm_calls, duration_ms=_ms(started),
        )


def _ms(since: float) -> int:
    return int((time.monotonic() - since) * 1000)
