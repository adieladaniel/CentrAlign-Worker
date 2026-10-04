"""AgentRunner: the task-agnostic loop. Context -> LLM -> (policy/approval) -> tools -> record -> guards -> verify."""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Awaitable, Callable

from worker.agent.broker import InteractionBroker
from worker.agent.context import build_messages, compress_step
from worker.agent.guards import GuardEvent, Guards, counts_toward_budget
from worker.agent.memory import WorkingMemory
from worker.agent.policy import ApprovalPolicy
from worker.agent.prompts import (
    DENIED_TEMPLATE,
    FINISH_NOT_ALONE,
    NO_TOOL_NUDGE,
    SKIPPED_AFTER_FAILURE,
    VERIFY_FAILED_TEMPLATE,
)
from worker.agent.report import build_report, render_markdown
from worker.agent.state import (
    STORED_OBSERVATION_LIMIT,
    TERMINAL_STATUSES,
    PlanItem,
    RunState,
    Step,
    truncate,
    utcnow,
)
from worker.agent.thoughts import THOUGHT_LIMIT, clean_thought
from worker.agent.verifier import Verdict, Verifier
from worker.config import Settings
from worker.llm.base import LLMClient, LLMError, LLMResponse, ToolCall
from worker.tools.base import Risk, Tool, ToolContext, ToolResult
from worker.tools.control import normalize_finish
from worker.tools.registry import ToolRegistry
from worker.tools.retry import execute_with_retry

log = logging.getLogger(__name__)

EventSink = Callable[[str, dict], Awaitable[None]]
MAX_VERIFY_ROUNDS = 2  # feedback rounds after a failed audit; a further failed audit ends the run as failed
MAX_TEXT_ONLY_REPLIES = 2
EVENT_OBSERVATION_LIMIT = 2000
_RETRY_NOTE = re.compile(r"after (\d+) automatic retr")
_STOP_WORDS = ("stop", "cancel", "abort", "quit", "give up")


class _Stop(Exception):
    """Internal control flow: end the run with this status."""

    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


class AgentRunner:
    """Runs one task to completion. Usage: ``state = await AgentRunner(task, llm, registry, settings, broker).run()``.

    Events are pushed to ``event_sink(event_type, payload)``; every payload carries ``run_id``.
    """

    def __init__(
        self,
        task: str,
        llm: LLMClient,
        registry: ToolRegistry,
        settings: Settings,
        broker: InteractionBroker,
        run_id: str | None = None,
        event_sink: EventSink | None = None,
    ):
        self.llm, self.registry, self.settings, self.broker = llm, registry, settings, broker
        self.state = RunState(run_id=run_id or uuid.uuid4().hex[:8], task=task)
        self.memory = WorkingMemory(self.state.facts)
        self.policy = ApprovalPolicy(settings.APPROVAL_MODE)
        self.guards = Guards(settings.MAX_STEPS, settings.MAX_CONSECUTIVE_FAILURES, settings.RUN_TIMEOUT_S)
        self.run_dir = Path(settings.RUNS_DIR) / self.state.run_id
        self.ctx = ToolContext(
            run_id=self.state.run_id,
            run_dir=self.run_dir,
            workspace=Path(settings.WORKSPACE_DIR),
            settings=settings,
            memory=self.memory,
            emit=self._emit,
            extras={"interact": self._interact},
        )
        self._sink = event_sink
        self._cancelled = asyncio.Event()
        self._turn = 0
        self._text_only = 0
        self._nudges: list[str] = []
        self._feedback = ""
        self._verify_failures = 0
        self._claim: dict | None = None
        self._verdict: Verdict | None = None
        self._approvals = {"requested": 0, "approved": 0, "denied": 0}
        if hasattr(llm, "on_retry"):  # ResilientLLM: surface network waits in the UI and let cancel interrupt them
            llm.on_retry = self._on_llm_retry

    async def _on_llm_retry(self, info: dict) -> None:
        """Called by ResilientLLM before each wait during a network outage."""
        if self._cancelled.is_set():
            raise _Stop("cancelled", "Cancelled by the user.")
        budget_min = max(1, round(info.get("budget_s", 0) / 60))
        message = f"Network unavailable, retrying the language model for up to {budget_min} min…"
        await self._emit("llm_waiting", {**info, "message": message})

    def cancel(self) -> None:
        """Ask the run to stop at the next safe point (also releases a pending approval/question)."""
        self._cancelled.set()

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> RunState:
        """Execute the task and return the final state (also persisted under ``runs/<run_id>/``)."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.state.status = "running"
        self._save()
        await self._emit("run_started", {"task": self.state.task, "approval_mode": self.policy.mode})
        status, reason = "failed", ""
        try:
            await self._loop()
        except _Stop as stop:
            status, reason = stop.status, stop.reason
        except asyncio.CancelledError:
            await self._finish_run("cancelled", "The run task was cancelled.")
            raise
        except Exception as exc:  # noqa: BLE001 - never lose the partial report to a bug
            log.exception("agent run crashed")
            status, reason = "failed", f"Internal error: {type(exc).__name__}: {exc}"
        await self._finish_run(status, reason)
        return self.state

    async def _loop(self) -> None:
        tool_names = self.registry.names()
        specs = self.registry.specs()
        force_tool = False
        while True:
            await self._check_stop()
            messages = build_messages(
                self.state, self.settings, tool_names, nudges=self._nudges, feedback=self._feedback
            )
            self._nudges = []
            try:
                response = await self.llm.complete(messages, specs, tool_choice="any" if force_tool else "auto")
            except LLMError as err:
                raise _Stop("needs_attention", f"The language model could not be reached: {err}") from err
            await self._account(response)
            self._turn += 1
            thought, is_summary = self._thought_of(response)
            if thought:
                await self._emit("thought", {"turn": self._turn, "text": thought})

            if not response.tool_calls:
                self._handle_text_only(response.text)
                force_tool = True
                continue
            self._text_only, force_tool = 0, False
            await self._handle_calls(response, thought, is_summary)
            event = self.guards.check_progress()
            if event is not None:
                await self._apply_guard(event)

    async def _check_stop(self) -> None:
        if self._cancelled.is_set():
            raise _Stop("cancelled", "Cancelled by the user.")
        work = sum(1 for s in self.state.steps if counts_toward_budget(s))
        event = self.guards.check_limits(work, len(self.state.steps))
        if event is not None:
            await self._emit("guard_triggered", {"guard": event.guard, "level": event.level, "message": event.message})
            raise _Stop("needs_attention", f"{event.message} Stopped with partial progress.")

    # ------------------------------------------------------------------ turn handling

    def _handle_text_only(self, text: str) -> None:
        """The model answered without a tool call: nudge once (forcing a tool call), then give up honestly."""
        self._text_only += 1
        self._add_step(Step(kind="llm", thought=text, ok=False, error="no_tool_call", turn=self._turn))
        if self._text_only >= MAX_TEXT_ONLY_REPLIES:
            self._claim = {"summary": text, "outputs": {}, "evidence": []}
            raise _Stop("needs_attention", "The agent stopped calling tools without finishing; its last message is the summary.")
        self._nudges.append(NO_TOOL_NUDGE)

    @staticmethod
    def _thought_of(response: LLMResponse) -> tuple[str, bool]:
        """What to show as the step's thought: the model's own text if any, else its condensed reasoning summary."""
        if response.text.strip():
            return response.text.strip(), False
        return clean_thought(getattr(response, "thought", ""), THOUGHT_LIMIT), True

    async def _handle_calls(self, response: LLMResponse, thought: str, is_summary: bool) -> None:
        calls, broken = response.tool_calls, False
        for call in calls:
            step = Step(thought=thought, thought_is_summary=is_summary and bool(thought), tool=call.name, args=call.args,
                        turn=self._turn, tool_call_id=call.id)
            if call.name == "finish" and len(calls) > 1:
                await self._finish_step(self._fail(step, "finish_not_alone", FINISH_NOT_ALONE))
            elif broken:
                await self._finish_step(self._fail(step, "skipped", SKIPPED_AFTER_FAILURE))
            elif call.name == "finish":
                await self._handle_finish(step)
            else:
                broken = not (await self._execute(step)).ok

    async def _execute(self, step: Step) -> Step:
        """classify -> approval gate -> run with retries -> record."""
        tool = self.registry.get(step.tool)
        if tool is None:
            known = ", ".join(self.registry.names())
            return await self._finish_step(self._fail(step, "unknown_tool", f"Unknown tool '{step.tool}'. Available: {known}"))
        risk = self._classify(tool, step.args)
        step.risk = risk.value
        if self.policy.needs_approval(tool, step.args, risk):
            approved, note = await self._seek_approval(tool, step, risk)
            if not approved:
                text = DENIED_TEMPLATE.format(note=note or "no reason given")
                return await self._finish_step(self._fail(step, "denied", text))
        step.kind = "ask_user" if tool.name == "ask_user" else "tool"
        self.memory.step = len(self.state.steps) + 1
        await self._emit("tool_started", {"turn": self._turn, "tool": tool.name, "args": step.args, "risk": step.risk,
                                          "action": tool.describe_action(step.args)})
        started = time.monotonic()
        result = await execute_with_retry(tool, step.args, self.ctx)
        step.duration_ms = int((time.monotonic() - started) * 1000)
        step.ok, step.error, step.evidence = result.ok, result.error, list(result.evidence)
        step.observation = truncate(result.observation, STORED_OBSERVATION_LIMIT)
        match = _RETRY_NOTE.search(result.observation)
        step.retries = int(match.group(1)) if match else 0
        if tool.name == "update_plan" and result.ok:
            await self._apply_plan(result.data or {})
        return await self._finish_step(step)

    @staticmethod
    def _classify(tool: Tool, args: dict) -> Risk:
        try:
            return tool.classify(args)
        except Exception:  # noqa: BLE001 - malformed args: assume the tool's declared risk
            return tool.risk

    async def _apply_plan(self, data: dict) -> None:
        self.state.plan = [PlanItem(**item) for item in data.get("plan", [])]
        self.state.goal_summary = data.get("goal_summary", self.state.goal_summary)
        self.state.success_criteria = data.get("success_criteria", self.state.success_criteria)
        await self._emit("plan_updated", {"plan": data.get("plan", []), "goal_summary": self.state.goal_summary,
                                          "success_criteria": self.state.success_criteria})

    # ------------------------------------------------------------------ approval and questions

    async def _seek_approval(self, tool: Tool, step: Step, risk: Risk) -> tuple[bool, str]:
        action = tool.describe_action(step.args)
        context: dict[str, Any] = {"reason": step.thought, "facts": self.memory.render()}
        try:  # what the human is about to approve, e.g. the filled form fields; never blocks the approval
            preview = await asyncio.wait_for(tool.approval_preview(step.args, self.ctx), timeout=5)
        except Exception:  # noqa: BLE001 - a preview is a convenience only
            preview = None
        if preview:
            context["form_values"] = preview
        payload = {"tool": tool.name, "action": action, "args": step.args, "risk": risk.value, "context": context}
        self._approvals["requested"] += 1
        response = await self._interact("approval", payload)
        response = response if isinstance(response, dict) else {}
        approved = bool(response.get("approved"))
        note = str(response.get("note") or ("" if response else "no response received"))
        self.policy.record_response(tool, risk, response)
        self._approvals["approved" if approved else "denied"] += 1
        self._add_step(Step(kind="approval", tool=tool.name, args=step.args, risk=risk.value, ok=approved,
                            thought=note, turn=self._turn,
                            observation=f"{'APPROVED' if approved else 'DENIED'}: {action}"))
        return approved, note

    async def _interact(self, kind: str, payload: dict) -> Any:
        """Ask the human via the broker (kind: "approval" | "question"). Returns None on timeout or cancellation."""
        approval = kind == "approval"
        self.state.status = "waiting_approval" if approval else "waiting_user"
        self._save()

        async def announce(request_id: str) -> None:
            await self._emit("approval_requested" if approval else "question_asked", {"request_id": request_id, **payload})

        request = asyncio.ensure_future(self.broker.request(kind, payload, on_created=announce))
        cancelled = asyncio.ensure_future(self._cancelled.wait())
        response: Any = None
        try:
            await asyncio.wait({request, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if request.done() and not request.cancelled() and request.exception() is None:
                response = request.result()
        finally:
            for pending in (request, cancelled):
                pending.cancel()
            if self.state.status in ("waiting_approval", "waiting_user"):
                self.state.status = "running"
        await self._emit("approval_resolved" if approval else "question_answered",
                         {"response": response, "answered": response is not None})
        return response

    # ------------------------------------------------------------------ finish and verification

    async def _handle_finish(self, step: Step) -> None:
        claim = normalize_finish(step.args)
        self._claim = claim
        if claim["status"] != "success":
            await self._finish_step(self._ok(step, "Finish recorded."))
            honest = "failed" if claim["status"] == "failed" else "needs_attention"
            raise _Stop(honest, f"The agent finished with status '{claim['status']}'; no verification was run.")

        verdict = await self._verify(claim)
        self._verdict = verdict
        if verdict.passed:
            await self._finish_step(self._ok(step, "Independent verification passed."))
            raise _Stop("succeeded", "")
        if verdict.inconclusive:
            await self._finish_step(self._fail(step, "verification_failed", f"Verification inconclusive: {verdict.summary}"))
            raise _Stop("needs_attention", f"The result could not be independently verified: {verdict.summary}")
        problems = "\n".join(f"- {c['criterion']}: {c['observed']}" for c in verdict.failed_checks) or "- (no details)"
        feedback = VERIFY_FAILED_TEMPLATE.format(problems=problems, summary=verdict.summary)
        await self._finish_step(self._fail(step, "verification_failed", feedback))
        self._verify_failures += 1
        if self._verify_failures > MAX_VERIFY_ROUNDS:
            raise _Stop("failed", f"Independent verification failed after {MAX_VERIFY_ROUNDS} correction rounds.")
        self._feedback = f"{problems}\n(Auditor summary: {verdict.summary})"

    async def _verify(self, claim: dict) -> Verdict:
        self.state.status = "verifying"
        self._save()
        await self._emit("verification_started", {"criteria": self.state.success_criteria})
        verdict = await Verifier(self.llm, self.registry).verify(
            task=self.state.task, criteria=self.state.success_criteria, claim=claim,
            facts_text=self.memory.render(), ctx=self.ctx, on_step=self._record_verify_step,
        )
        self.state.usage["llm_calls"] += verdict.llm_calls
        self.state.usage["input_tokens"] += verdict.input_tokens
        self.state.usage["output_tokens"] += verdict.output_tokens
        self.state.status = "running"
        await self._emit("verification_finished", {"passed": verdict.passed, "inconclusive": verdict.inconclusive,
                                                   "checks": verdict.checks, "summary": verdict.summary})
        return verdict

    async def _record_verify_step(self, tool: str, args: dict, result: ToolResult, duration_ms: int) -> None:
        step = Step(kind="verify", tool=tool, args=args, ok=result.ok, error=result.error, evidence=list(result.evidence),
                    observation=truncate(result.observation, STORED_OBSERVATION_LIMIT), duration_ms=duration_ms)
        await self._finish_step(step, phase="verify", feed_guards=False)

    # ------------------------------------------------------------------ guards

    async def _apply_guard(self, event: GuardEvent) -> None:
        await self._emit("guard_triggered", {"guard": event.guard, "level": event.level, "message": event.message})
        self._add_step(Step(kind="system", thought=event.message, ok=False, error=event.guard, turn=self._turn))
        if event.level == "nudge":
            self._nudges.append(event.message)
            return
        recent = next((compress_step(s) for s in reversed(self.state.steps) if s.kind == "tool" and not s.ok), "")
        question = f"I'm stuck: {event.message} Last problem: {recent or 'none recorded'}. How should I proceed?"
        response = await self._interact("question", {"question": question, "options": ["Keep trying", "Stop the task"]})
        answer = str(response.get("answer", "")).strip() if isinstance(response, dict) else ""
        if not answer or answer.lower().startswith(_STOP_WORDS):
            raise _Stop("needs_attention", f"Stopped: {event.message} No guidance was given to continue.")
        self._nudges.append(f"The user answered your request for help: {answer}")
        self.guards.reset_after_guidance()

    # ------------------------------------------------------------------ recording helpers

    @staticmethod
    def _ok(step: Step, observation: str) -> Step:
        step.ok, step.observation = True, observation
        return step

    @staticmethod
    def _fail(step: Step, error: str, observation: str) -> Step:
        step.ok, step.error, step.observation = False, error, observation
        return step

    def _add_step(self, step: Step) -> Step:
        self.state.add_step(step)
        self._save()
        return step

    async def _finish_step(self, step: Step, *, phase: str = "work", feed_guards: bool = True) -> Step:
        """Store the step, feed the guards, persist, and tell the UI."""
        self._add_step(step)
        if feed_guards:
            self.guards.record(step)
        payload = asdict(step)
        payload["observation"] = truncate(step.observation, EVENT_OBSERVATION_LIMIT)
        await self._emit("tool_finished", {**payload, "phase": phase})
        return step

    def _save(self) -> None:
        self.state.save(self.run_dir / "run.json")

    async def _emit(self, event_type: str, payload: dict) -> None:
        if self._sink is None:
            return
        try:
            await self._sink(event_type, {"run_id": self.state.run_id, **payload})
        except Exception:  # noqa: BLE001 - a broken UI must not kill the run
            log.exception("event sink failed for %s", event_type)

    async def _account(self, response: LLMResponse) -> None:
        usage = self.state.usage
        usage["llm_calls"] += 1
        usage["input_tokens"] += response.usage.input_tokens
        usage["output_tokens"] += response.usage.output_tokens
        model = getattr(self.llm, "active_model", None)
        extra: dict[str, Any] = {}
        if model:
            switches = getattr(self.llm, "model_switches", 0)
            if model != usage.get("model") and usage.get("model"):
                await self._emit("model_switched", {"from": usage["model"], "to": model})
            usage["model"], usage["model_switches"] = model, switches
            extra = {"model": model, "model_switches": switches}
        await self._emit("usage", {**usage, **extra, "llm_retries": getattr(self.llm, "retries", 0)})

    # ------------------------------------------------------------------ wrap-up

    async def _finish_run(self, status: str, reason: str) -> None:
        assert status in TERMINAL_STATUSES
        await self._close_browser()
        report = build_report(self.state, status=status, claim=self._claim, verdict=self._verdict, reason=reason,
                              approvals=self._approvals, run_dir=self.run_dir)
        self.state.status, self.state.final_report, self.state.finished_at = status, report, utcnow()
        self._save()
        (self.run_dir / "report.md").write_text(render_markdown(self.state, report), encoding="utf-8")
        await self._emit("run_finished", {"status": status, "report": report})

    async def _close_browser(self) -> None:
        try:
            from worker.tools.browser import close_browser
        except ImportError:
            return
        try:
            await close_browser(self.ctx)
        except Exception:  # noqa: BLE001 - cleanup must not mask the run result
            log.exception("closing the browser failed")
