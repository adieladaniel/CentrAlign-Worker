"""End-to-end tests of the AgentRunner using a scripted LLM and simple fake tools (no network, no browser)."""
from __future__ import annotations

import asyncio
import itertools
import json

import pytest

from worker.agent.broker import AutoBroker, InteractionBroker
from worker.agent.runner import AgentRunner
from worker.config import Settings
from worker.llm.base import LLMError, LLMResponse, ToolCall, Usage
from worker.llm.fake import ScriptedLLM
from worker.tools.base import Risk, Tool, ToolResult
from worker.tools.control import FinishTool, UpdatePlanTool
from worker.tools.interaction import AskUserTool
from worker.tools.memory_tools import RememberTool
from worker.tools.registry import ToolRegistry

AUDITOR_PREFIX = "You are an independent auditor"
_ids = itertools.count(1)


# ---------------------------------------------------------------- scripting helpers


def call(name: str, **args) -> ToolCall:
    return ToolCall(id=f"call_{next(_ids)}", name=name, args=args)


def reply(text: str = "", *calls: ToolCall) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=list(calls), usage=Usage(10, 5))


def plan_call(*items: tuple[str, str]) -> ToolCall:
    return call(
        "update_plan",
        goal_summary="Goal",
        success_criteria=["The record is saved", "Nothing else changed"],
        items=[{"text": t, "status": s} for t, s in items],
    )


def finish_call(status: str = "success", summary: str = "All done.") -> ToolCall:
    return call(
        "finish",
        status=status,
        summary=summary,
        evidence=[{"claim": "record saved", "source": "save_record result"}],
        outputs=[{"name": "record_id", "value": "R-1"}],
    )


def verdict(passed: bool, observed: str = "looked right") -> LLMResponse:
    return reply("", call("submit_verdict", passed=passed, summary="audit summary",
                          checks=[{"criterion": "The record is saved", "passed": passed, "observed": observed}]))


def make_llm(worker, auditor=()) -> ScriptedLLM:
    """Route calls by system prompt: worker turns vs. auditor turns. ``worker`` may be a list or a callable."""
    worker_queue, auditor_queue = list(worker) if not callable(worker) else worker, list(auditor)

    def responder(messages, tools):
        if messages[0].content.startswith(AUDITOR_PREFIX):
            return auditor_queue.pop(0)
        return worker_queue(messages, tools) if callable(worker_queue) else worker_queue.pop(0)

    return ScriptedLLM(responder)


def worker_calls(llm: ScriptedLLM) -> list[dict]:
    return [c for c in llm.calls if not c["messages"][0].content.startswith(AUDITOR_PREFIX)]


def auditor_calls(llm: ScriptedLLM) -> list[dict]:
    return [c for c in llm.calls if c["messages"][0].content.startswith(AUDITOR_PREFIX)]


def full_text(messages) -> str:
    return "\n".join(m.content for m in messages)


# ---------------------------------------------------------------- fake tools


class ReadNoteTool(Tool):
    name = "read_note"
    description = "Read a note (read-only)."
    parameters = {"type": "object", "properties": {"topic": {"type": "string"}}}

    async def run(self, args, ctx):
        return ToolResult(True, f"note about {args.get('topic', 'x')}: value 42")


class SaveRecordTool(Tool):
    name = "save_record"
    description = "Save a record (write)."
    parameters = {"type": "object", "properties": {"value": {"type": "string"}}}
    risk = Risk.WRITE
    read_only_safe = False

    def __init__(self):
        self.saved: list[dict] = []

    async def run(self, args, ctx):
        self.saved.append(args)
        return ToolResult(True, f"Saved record R-{len(self.saved)}")


class FlakyTool(Tool):
    """Always fails with a semantic (non-retryable) error."""

    name = "primary_path"
    description = "A path that is broken."
    parameters = {"type": "object", "properties": {"n": {"type": "integer"}}}
    read_only_safe = False

    async def run(self, args, ctx):
        return ToolResult(False, "Validation failed: Due date must be YYYY-MM-DD", "validation")


class AlternativeTool(Tool):
    name = "alternative_path"
    description = "A path that works."
    parameters = {"type": "object", "properties": {}}
    risk = Risk.WRITE
    read_only_safe = False

    async def run(self, args, ctx):
        return ToolResult(True, "Saved via alternative path")


class DeleteTool(Tool):
    name = "delete_thing"
    description = "Delete (irreversible)."
    parameters = {"type": "object", "properties": {}}
    risk = Risk.IRREVERSIBLE
    read_only_safe = False

    async def run(self, args, ctx):
        return ToolResult(True, "Deleted")


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "ws").mkdir()
    return Settings(
        _env_file=None, RUNS_DIR=str(tmp_path / "runs"), WORKSPACE_DIR=str(tmp_path / "ws"),
        MAX_STEPS=30, MAX_CONSECUTIVE_FAILURES=4, RUN_TIMEOUT_S=60, APPROVAL_MODE="confirm_writes",
    )


@pytest.fixture
def save_tool():
    return SaveRecordTool()


@pytest.fixture
def registry(save_tool):
    reg = ToolRegistry()
    for tool in (ReadNoteTool(), save_tool, FlakyTool(), AlternativeTool(), DeleteTool(),
                 RememberTool(), UpdatePlanTool(), FinishTool(), AskUserTool()):
        reg.register(tool)
    return reg


class Harness:
    """Runs a task and collects events."""

    def __init__(self, llm, registry, settings, broker=None, task="Do the task"):
        self.events: list[tuple[str, dict]] = []
        self.broker = broker or AutoBroker.approve_all()
        self.runner = AgentRunner(task, llm, registry, settings, self.broker, run_id="t1", event_sink=self._sink)

    async def _sink(self, event_type, payload):
        self.events.append((event_type, payload))

    async def run(self):
        return await self.runner.run()

    def types(self) -> list[str]:
        return [t for t, _ in self.events]

    def of(self, event_type: str) -> list[dict]:
        return [p for t, p in self.events if t == event_type]


# ---------------------------------------------------------------- tests


async def test_happy_path_plan_tool_remember_finish_verified(registry, settings, save_tool):
    llm = make_llm(
        worker=[
            reply("Plan it.", plan_call(("Read the note", "in_progress"), ("Save the record", "pending")),
                  call("read_note", topic="a")),
            reply("Remember and save.", call("remember", key="answer", value="42", source="read_note a"),
                  call("save_record", value="42")),
            reply("Verified in output.", finish_call()),
        ],
        auditor=[reply("Check.", call("read_note", topic="a")), verdict(True, "saw R-1")],
    )
    h = Harness(llm, registry, settings)
    state = await h.run()

    assert state.status == "succeeded"
    assert save_tool.saved == [{"value": "42"}]
    assert state.goal_summary == "Goal" and state.success_criteria == ["The record is saved", "Nothing else changed"]
    assert [p.text for p in state.plan] == ["Read the note", "Save the record"]
    assert state.facts["answer"].value == "42" and state.facts["answer"].source == "read_note a"
    assert state.facts["answer"].step > 0

    report = state.final_report
    assert report["status"] == "succeeded" and report["summary"] == "All done."
    assert report["outputs"] == {"record_id": "R-1"}
    assert report["evidence"][0]["claim"] == "record saved"
    assert report["verification"]["passed"] and report["verification"]["checks"][0]["observed"] == "saw R-1"
    assert report["stats"]["llm_calls"] == 5 and report["stats"]["approvals"] == 1 and report["stats"]["tool_failures"] == 0

    run_dir = settings.RUNS_DIR + "/t1"
    saved = json.loads(open(run_dir + "/run.json", encoding="utf-8").read())
    assert saved["status"] == "succeeded" and saved["facts"]["answer"]["value"] == "42"
    assert "Independent audit: **PASSED**" in open(run_dir + "/report.md", encoding="utf-8").read()

    types = h.types()
    for expected in ("run_started", "plan_updated", "thought", "tool_started", "tool_finished", "memory_updated",
                     "approval_requested", "approval_resolved", "verification_started", "verification_finished",
                     "run_finished", "usage"):
        assert expected in types, expected
    assert types[0] == "run_started" and types[-1] == "run_finished"
    assert all(p["run_id"] == "t1" for _, p in h.events)
    json.dumps([p for _, p in h.events], default=str)  # payloads are JSON-serializable


async def test_auditor_sees_only_read_only_tools_and_fresh_context(registry, settings):
    llm = make_llm(
        worker=[reply("Go.", call("read_note"), ), reply("Done.", finish_call())],
        auditor=[verdict(True)],
    )
    h = Harness(llm, registry, settings, task="Secret task text")
    h.runner.ctx.extras["read_only"] = "sentinel"
    await h.run()
    audit = auditor_calls(llm)[0]
    tool_names = {t.name for t in audit["tools"]}
    assert tool_names == {"read_note", "submit_verdict"}
    briefing = audit["messages"][1].content
    assert "Secret task text" in briefing and "WORKER'S CLAIM" in briefing
    assert "Plan updated" not in full_text(audit["messages"])  # worker transcript is not shared
    assert h.runner.ctx.extras["read_only"] == "sentinel"  # restored after verification


async def test_tool_failure_then_alternative_tool_then_success(registry, settings):
    llm = make_llm(
        worker=[
            reply("Try the main path.", call("primary_path", n=1)),
            reply("Failed on validation; use the other path.", call("alternative_path")),
            reply("Done.", finish_call()),
        ],
        auditor=[verdict(True)],
    )
    h = Harness(llm, registry, settings)
    state = await h.run()
    assert state.status == "succeeded"
    assert state.final_report["stats"]["tool_failures"] == 1
    second_turn = worker_calls(llm)[1]["messages"]
    assert "ERROR: " in second_turn[-1].content and "Due date must be YYYY-MM-DD" in second_turn[-1].content


async def test_write_requiring_approval_approved(registry, settings, save_tool):
    llm = make_llm(worker=[reply("Save it.", call("save_record", value="v")), reply("Done.", finish_call("blocked", "stop"))])
    h = Harness(llm, registry, settings, broker=AutoBroker.approve_all())
    await h.run()
    assert save_tool.saved == [{"value": "v"}]
    request = h.of("approval_requested")[0]
    assert request["tool"] == "save_record" and request["risk"] == "write" and request["args"] == {"value": "v"}
    assert request["action"].startswith("save_record(") and request["context"]["reason"] == "Save it."
    assert "request_id" in request


async def test_approval_context_carries_the_tool_preview_and_survives_a_broken_preview(registry, settings, save_tool):
    save_tool.approval_preview = _async_returning([{"label": "Issue date", "value": "2026-09-28"}])
    llm = make_llm(worker=[reply("Save it.", call("save_record", value="v")), reply("Done.", finish_call("blocked", "stop"))])
    h = Harness(llm, registry, settings, broker=AutoBroker.approve_all())
    await h.run()
    assert h.of("approval_requested")[0]["context"]["form_values"] == [{"label": "Issue date", "value": "2026-09-28"}]

    async def broken(args, ctx):
        raise RuntimeError("page closed")

    save_tool.approval_preview = broken
    llm = make_llm(worker=[reply("Save it.", call("save_record", value="v")), reply("Done.", finish_call("blocked", "stop"))])
    h = Harness(llm, registry, settings, broker=AutoBroker.approve_all())
    await h.run()
    request = h.of("approval_requested")[0]
    assert "form_values" not in request["context"] and request["context"]["reason"] == "Save it."
    assert len(save_tool.saved) == 2  # the approval still went through


def _async_returning(value):
    async def preview(args, ctx):
        return value

    return preview


async def test_write_denied_agent_gets_denial_observation_and_tool_is_not_run(registry, settings, save_tool):
    broker = AutoBroker(lambda kind, p: {"approved": False, "note": "amount looks wrong"})
    llm = make_llm(worker=[reply("Save it.", call("save_record", value="v")),
                           reply("Denied; I stop.", finish_call("blocked", "User denied the write."))])
    h = Harness(llm, registry, settings, broker=broker)
    state = await h.run()
    assert save_tool.saved == []
    next_messages = worker_calls(llm)[1]["messages"]
    assert "User DENIED this action. Note: amount looks wrong. Do not retry the same action; adapt or ask the user." in next_messages[-1].content
    assert state.status == "needs_attention"  # blocked finish is not verified and not a success
    assert state.final_report["verification"]["ran"] is False
    assert state.final_report["stats"]["denials"] == 1
    assert not auditor_calls(llm)


async def test_approve_similar_asks_once_for_repeated_writes(registry, settings, save_tool):
    broker = AutoBroker(lambda kind, p: {"approved": True, "remember": True})
    llm = make_llm(worker=[reply("1", call("save_record", value="a")), reply("2", call("save_record", value="b")),
                           reply("3", finish_call("failed", "enough"))])
    await Harness(llm, registry, settings, broker=broker).run()
    assert len(save_tool.saved) == 2 and len(broker.requests) == 1


async def test_irreversible_asked_even_in_auto_mode(registry, settings):
    settings = settings.model_copy(update={"APPROVAL_MODE": "auto"})
    broker = AutoBroker(lambda kind, p: {"approved": False})
    llm = make_llm(worker=[reply("Save", call("save_record", value="a")), reply("Delete", call("delete_thing")),
                           reply("end", finish_call("failed", "x"))])
    await Harness(llm, registry, settings, broker=broker).run()
    assert [p["tool"] for k, p in broker.requests if k == "approval"] == ["delete_thing"]


async def test_ask_user_routed_through_broker(registry, settings):
    broker = AutoBroker(lambda kind, p: {"answer": "Holdings"})
    llm = make_llm(worker=[reply("Two matches.", call("ask_user", question="Which vendor?", options=["Ltd", "Holdings"])),
                           reply("Thanks.", finish_call("blocked", "done asking"))])
    h = Harness(llm, registry, settings, broker=broker)
    await h.run()
    assert broker.requests == [("question", {"question": "Which vendor?", "options": ["Ltd", "Holdings"]})]
    assert "User answered: Holdings" in worker_calls(llm)[1]["messages"][-1].content
    assert h.of("question_asked")[0]["question"] == "Which vendor?"
    assert h.of("question_answered")[0]["answered"] is True
    statuses_during = [s.kind for s in h.runner.state.steps]
    assert "ask_user" in statuses_during


async def test_verifier_failure_feeds_back_and_worker_fixes(registry, settings, save_tool):
    llm = make_llm(
        worker=[
            reply("Save wrong.", call("save_record", value="wrong")),
            reply("Claim done.", finish_call()),
            reply("Fix per feedback.", call("save_record", value="right")),
            reply("Claim done again.", finish_call()),
        ],
        auditor=[verdict(False, "amount was wrong"), verdict(True, "now right")],
    )
    h = Harness(llm, registry, settings)
    state = await h.run()
    assert state.status == "succeeded"
    assert [s["value"] for s in save_tool.saved] == ["wrong", "right"]
    after_fail = worker_calls(llm)[2]["messages"]
    assert "Independent verification FAILED" in after_fail[-1].content
    assert "amount was wrong" in after_fail[-1].content
    assert "VERIFIER FEEDBACK (unresolved)" in after_fail[-1].content
    assert [p["passed"] for p in h.of("verification_finished")] == [False, True]
    assert state.final_report["stats"]["tool_failures"] == 0  # a rejected finish is not a tool malfunction


async def test_verification_failing_repeatedly_ends_failed_honestly(registry, settings):
    llm = make_llm(worker=lambda m, t: reply("Claim.", finish_call()), auditor=[verdict(False, "still wrong")] * 3)
    state = await Harness(llm, registry, settings).run()
    assert state.status == "failed"
    assert len(auditor_calls(llm)) == 3
    report = state.final_report
    assert report["verification"]["passed"] is False and "still wrong" in report["verification"]["checks"][0]["observed"]
    assert "Independent verification failed" in report["summary"]


async def test_inconclusive_verification_needs_attention(registry, settings):
    def auditor_fails(messages, tools):
        raise LLMError("auditor down")

    def responder(messages, tools):
        if messages[0].content.startswith(AUDITOR_PREFIX):
            return auditor_fails(messages, tools)
        return reply("Claim.", finish_call())

    state = await Harness(ScriptedLLM(responder), registry, settings).run()
    assert state.status == "needs_attention" and state.final_report["verification"]["inconclusive"]


async def test_repetition_guard_nudges_then_escalates_to_user(registry, settings):
    broker = AutoBroker(lambda kind, p: {"answer": "stop"})
    llm = make_llm(worker=lambda m, t: reply("Again.", call("read_note", topic="same")))
    h = Harness(llm, registry, settings, broker=broker)
    state = await h.run()
    guard_events = h.of("guard_triggered")
    assert [(g["guard"], g["level"]) for g in guard_events] == [("repetition", "nudge"), ("repetition", "escalate")]
    nudged_call = worker_calls(llm)[3]["messages"]  # the call right after the 3rd identical action
    assert "repeated the same action" in nudged_call[-1].content
    assert broker.requests[0][0] == "question" and "stuck" in broker.requests[0][1]["question"]
    assert state.status == "needs_attention"


async def test_consecutive_failures_nudge_then_escalate_and_user_guidance_resumes(registry, settings):
    answers = iter(["Try the alternative path", "unused"])
    broker = AutoBroker(lambda kind, p: {"answer": next(answers)})
    counter = itertools.count()

    def worker(messages, tools):
        n = next(counter)
        if n < 4:
            return reply("fail", call("primary_path", n=n))  # distinct args so only the failure guard fires
        if n == 4:
            return reply("Use alternative.", call("alternative_path"))
        return reply("Done.", finish_call("blocked", "gave up after help"))

    llm = make_llm(worker)
    h = Harness(llm, registry, settings, broker=broker)
    state = await h.run()
    levels = [(g["guard"], g["level"]) for g in h.of("guard_triggered")]
    assert levels == [("failures", "nudge"), ("failures", "escalate")]
    assert "consecutive failures" in worker_calls(llm)[2]["messages"][-1].content  # nudge after 2nd failure
    assert "Try the alternative path" in worker_calls(llm)[4]["messages"][-1].content  # guidance injected
    assert state.status == "needs_attention"  # the scripted worker finished "blocked"


async def test_escalation_without_answer_ends_needs_attention(registry, settings):
    llm = make_llm(worker=lambda m, t: reply("fail", call("primary_path", n=next(_ids))))
    state = await Harness(llm, registry, settings, broker=AutoBroker(lambda k, p: None)).run()
    assert state.status == "needs_attention" and "No guidance" in state.final_report["summary"]


async def test_step_budget_ends_needs_attention_with_partial_report(registry, settings):
    settings = settings.model_copy(update={"MAX_STEPS": 3})
    llm = make_llm(worker=lambda m, t: reply("more", call("read_note", topic=str(next(_ids)))))
    h = Harness(llm, registry, settings)
    state = await h.run()
    assert state.status == "needs_attention"
    assert "Step budget" in state.final_report["summary"]
    assert state.final_report["stats"]["steps"] == 3
    assert h.of("guard_triggered")[-1]["guard"] == "budget"


async def test_timeout_guard(registry, settings):
    settings = settings.model_copy(update={"RUN_TIMEOUT_S": -1})
    state = await Harness(make_llm(worker=[]), registry, settings).run()
    assert state.status == "needs_attention" and "time limit" in state.final_report["summary"]


async def test_text_only_reply_is_nudged_and_forces_a_tool_call(registry, settings):
    llm = make_llm(worker=[reply("I think I should look at it."), reply("OK.", finish_call("failed", "x"))])
    await Harness(llm, registry, settings).run()
    first, second = worker_calls(llm)
    assert first["tool_choice"] == "auto" and second["tool_choice"] == "any"
    assert "contained no tool call" in second["messages"][-1].content


async def test_two_text_only_replies_in_a_row_end_needs_attention_with_the_text(registry, settings):
    llm = make_llm(worker=[reply("The answer is 42."), reply("The answer is 42, really.")])
    state = await Harness(llm, registry, settings).run()
    assert state.status == "needs_attention"
    assert "The answer is 42, really." in state.final_report["summary"]


async def test_failed_finish_ends_failed_without_verification(registry, settings):
    llm = make_llm(worker=[reply("Cannot.", finish_call("failed", "The portal is down."))])
    state = await Harness(llm, registry, settings).run()
    assert state.status == "failed" and "The portal is down." in state.final_report["summary"]
    assert not auditor_calls(llm)


async def test_llm_failure_ends_needs_attention_with_error(registry, settings):
    def boom(messages, tools):
        raise LLMError("quota exhausted")

    state = await Harness(ScriptedLLM(boom), registry, settings).run()
    assert state.status == "needs_attention" and "quota exhausted" in state.final_report["summary"]


async def test_finish_must_be_alone_and_unknown_tools_are_reported(registry, settings):
    llm = make_llm(worker=[
        reply("Rush.", call("read_note"), finish_call()),
        reply("Oops.", call("teleport")),
        reply("Fine.", finish_call("failed", "x")),
    ])
    state = await Harness(llm, registry, settings).run()
    msgs = worker_calls(llm)
    assert "must be called alone" in msgs[1]["messages"][-1].content
    assert "Unknown tool 'teleport'" in msgs[2]["messages"][-1].content
    assert state.status == "failed"


async def test_calls_after_a_failure_in_the_same_turn_are_skipped(registry, settings, save_tool):
    llm = make_llm(worker=[reply("Two at once.", call("primary_path", n=1), call("save_record", value="x")),
                           reply("end", finish_call("failed", "x"))])
    await Harness(llm, registry, settings).run()
    assert save_tool.saved == []
    messages = worker_calls(llm)[1]["messages"]
    assert [m.role for m in messages[-3:]] == ["assistant", "tool", "tool"]  # pairing preserved
    assert "Skipped" in messages[-1].content


async def test_cancel_while_waiting_for_user_ends_cancelled(registry, settings):
    broker = InteractionBroker()  # nobody ever answers
    llm = make_llm(worker=[reply("Ask.", call("ask_user", question="Which one?"))])
    h = Harness(llm, registry, settings, broker=broker)
    original = h._sink

    async def cancel_on_question(event_type, payload):
        await original(event_type, payload)
        if event_type == "question_asked":
            h.runner.cancel()

    h.runner._sink = cancel_on_question
    state = await asyncio.wait_for(h.run(), timeout=5)
    assert state.status == "cancelled"
    assert broker.pending() == []


async def test_failing_event_sink_does_not_break_the_run(registry, settings):
    async def bad_sink(event_type, payload):
        raise RuntimeError("ui crashed")

    llm = make_llm(worker=[reply("end", finish_call("failed", "x"))])
    runner = AgentRunner("t", llm, registry, settings, AutoBroker.approve_all(), run_id="t2", event_sink=bad_sink)
    assert (await runner.run()).status == "failed"


# ---------------------------------------------------------------- model failover and thought summaries


async def test_all_models_out_of_quota_ends_needs_attention_with_clear_message(registry, settings):
    from worker.llm.base import QuotaExhaustedError
    from worker.llm.failover import FailoverLLM, ModelHealth
    from worker.llm.resilient import ResilientLLM

    class Dead(ScriptedLLM):
        async def complete(self, messages, tools=None, **kwargs):
            raise QuotaExhaustedError("20/day")

    llm = ResilientLLM(FailoverLLM(["a", "b"], lambda m: Dead([]), health=ModelHealth()), min_interval_s=0.0)
    state = await Harness(llm, registry, settings).run()
    assert state.status == "needs_attention"
    summary = state.final_report["summary"]
    assert "All configured Gemini models are out of free-tier quota for today" in summary
    assert "GEMINI_MODELS" in summary and "tomorrow" in summary


async def test_thought_summary_fills_empty_thought_and_is_not_echoed_back(registry, settings):
    summary_reply = LLMResponse(text="", tool_calls=[call("read_note", topic="a")], usage=Usage(10, 5),
                                thought="Checking the note.\n" + "very long reasoning " * 40)
    llm = make_llm(worker=[summary_reply, reply("", finish_call("blocked", "stop"))])
    harness = Harness(llm, registry, settings)
    state = await harness.run()
    step = next(s for s in state.steps if s.tool == "read_note")
    assert step.thought.startswith("Checking the note.") and step.thought_is_summary and len(step.thought) <= 301
    assert harness.of("thought")[0]["text"] == step.thought
    second_turn = worker_calls(llm)[1]["messages"]
    assert all(not m.content.startswith("Checking") for m in second_turn if m.role == "assistant")


async def test_gemini_style_summary_is_condensed_for_the_dashboard(registry, settings):
    summary = ("**Reading the note**\n\n"
               "Okay, here's my take on those thoughts, tailored for an expert audience: "
               "Alright, let me see... I need the note about topic a. Then I finish.")
    llm = make_llm(worker=[LLMResponse(text="", tool_calls=[call("read_note", topic="a")], usage=Usage(10, 5), thought=summary),
                           reply("", finish_call("blocked", "stop"))])
    harness = Harness(llm, registry, settings)
    await harness.run()
    assert harness.of("thought")[0]["text"] == "Reading the note: I need the note about topic a; then I finish."


async def test_network_wait_emits_llm_waiting_and_cancel_interrupts_it(registry, settings):
    from worker.llm.base import TransientLLMError
    from worker.llm.resilient import ResilientLLM

    class Offline(ScriptedLLM):
        async def complete(self, messages, tools=None, **kwargs):
            raise TransientLLMError("getaddrinfo failed", network=True)

    async def instant(_s):
        await asyncio.sleep(0)  # yield to the event loop like a real sleep

    llm = ResilientLLM(Offline([]), min_interval_s=0.0, sleep=instant)
    harness = Harness(llm, registry, settings)
    first = asyncio.ensure_future(harness.run())
    for _ in range(50):
        await asyncio.sleep(0)
        if harness.of("llm_waiting"):
            break
    harness.runner.cancel()
    state = await asyncio.wait_for(first, 5)
    waiting = harness.of("llm_waiting")
    assert waiting and "Network unavailable, retrying the language model for up to 5 min" in waiting[0]["message"]
    assert state.status == "cancelled"


async def test_usage_event_reports_active_model(registry, settings):
    class ModelLLM(ScriptedLLM):
        active_model, model_switches = "gemini-3.6-flash", 1

    llm = ModelLLM(lambda messages, tools: reply("", finish_call("blocked", "stop")))
    harness = Harness(llm, registry, settings)
    state = await harness.run()
    assert harness.of("usage")[0]["model"] == "gemini-3.6-flash" and harness.of("usage")[0]["model_switches"] == 1
    assert state.final_report["stats"]["model"] == "gemini-3.6-flash"
