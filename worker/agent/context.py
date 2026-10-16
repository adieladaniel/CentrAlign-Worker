"""Builds the LLM message list each turn from run state instead of an ever-growing chat history.

Layout: system prompt, one user message (task, environment, compressed older history), then the last few turns
verbatim as properly paired assistant tool-call / tool-result messages. A state block (plan, facts, nudges,
verifier feedback) is appended to the newest message so the model always sees where it stands.
"""
from __future__ import annotations

import json
from datetime import date
from itertools import groupby

from worker.agent.memory import render_facts
from worker.agent.prompts import SYSTEM_PROMPT
from worker.agent.state import NON_FAULT_ERRORS, RunState, Step, render_plan, truncate
from worker.config import Settings
from worker.llm.base import Message, ToolCall

WINDOW_STEPS = 6  # trailing steps kept verbatim
RECENT_OBSERVATION_LIMIT = 2000  # observations inside the window other than the latest turn's
HISTORY_LINE_LIMIT = 150
HISTORY_MAX_LINES = 60


def _one_line(text: str, limit: int) -> str:
    return truncate(" ".join(text.split()), limit)


def compress_step(step: Step) -> str:
    """One-line summary of a step for the "History so far" block."""
    args = _one_line(json.dumps(step.args, ensure_ascii=False, default=str), 70)
    result = "OK" if step.ok else "ERROR"
    detail = step.error if (not step.ok and step.error and step.error not in NON_FAULT_ERRORS) else step.observation
    return f"#{step.index} {step.tool} {args} -> {result}: {_one_line(detail, HISTORY_LINE_LIMIT)}"


def _tool_content(step: Step, limit: int | None) -> str:
    text = step.observation if limit is None else truncate(step.observation, limit)
    return text if step.ok or text.startswith("ERROR") else f"ERROR: {text}"


def _turns(steps: list[Step]) -> list[list[Step]]:
    """Group transcript-eligible steps (those tied to a tool call) by the LLM turn that produced them."""
    eligible = [s for s in steps if s.tool_call_id]
    return [list(group) for _, group in groupby(eligible, key=lambda s: s.turn)]


def _split_window(turns: list[list[Step]], window: int) -> tuple[list[list[Step]], list[list[Step]]]:
    """Split into (older, recent) where recent holds whole turns covering at least ``window`` steps."""
    kept, count = 0, 0
    for turn in reversed(turns):
        if count >= window:
            break
        kept += 1
        count += len(turn)
    cut = len(turns) - kept
    return turns[:cut], turns[cut:]


def transcript_messages(turns: list[list[Step]]) -> list[Message]:
    """Assistant tool-call message + one tool message per call, per turn. The last turn is shown in full."""
    messages: list[Message] = []
    for position, turn in enumerate(turns):
        is_latest = position == len(turns) - 1
        calls = [ToolCall(id=s.tool_call_id, name=s.tool, args=s.args) for s in turn]
        said = "" if turn[0].thought_is_summary else turn[0].thought  # summaries are for humans; don't pay tokens for them
        messages.append(Message(role="assistant", content=said, tool_calls=calls))
        for step in turn:
            limit = None if is_latest else RECENT_OBSERVATION_LIMIT
            messages.append(
                Message(role="tool", content=_tool_content(step, limit), tool_call_id=step.tool_call_id, name=step.tool)
            )
    return messages


KNOWLEDGE_HEADER = "Organization knowledge (from past verified runs; verify before relying on it):"
KNOWLEDGE_CONTENT_LIMIT = 300


def _age(days: int | None) -> str:
    if days is None:
        return ""
    if days < 1:
        return "today"
    if days < 60:
        return f"{days}d old"
    return f"{days // 30}mo old"


def render_knowledge(items: list[dict]) -> str:
    """Recalled organization memories, one compact line each: ``- [scope] key: content (age)``."""
    lines = []
    for item in items:
        scope = f"[{item['scope']}] " if item.get("scope") else ""
        age = _age(item.get("age_days"))
        lines.append(f"- {scope}{item['key']}: {_one_line(item['content'], KNOWLEDGE_CONTENT_LIMIT)}" + (f" ({age})" if age else ""))
    return "\n".join(lines)


def state_block(state: RunState, nudges: list[str], feedback: str, budget: int, knowledge: list[dict] | None = None) -> str:
    """The "where you stand" block: goal, success criteria, plan, memory, organization knowledge, guard nudges, verifier
    feedback. ``knowledge``: the organization memories recalled for this run (see ``worker/memory_store.py``)."""
    work = sum(1 for s in state.steps if s.kind == "tool")
    parts = [f"=== CURRENT STATE (action {work} of at most {budget}) ==="]
    if state.goal_summary:
        parts.append(f"Goal: {state.goal_summary}")
    if state.success_criteria:
        parts.append("Success criteria:\n" + "\n".join(f"- {c}" for c in state.success_criteria))
    else:
        parts.append("Success criteria: not defined yet. Define them with update_plan.")
    parts.append("Plan:\n" + render_plan(state.plan))
    parts.append("Working memory:\n" + render_facts(state.facts))
    if knowledge:
        parts.append(f"{KNOWLEDGE_HEADER}\n{render_knowledge(knowledge)}")
    if feedback:
        parts.append("VERIFIER FEEDBACK (unresolved):\n" + feedback)
    for nudge in nudges:
        parts.append(f"NOTICE: {nudge}")
    return "\n\n".join(parts)


def vault_line(credentials: list[dict]) -> str:
    """Environment bullet naming the vault credentials (names and descriptions only, never values)."""
    names = ", ".join(c["name"] + (f" ({c['description']})" if c.get("description") else "") for c in credentials)
    return (f"- Credentials are available in the secure vault: {names}. Use list_credentials, browser_fill_secret and "
            "http_request(auth_credential=...); you never see the values.")


def _first_user_message(
    state: RunState,
    settings: Settings,
    tool_names: list[str],
    older: list[list[Step]],
    vault_credentials: list[dict] | None = None,
) -> str:
    lines = [
        f"Task from the user:\n{state.task}",
        f"Today's date: {date.today().isoformat()}",
        "Environment:\n"
        f"- Tools: {', '.join(tool_names)}\n"
        f"- Shared workspace folder (list_files/read_file/write_file): {settings.WORKSPACE_DIR}\n"
        + (f"- Company intranet start page (open this link first; it selects your organization's environment): {settings.SIM_ENTRY_URL}"
           if settings.SIM_ENTRY_URL else f"- Company intranet start page: {settings.SIM_BASE_URL}")
        + (f"\n{vault_line(vault_credentials)}" if vault_credentials else ""),
    ]
    history = [compress_step(s) for turn in older for s in turn]
    if history:
        omitted = max(0, len(history) - HISTORY_MAX_LINES)
        header = "History so far (older steps, oldest first; details are gone, rely on working memory):"
        if omitted:
            header += f" [{omitted} earliest steps omitted]"
        lines.append(header + "\n" + "\n".join(history[omitted:]))
    return "\n\n".join(lines)


def build_messages(
    state: RunState,
    settings: Settings,
    tool_names: list[str],
    *,
    nudges: list[str] | None = None,
    feedback: str = "",
    window: int = WINDOW_STEPS,
    vault_credentials: list[dict] | None = None,
    knowledge: list[dict] | None = None,
) -> list[Message]:
    """Assemble the full message list for the next LLM call. ``vault_credentials`` is the public description of the
    run's vault credentials (``VaultScope.public()``); when empty the environment section is unchanged. ``knowledge``:
    organization memories recalled for the run (shown in the state block; none = the section is absent)."""
    older, recent = _split_window(_turns(state.steps), window)
    messages = [
        Message(role="system", content=SYSTEM_PROMPT),
        Message(role="user", content=_first_user_message(state, settings, tool_names, older, vault_credentials)),
        *transcript_messages(recent),
    ]
    block = state_block(state, nudges or [], feedback, settings.MAX_STEPS, knowledge)
    messages[-1].content = f"{messages[-1].content}\n\n{block}"
    return messages
