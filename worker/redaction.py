"""Defense in depth against secret leaks: a per-run ``Redactor`` and a ``RedactingStore`` that applies it.

The secret tools (``worker/tools/secrets.py``) are built so the model never *receives* a secret. This module covers the
other direction: if a secret shows up anyway (a page echoes the submitted password, an API returns the key, the user
typed it into the task, the model remembers it), it is replaced by ``[REDACTED:<credential>.<field>]`` before it is
stored, emitted, shown to a human approver or sent to the model.

Where it is applied (every path that leaves the process boundary of a run):

* tool results, inside ``execute_with_retry`` (so the runner and the verifier both get clean observations);
* the runner: steps (in place), events and sink payloads, approval payloads, the plan, facts, the report, and the
  complete message list right before each LLM call; the verifier does the same for its own conversation;
* ``RedactingStore``: every write to the ``RunStore`` (header, steps, facts, events, runtime, report).

It matches exact values plus simple encodings (URL, HTML, JSON-escaped, base64). It cannot catch a secret the model
transforms itself (reversed, split, spelled out) or one rendered inside an image.
"""
from __future__ import annotations

import base64
import html
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

from worker.agent.state import Fact, RunState, Step
from worker.llm.base import Message, ToolCall
from worker.store import RunStore
from worker.tools.base import ToolResult

MIN_SECRET_LEN = 4  # shorter values are not redacted: they would shred ordinary text and are guessable anyway


def _lower_hex(encoded: str) -> str:
    return re.sub(r"%[0-9A-F]{2}", lambda m: m.group().lower(), encoded)


def _variants(value: str) -> set[str]:
    """The value and the common ways it shows up in pages, URLs, JSON and headers."""
    raw = value.encode("utf-8")
    forms = {
        value, value.strip(),
        quote(value, safe=""), quote(value), quote_plus(value),
        html.escape(value), html.escape(value, quote=False),
        json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(value)[1:-1],
        base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode(),
    }
    forms |= {_lower_hex(f) for f in forms}
    return {f for f in forms if len(f) >= MIN_SECRET_LEN}


class Redactor:
    """Replaces known secret values (and their simple encodings) with ``[REDACTED:<label>]``.

    ``secrets`` maps a label such as ``"erp.password"`` to the value. Replacement is one regex pass over the text,
    longest match first, so a marker is never re-scanned and overlapping secrets cannot corrupt each other."""

    def __init__(self, secrets: Mapping[str, str]):
        labels: dict[str, str] = {}
        for label, value in secrets.items():
            if len(str(value).strip()) < MIN_SECRET_LEN:
                continue
            for form in _variants(str(value)):
                labels.setdefault(form, label)
        self._labels = labels
        ordered = sorted(labels, key=len, reverse=True)
        self._pattern = re.compile("|".join(re.escape(form) for form in ordered)) if ordered else None

    @classmethod
    def from_scope(cls, scope: Any) -> Redactor:
        """A redactor for every secret of a ``VaultScope``."""
        return cls(scope.secret_values())

    def __bool__(self) -> bool:
        return self._pattern is not None

    # ---- text and structures
    def redact(self, text: str) -> str:
        if not text or self._pattern is None:
            return text
        return self._pattern.sub(lambda m: f"[REDACTED:{self._labels[m.group()]}]", text)

    def redact_obj(self, obj: Any) -> Any:
        """``obj`` with every string inside (dict values, list items) redacted; other values are returned as they are."""
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {key: self.redact_obj(value) for key, value in obj.items()}
        if isinstance(obj, list):
            return [self.redact_obj(item) for item in obj]
        if isinstance(obj, tuple):
            return tuple(self.redact_obj(item) for item in obj)
        return obj

    # ---- domain objects
    def redact_messages(self, messages: Iterable[Message]) -> list[Message]:
        """Copies of ``messages`` that are safe to send to a model."""
        return [
            replace(m, content=self.redact(m.content),
                    tool_calls=[ToolCall(id=c.id, name=c.name, args=self.redact_obj(c.args)) for c in m.tool_calls])
            for m in messages
        ]

    def redact_result(self, result: ToolResult) -> ToolResult:
        """A copy of a tool result with observation, error and data scrubbed."""
        return replace(result, observation=self.redact(result.observation),
                       error=self.redact(result.error) if result.error else result.error,
                       data=self.redact_obj(result.data))

    def redact_step(self, step: Step) -> Step:
        """Scrub ``step`` in place (what the model said and did, and what came back) and return it."""
        step.thought = self.redact(step.thought)
        step.args = self.redact_obj(step.args)
        step.observation = self.redact(step.observation)
        step.error = self.redact(step.error) if step.error else step.error
        return step

    def redact_fact(self, fact: Fact) -> Fact:
        fact.value, fact.source = self.redact(fact.value), self.redact(fact.source)
        return fact


class RedactingStore:
    """A ``RunStore`` that scrubs everything written through it. Reads pass straight through to the inner store."""

    def __init__(self, inner: RunStore, redactor: Redactor):
        self.inner = inner
        self.redactor = redactor

    def _state(self, state: RunState, *, with_children: bool) -> RunState:
        source = state if with_children else replace(state, steps=[], facts={})
        return RunState.from_dict(self.redactor.redact_obj(source.to_dict()))

    # ---- writes (redacted)
    async def create_run(self, state: RunState) -> None:
        await self.inner.create_run(self._state(state, with_children=True))

    async def save_state(self, state: RunState) -> None:
        await self.inner.save_state(self._state(state, with_children=False))

    async def append_step(self, run_id: str, step: Step) -> None:
        await self.inner.append_step(run_id, self.redactor.redact_step(replace(step)))

    async def upsert_fact(self, run_id: str, fact: Fact) -> None:
        await self.inner.upsert_fact(run_id, self.redactor.redact_fact(replace(fact)))

    async def append_event(self, run_id: str, event_type: str, payload: dict) -> int:
        return await self.inner.append_event(run_id, event_type, self.redactor.redact_obj(payload))

    async def save_runtime(self, run_id: str, runtime: dict) -> None:
        await self.inner.save_runtime(run_id, self.redactor.redact_obj(runtime))

    async def save_report(self, run_id: str, report: dict, markdown: str) -> None:
        await self.inner.save_report(run_id, self.redactor.redact_obj(report), self.redactor.redact(markdown))

    # ---- reads and files (delegated)
    async def read_events(self, run_id: str, after_seq: int = 0) -> list[dict]:
        return await self.inner.read_events(run_id, after_seq)

    async def load_runtime(self, run_id: str) -> dict | None:
        return await self.inner.load_runtime(run_id)

    async def load_report_markdown(self, run_id: str) -> str | None:
        return await self.inner.load_report_markdown(run_id)

    async def load_state(self, run_id: str) -> RunState | None:
        return await self.inner.load_state(run_id)

    async def list_runs(self, limit: int | None = None, org_id: str | None = None) -> list[dict]:
        return await self.inner.list_runs(limit, org_id)

    def artifact_dir(self, run_id: str) -> Path:
        return self.inner.artifact_dir(run_id)

    def artifact_file(self, run_id: str, rel_path: str) -> Path | None:
        return self.inner.artifact_file(run_id, rel_path)
