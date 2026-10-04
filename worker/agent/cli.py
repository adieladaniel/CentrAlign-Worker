"""Terminal front-end: ``python -m worker.agent "task" [--approval MODE] [--headed]``. Handy for debugging."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from worker.agent.broker import AutoBroker
from worker.agent.runner import AgentRunner
from worker.agent.toolset import build_registry
from worker.config import get_settings
from worker.llm.factory import make_llm


def _show(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


async def print_event(event_type: str, payload: dict) -> None:
    """Render one runner event as a terminal line."""
    if event_type == "thought":
        print(f"\n> {_show(payload['text'])}")
    elif event_type == "plan_updated":
        print("  plan: " + " | ".join(f"{p['status']}: {p['text']}" for p in payload["plan"]))
    elif event_type == "tool_started":
        print(f"  -> {payload['action']}  [{payload['risk']}]")
    elif event_type == "tool_finished" and payload["tool"]:
        mark = "ok " if payload["ok"] else "ERR"
        print(f"  <- {mark} {payload['tool']}: {_show(payload['observation'], 220)}")
    elif event_type == "model_switched":
        print(f"  ~~ model switched: {payload['from']} -> {payload['to']}")
    elif event_type == "llm_waiting":
        print(f"  .. {payload['message']} (waited {payload.get('waited_s', 0):.0f}s)")
    elif event_type == "memory_updated":
        print(f"  remembered {payload['key']} = {_show(payload['value'], 100)}")
    elif event_type == "guard_triggered":
        print(f"  !! guard {payload['guard']} ({payload['level']}): {_show(payload['message'])}")
    elif event_type == "verification_started":
        print("\n=== independent verification ===")
    elif event_type == "verification_finished":
        print(f"  verdict: {'PASSED' if payload['passed'] else 'FAILED'}  {_show(payload['summary'])}")
        for check in payload["checks"]:
            print(f"   [{'x' if check['passed'] else ' '}] {check['criterion']}: {_show(check['observed'], 150)}")
    elif event_type == "run_finished":
        report = payload["report"]
        print(f"\n=== {payload['status'].upper()} ===\n{report['summary']}")
        if report["outputs"]:
            print(json.dumps(report["outputs"], indent=2, ensure_ascii=False))
        print("stats:", report["stats"])


async def _ask(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


async def answer_in_terminal(kind: str, payload: dict) -> dict:
    """Broker policy for the CLI: ask the person at the terminal."""
    if kind == "approval":
        print(f"\n*** APPROVAL NEEDED ({payload['risk']}) ***\n  {payload['action']}")
        if payload["context"].get("reason"):
            print(f"  reason: {_show(payload['context']['reason'])}")
        choice = (await _ask("  approve? [y]es / [n]o / [a]lways for similar: ")).strip().lower()
        if choice.startswith("y"):
            return {"approved": True}
        if choice.startswith("a"):
            return {"approved": True, "remember": True}
        return {"approved": False, "note": (await _ask("  why not (optional): ")).strip()}
    print(f"\n*** QUESTION ***\n  {payload['question']}")
    if payload.get("options"):
        print("  options: " + " / ".join(payload["options"]))
    return {"answer": (await _ask("  your answer: ")).strip()}


async def run_cli(task: str, approval: str | None, headed: bool) -> int:
    """Run one task in the terminal; returns a process exit code (0 = succeeded)."""
    overrides: dict = {"HEADLESS": not headed}
    if approval:
        overrides["APPROVAL_MODE"] = approval
    settings = get_settings().model_copy(update=overrides)
    runner = AgentRunner(
        task, make_llm(settings), build_registry(settings), settings, AutoBroker(answer_in_terminal), event_sink=print_event
    )
    state = await runner.run()
    print(f"\nRun files: {runner.run_dir}")
    return 0 if state.status == "succeeded" else 1


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(prog="python -m worker.agent", description="Run the autonomous worker on a task.")
    parser.add_argument("task", help="The task, in plain language.")
    parser.add_argument("--approval", choices=["auto", "confirm_writes", "confirm_all"], help="Approval mode.")
    parser.add_argument("--headed", action="store_true", help="Show the browser window.")
    args = parser.parse_args()
    sys.exit(asyncio.run(run_cli(args.task, args.approval, args.headed)))
