"""Explicit, JSON-serializable run state. Persisted through a ``RunStore`` (see ``worker/store.py``) after every step."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Literal

PlanStatus = Literal["pending", "in_progress", "done", "blocked", "skipped"]
StepKind = Literal["llm", "tool", "approval", "ask_user", "verify", "system"]
RunStatus = Literal[
    "pending", "running", "waiting_approval", "waiting_user", "verifying",
    "succeeded", "failed", "needs_attention", "cancelled",
]
TERMINAL_STATUSES = ("succeeded", "failed", "needs_attention", "cancelled")
PLAN_STATUSES = ("pending", "in_progress", "done", "blocked", "skipped")
STORED_OBSERVATION_LIMIT = 8000
# Step errors that are outcomes of policy/flow, not tool malfunctions.
NON_FAULT_ERRORS = ("denied", "skipped", "verification_failed")


def utcnow() -> str:
    """Current UTC time as an ISO-8601 string (seconds precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def truncate(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit`` characters, noting how much was cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


def _build(cls: type, data: dict) -> Any:
    """Construct dataclass ``cls`` from ``data``, ignoring unknown keys (older or newer stored versions)."""
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class PlanItem:
    """One checklist entry of the agent's plan."""

    id: int
    text: str
    status: PlanStatus = "pending"
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> PlanItem:
        """Inverse of ``asdict``."""
        return _build(cls, data)


PLAN_MARKS = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]", "blocked": "[!]", "skipped": "[-]"}


def render_plan(plan: list[PlanItem]) -> str:
    """Checklist text for prompts and tool observations."""
    if not plan:
        return "(no plan yet)"
    return "\n".join(
        f"{PLAN_MARKS.get(item.status, '[ ]')} {item.id}. {item.text}" + (f"  -- {item.note}" if item.note else "")
        for item in plan
    )


@dataclass
class Fact:
    """A discovered piece of information with provenance (where it came from, at which step)."""

    key: str
    value: str
    source: str = ""
    step: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> Fact:
        """Inverse of ``asdict``."""
        return _build(cls, data)


@dataclass
class Step:
    """One recorded unit of work: a tool call, an approval, a question, a verifier action or a system event."""

    index: int = 0
    kind: StepKind = "tool"
    thought: str = ""
    thought_is_summary: bool = False  # thought is a provider reasoning summary (display only, not echoed to the model)
    tool: str = ""
    args: dict = field(default_factory=dict)
    risk: str = "read"
    ok: bool = True
    observation: str = ""
    error: str | None = None
    evidence: list[str] = field(default_factory=list)
    duration_ms: int = 0
    retries: int = 0
    timestamp: str = field(default_factory=utcnow)
    turn: int = 0  # which LLM reply produced this step; steps of one turn share an assistant message
    tool_call_id: str = ""  # links the step to the model's tool call (needed to rebuild the transcript)

    @classmethod
    def from_dict(cls, data: dict) -> Step:
        """Inverse of ``asdict``."""
        return _build(cls, data)


@dataclass
class RunState:
    """Everything known about a run; the single source the context builder and the UI read from."""

    run_id: str
    task: str
    status: RunStatus = "pending"
    goal_summary: str = ""
    success_criteria: list[str] = field(default_factory=list)
    plan: list[PlanItem] = field(default_factory=list)
    facts: dict[str, Fact] = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)
    final_report: dict | None = None
    usage: dict = field(default_factory=lambda: {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0})
    started_at: str = field(default_factory=utcnow)
    finished_at: str | None = None

    def add_step(self, step: Step) -> Step:
        """Append ``step`` and assign its 1-based index."""
        step.index = len(self.steps) + 1
        self.steps.append(step)
        return step

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form, safe for ``json.dumps``."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> RunState:
        """Inverse of ``to_dict``; tolerant of missing optional fields so older stored runs still load."""
        state = _build(cls, {k: v for k, v in data.items() if k not in ("plan", "facts", "steps")})
        state.plan = [PlanItem.from_dict(p) for p in data.get("plan") or []]
        state.facts = {key: Fact.from_dict(f) for key, f in (data.get("facts") or {}).items()}
        state.steps = [Step.from_dict(s) for s in data.get("steps") or []]
        state.usage = {**{"llm_calls": 0, "input_tokens": 0, "output_tokens": 0}, **(data.get("usage") or {})}
        return state
