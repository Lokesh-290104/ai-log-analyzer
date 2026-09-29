"""Incident scenarios against the REAL model: does the agent reach the right conclusion?

run_eval.py checks facts on one big log; this checks reasoning across many small incidents
(root cause, recovery, red herrings, healthy logs, follow-ups). Not part of CI (needs a key).
Run from backend/:
    python -m eval.run_scenarios                   # all scenarios
    python -m eval.run_scenarios disk-full k8s     # names containing these words

A scenario passes when the agent answers (a fail-closed "failed" never counts as a wrong
answer, but it is not a pass either), every `must` group has at least one phrase in the
answer, and no `must_not` phrase appears.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from app.agent import Agent
from app.config import Settings
from app.llm import create_llm_client
from app.main import build_overview
from app.parser import parse_logs

SCENARIOS = Path(__file__).with_name("scenarios.json")
PAUSE_S = float(os.getenv("EVAL_PAUSE_S", "15"))


def grade(answer: str, case: dict) -> list[str]:
    text = answer.lower()
    problems = [f"missing one of {group}" for group in case["must"] if not any(p.lower() in text for p in group)]
    problems += [f"contains '{bad}'" for bad in case.get("must_not", []) if bad.lower() in text]
    return problems


async def main(filters: list[str]) -> int:
    settings = Settings()
    agent = Agent(create_llm_client(settings), settings.max_agent_steps, settings.max_repairs,
                  settings.agent_deadline_ms / 1000)
    cases = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    logs = {c["name"]: c["log"] for c in cases if "log" in c}
    selected = [c for c in cases if not filters or any(f in c["name"] for f in filters)]

    passed = 0
    for i, case in enumerate(selected):
        if i:
            await asyncio.sleep(PAUSE_S)
        entries = parse_logs("\n".join(case.get("log") or logs[case["log_from"]])).entries
        history = [tuple(h) for h in case.get("history", [])]
        started = time.monotonic()
        outcome = await agent.answer(case["question"], entries, build_overview(entries), history)
        answer = outcome.answer or ""
        problems = grade(answer, case) if outcome.status == "answered" else [f"failed: {outcome.error_code}"]
        passed += not problems
        tools = [t["tool"] for t in outcome.trace if t["kind"] == "tool_call"]
        rejected = [t["stage"] for t in outcome.trace if t["kind"] == "rejected"]
        print(f"\n[{'PASS' if not problems else 'FAIL'}] {case['name']}: {case['question']}")
        print(f"  tools={tools} rejections={rejected} {time.monotonic() - started:.1f}s")
        print("  answer: " + (answer or str(outcome.error)).replace("\n", "\n          "))
        for p in problems:
            print(f"  !! {p}")
    print(f"\n{passed}/{len(selected)} scenarios passed")
    return 0 if passed == len(selected) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
