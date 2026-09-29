"""Run the agent against the REAL model on the sample log and score it.

Not part of CI (needs an API key and spends quota). Run from backend/:
    python -m eval.run_eval            # all questions
    python -m eval.run_eval 1 4        # only questions 1 and 4 (1-based)

Scores per question:
  verified  the answer passed the judge (or failed closed: never a wrong answer shown)
  tools     at least one expected tool was used (skipped when none expected)
  facts     every expected fact appears in the answer ("a|b" = either)
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
from app.sample import generate_sample_logs

QUESTIONS = Path(__file__).with_name("questions.json")
# Free tiers cap tokens per minute; pause between questions so the eval measures the agent, not the quota.
PAUSE_S = float(os.getenv("EVAL_PAUSE_S", "15"))


async def main(selected: set[int]) -> int:
    settings = Settings()
    llm = create_llm_client(settings)
    agent = Agent(llm, settings.max_agent_steps, settings.max_repairs, settings.agent_deadline_ms / 1000)
    entries = parse_logs(generate_sample_logs()).entries
    overview = build_overview(entries)
    cases = json.loads(QUESTIONS.read_text(encoding="utf-8"))

    passed = 0
    run = 0
    for number, case in enumerate(cases, start=1):
        if selected and number not in selected:
            continue
        if run:
            await asyncio.sleep(PAUSE_S)
        run += 1
        started = time.monotonic()
        outcome = await agent.answer(case["question"], entries, overview)
        tools = [t["tool"] for t in outcome.trace if t["kind"] == "tool_call"]
        rejections = [t["stage"] for t in outcome.trace if t["kind"] == "rejected"]
        answer = outcome.answer or ""
        tools_ok = not case["expect_tools"] or any(t in case["expect_tools"] for t in tools)
        # A fact may list alternatives separated by "|" (e.g. "193|192": the total, or ERROR + FATAL parts).
        facts_ok = outcome.status == "answered" and all(
            any(alt.lower() in answer.lower() for alt in f.split("|")) for f in case["expect_in_answer"]
        )
        clean = all(bad.lower() not in answer.lower() for bad in case.get("expect_not_in_answer", []))
        ok = tools_ok and facts_ok and clean
        passed += ok
        print(f"\n[{'PASS' if ok else 'FAIL'}] Q{number}: {case['question']}")
        print(f"  status={outcome.status} code={outcome.error_code} tools={tools} rejections={rejections} "
              f"llm_calls={outcome.llm_calls} {time.monotonic() - started:.1f}s model={outcome.model}")
        print(f"  answer: {answer or outcome.error}")
    print(f"\n{passed}/{run} passed")
    return 0 if passed == run else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main({int(a) for a in sys.argv[1:]})))
