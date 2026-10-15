"""Run persistence: the single seam through which an agent run saves everything.

``RunStore`` is an async protocol. ``AgentRunner`` and the server only talk to it, so the file layout used by the CLI,
the evals and the tests (``FileRunStore``) can be swapped for a database implementation without touching either.

A run is stored as:

* the **header** (status, plan, goal, criteria, usage, final report, timestamps) via ``create_run`` / ``save_state``;
* **steps** and **facts** as append-only rows via ``append_step`` / ``upsert_fact``;
* an **event log** with a 1-based, increasing ``seq`` per run via ``append_event`` / ``read_events``;
* the runner's hidden **runtime** state (what is needed to resume a run) via ``save_runtime`` / ``load_runtime``;
* the final **report** (dict plus markdown) via ``save_report``;
* **artifacts** (screenshots, downloads) as files in a local scratch directory, ``artifact_dir``.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from worker.agent.state import Fact, RunState, Step, utcnow

RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class RunStore(Protocol):
    """Where a run lives. All methods are keyed by ``run_id``; implementations must be safe for one writer per run."""

    async def create_run(self, state: RunState) -> None:
        """Create the run from its initial state (header, plus any steps and facts it already has)."""

    async def save_state(self, state: RunState) -> None:
        """Persist the run header: everything except steps and facts, which have their own methods."""

    async def append_step(self, run_id: str, step: Step) -> None:
        """Append a finished step. Steps are never modified afterwards."""

    async def upsert_fact(self, run_id: str, fact: Fact) -> None:
        """Insert a fact, or overwrite the one with the same key."""

    async def append_event(self, run_id: str, event_type: str, payload: dict) -> int:
        """Append an event and return its ``seq`` (1-based, increasing within the run)."""

    async def read_events(self, run_id: str, after_seq: int = 0) -> list[dict]:
        """Events ``{seq, type, ts, payload}`` with ``seq > after_seq`` in order (``after_seq <= 0`` returns all)."""

    async def save_runtime(self, run_id: str, runtime: dict) -> None:
        """Replace the runner's hidden state (see ``AgentRunner.resume``)."""

    async def load_runtime(self, run_id: str) -> dict | None:
        """The last saved runtime dict, or None."""

    async def save_report(self, run_id: str, report: dict, markdown: str) -> None:
        """Store the final report (structured and rendered)."""

    async def load_report_markdown(self, run_id: str) -> str | None:
        """The rendered report, or None while the run has none."""

    async def load_state(self, run_id: str) -> RunState | None:
        """The complete state (header, steps, facts), or None for an unknown run."""

    async def list_runs(self, limit: int | None = None, org_id: str | None = None) -> list[dict]:
        """Summaries ``{id, task, status, started_at, finished_at}``, newest first. ``org_id`` restricts the list to
        one organisation (stores that are not multi-tenant ignore it; a scoped store never lists other orgs)."""

    def artifact_dir(self, run_id: str) -> Path:
        """Local scratch directory for the run's screenshots and downloads (created if missing)."""

    def artifact_file(self, run_id: str, rel_path: str) -> Path | None:
        """A file inside the run's artifact directory, or None if it is missing or outside of it."""


def _write_json(path: Path, data: Any) -> None:
    """Write ``data`` as JSON atomically (temp file, then replace)."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class FileRunStore:
    """Stores runs under ``<root>/<run_id>/``: ``run.json``, ``events.jsonl``, ``runtime.json``, ``report.md``,
    ``screenshots/`` and ``downloads/``.

    ``run.json`` is rewritten atomically on every change (fine for files). The document of a run being written is
    cached in memory, as is its event log, until the run finishes; other runs are always read from disk.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._docs: dict[str, dict] = {}
        self._events: dict[str, list[dict]] = {}

    # ----------------------------------------------------------------- paths

    def _dir(self, run_id: str) -> Path:
        if not RUN_ID_RE.match(run_id):
            raise ValueError(f"invalid run id: {run_id!r}")
        return self.root / run_id

    def artifact_dir(self, run_id: str) -> Path:
        path = self._dir(run_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def artifact_file(self, run_id: str, rel_path: str) -> Path | None:
        root = self._dir(run_id).resolve()
        try:
            target = (root / rel_path).resolve()
        except (OSError, ValueError):
            return None
        return target if target.is_relative_to(root) and target.is_file() else None

    # ----------------------------------------------------------------- header, steps, facts

    def _doc(self, run_id: str) -> dict:
        """The run.json document being written (loaded from disk the first time, e.g. when resuming)."""
        doc = self._docs.get(run_id)
        if doc is None:
            doc = _read_json(self._dir(run_id) / "run.json") or {}
            doc.setdefault("steps", [])
            doc.setdefault("facts", {})
            self._docs[run_id] = doc
        return doc

    def _flush(self, run_id: str) -> None:
        _write_json(self._dir(run_id) / "run.json", self._docs[run_id])

    async def create_run(self, state: RunState) -> None:
        self.artifact_dir(state.run_id)
        self._docs[state.run_id] = state.to_dict()
        self._flush(state.run_id)

    async def save_state(self, state: RunState) -> None:
        doc = self._doc(state.run_id)
        doc.update({k: v for k, v in state.to_dict().items() if k not in ("steps", "facts")})
        self._flush(state.run_id)

    async def append_step(self, run_id: str, step: Step) -> None:
        self._doc(run_id)["steps"].append(asdict(step))
        self._flush(run_id)

    async def upsert_fact(self, run_id: str, fact: Fact) -> None:
        self._doc(run_id)["facts"][fact.key] = asdict(fact)
        self._flush(run_id)

    async def load_state(self, run_id: str) -> RunState | None:
        data = self._docs.get(run_id) or _read_json(self._dir(run_id) / "run.json")
        return RunState.from_dict(data) if data is not None else None

    async def list_runs(self, limit: int | None = None, org_id: str | None = None) -> list[dict]:
        rows = []
        for path in self.root.glob("*/run.json") if self.root.is_dir() else []:
            data = _read_json(path)
            if data is not None:
                rows.append({"id": data.get("run_id", path.parent.name), "task": data.get("task", ""),
                             "status": data.get("status", "unknown"), "started_at": data.get("started_at", ""),
                             "finished_at": data.get("finished_at")})
        rows.sort(key=lambda r: r["started_at"], reverse=True)
        return rows[:limit]

    # ----------------------------------------------------------------- events

    def _read_event_file(self, run_id: str) -> list[dict]:
        path = self._dir(run_id) / "events.jsonl"
        events = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return events
        for line in lines:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events

    async def append_event(self, run_id: str, event_type: str, payload: dict) -> int:
        events = self._events.get(run_id)
        if events is None:
            events = self._events[run_id] = self._read_event_file(run_id)
        seq = events[-1]["seq"] + 1 if events else 1
        line = json.dumps({"seq": seq, "type": event_type, "ts": utcnow(), "payload": payload}, ensure_ascii=False, default=str)
        self.artifact_dir(run_id)
        with (self._dir(run_id) / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        events.append(json.loads(line))  # the same normalised form a reader gets from disk
        if event_type == "run_finished":
            del self._events[run_id]
        return seq

    async def read_events(self, run_id: str, after_seq: int = 0) -> list[dict]:
        events = self._events.get(run_id)
        if events is None:
            events = self._read_event_file(run_id)
        return [e for e in events if e["seq"] > after_seq] if after_seq > 0 else list(events)

    # ----------------------------------------------------------------- runtime and report

    async def save_runtime(self, run_id: str, runtime: dict) -> None:
        _write_json(self._dir(run_id) / "runtime.json", runtime)

    async def load_runtime(self, run_id: str) -> dict | None:
        return _read_json(self._dir(run_id) / "runtime.json")

    async def save_report(self, run_id: str, report: dict, markdown: str) -> None:
        doc = self._doc(run_id)
        doc["final_report"] = report
        self._flush(run_id)
        del self._docs[run_id]  # the run is over: stop caching it
        (self._dir(run_id) / "report.md").write_text(markdown, encoding="utf-8")

    async def load_report_markdown(self, run_id: str) -> str | None:
        try:
            return (self._dir(run_id) / "report.md").read_text(encoding="utf-8")
        except OSError:
            return None
