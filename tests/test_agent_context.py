"""Tests for context building: transcript pairing, compression of old steps, and the state block."""
from __future__ import annotations

import pytest

from worker.agent.context import build_messages, compress_step
from worker.agent.state import Fact, PlanItem, RunState, Step
from worker.config import Settings

SETTINGS = Settings(_env_file=None, MAX_STEPS=40)
TOOLS = ["read_file", "update_plan"]


def make_state(turn_sizes: list[int], obs_len: int = 100) -> RunState:
    """A state with one LLM turn per entry of ``turn_sizes``, each holding that many tool steps."""
    state = RunState(run_id="r1", task="Do the thing")
    for turn, size in enumerate(turn_sizes, start=1):
        for k in range(size):
            state.add_step(
                Step(
                    thought=f"thinking {turn}",
                    tool="read_file",
                    args={"path": f"f{turn}_{k}.txt"},
                    observation=f"content of turn {turn} call {k} " + "x" * obs_len,
                    turn=turn,
                    tool_call_id=f"c{turn}_{k}",
                )
            )
    return state


def assert_pairing(messages) -> None:
    """Every assistant tool call is answered by a tool message right after it, in order; no orphans."""
    assert messages[0].role == "system" and messages[1].role == "user"
    i = 2
    while i < len(messages):
        msg = messages[i]
        assert msg.role == "assistant" and msg.tool_calls, f"unexpected {msg.role} message at {i}"
        ids = [c.id for c in msg.tool_calls]
        following = messages[i + 1 : i + 1 + len(ids)]
        assert [m.role for m in following] == ["tool"] * len(ids)
        assert [m.tool_call_id for m in following] == ids
        i += 1 + len(ids)
    assert i == len(messages)


@pytest.mark.parametrize("window", [1, 2, 6, 10])
@pytest.mark.parametrize("turn_sizes", [[], [1], [2, 1, 3], [1] * 25, [3, 1, 2, 2, 1, 4, 1, 1, 2, 3] * 3])
def test_tool_call_pairing_invariant_over_histories(turn_sizes, window):
    messages = build_messages(make_state(turn_sizes), SETTINGS, TOOLS, window=window)
    assert_pairing(messages)


def test_window_keeps_whole_turns_covering_at_least_k_steps():
    state = make_state([1, 3, 3, 1])  # last turns: 1, then 3 -> 4 steps < 6, third from end adds 3 more
    messages = build_messages(state, SETTINGS, TOOLS, window=6)
    kept = sum(len(m.tool_calls) for m in messages if m.role == "assistant")
    assert kept == 7  # turns of sizes 1+3+3: the oldest turn is only included whole
    assert_pairing(messages)


def test_old_steps_are_compressed_into_history_block():
    state = make_state([1] * 20)
    messages = build_messages(state, SETTINGS, TOOLS, window=6)
    first_user = messages[1].content
    assert "Task from the user:\nDo the thing" in first_user
    assert "History so far" in first_user
    assert "#1 read_file" in first_user and "#14 read_file" in first_user
    assert "#15 read_file" not in first_user  # steps 15-20 are in the verbatim window
    verbatim_ids = [c.id for m in messages if m.role == "assistant" for c in m.tool_calls]
    assert verbatim_ids == [f"c{t}_0" for t in range(15, 21)]
    assert "Company intranet start page" in first_user


def test_history_block_is_capped_and_says_how_much_was_omitted():
    messages = build_messages(make_state([1] * 120), SETTINGS, TOOLS, window=6)
    assert "earliest steps omitted" in messages[1].content
    assert messages[1].content.count("\n#") <= 61


def test_only_latest_turn_observations_are_shown_in_full():
    state = make_state([1, 1, 1], obs_len=5000)
    messages = build_messages(state, SETTINGS, TOOLS, window=6)
    tools = [m for m in messages if m.role == "tool"]
    assert "truncated" in tools[0].content and "truncated" in tools[1].content
    assert "truncated" not in tools[2].content and len(tools[2].content) > 5000


def test_failed_observations_are_prefixed_with_error():
    state = make_state([1])
    state.steps[0].ok = False
    state.steps[0].error = "ValueError: bad date"
    state.steps[0].observation = "Tool 'read_file' failed: ValueError: bad date"
    tool_msg = [m for m in build_messages(state, SETTINGS, TOOLS) if m.role == "tool"][0]
    assert tool_msg.content.startswith("ERROR: Tool 'read_file' failed")


def test_compress_step_one_liner():
    step = Step(index=12, tool="browser_click", args={"ref": 7}, ok=False, error="Due date must be YYYY-MM-DD",
                observation="Tool failed: Due date must be YYYY-MM-DD")
    line = compress_step(step)
    assert line.startswith("#12 browser_click") and "-> ERROR: Due date must be YYYY-MM-DD" in line
    ok = compress_step(Step(index=3, tool="read_file", args={"path": "a"}, observation="line one\nline two"))
    assert "-> OK: line one line two" in ok


def test_non_transcript_steps_never_enter_messages():
    state = make_state([2])
    state.add_step(Step(kind="approval", tool="save", observation="APPROVED: save"))
    state.add_step(Step(kind="system", thought="nudge", ok=False, error="failures"))
    state.add_step(Step(kind="verify", tool="read_file", observation="audit read"))
    state.add_step(Step(kind="llm", thought="chatty text", ok=False, error="no_tool_call", turn=2))
    messages = build_messages(state, SETTINGS, TOOLS)
    assert_pairing(messages)
    assert sum(len(m.tool_calls) for m in messages if m.role == "assistant") == 2


def test_state_block_on_latest_message_has_plan_facts_nudges_and_feedback():
    state = make_state([1, 1])
    state.goal_summary = "Get it done"
    state.success_criteria = ["record exists", "amount matches"]
    state.plan = [PlanItem(1, "Find doc", "done"), PlanItem(2, "Enter data", "in_progress", "form open"), PlanItem(3, "Check", "pending")]
    state.facts["amount"] = Fact("amount", "1234.50", "read_file doc.pdf", 1)
    messages = build_messages(state, SETTINGS, TOOLS, nudges=["Try another way."], feedback="- amount: saw 1234.00")
    last = messages[-1]
    assert last.role == "tool"
    for expected in (
        "Goal: Get it done", "- record exists", "[x] 1. Find doc", "[>] 2. Enter data  -- form open", "[ ] 3. Check",
        "- amount: 1234.50  [read_file doc.pdf, step 1]", "NOTICE: Try another way.",
        "VERIFIER FEEDBACK (unresolved):\n- amount: saw 1234.00", "of at most 40",
    ):
        assert expected in last.content, expected
    assert "CURRENT STATE" not in messages[-2].content  # only the newest message carries the block


def test_fresh_run_attaches_state_block_to_the_user_message():
    state = RunState(run_id="r", task="Task text")
    messages = build_messages(state, SETTINGS, TOOLS)
    assert [m.role for m in messages] == ["system", "user"]
    assert "Task text" in messages[1].content and "no plan yet" in messages[1].content
    assert "Success criteria: not defined yet" in messages[1].content
    assert "nothing remembered yet" in messages[1].content
