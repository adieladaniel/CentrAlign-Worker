"""Scenario files (``evals/scenarios/*.yaml``): loading, validation and the scripted human that answers the agent.

A scenario file has: ``id``, ``title``, ``task`` (plain language, as a user would say it), ``chaos`` (sim flags),
``approval`` (``approve_all`` | ``deny_writes`` | ``{script: [...], default: ...}``), ``answers`` (replies to
``ask_user`` matched by keyword, plus a default), ``expect`` (``checker`` name + ``params``) and ``tags``.
``worker/server.py`` reads the same files for the dashboard presets and only uses id, title, task and chaos.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from company_sim.chaos import FLAGS
from evals.checkers import CHECKERS

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
APPROVAL_POLICIES = ("approve_all", "deny_writes")
DEFAULT_ANSWER = "Please use your best judgement and proceed."
STUCK_ANSWER = "Keep trying, with a different approach."
DENY_NOTE = "Denied: this run is not allowed to change business systems."


class ScenarioError(ValueError):
    """A scenario file is malformed."""


@dataclass
class Scenario:
    id: str
    title: str
    task: str
    chaos: list[str] = field(default_factory=list)
    approval: Any = "approve_all"
    answers: dict = field(default_factory=dict)
    checker: str = ""
    params: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    path: str = ""


def parse_scenario(data: Any, path: Path | str = "<memory>") -> Scenario:
    """Validate raw YAML data and build a Scenario; raises ScenarioError with every problem found."""
    errors: list[str] = []
    if not isinstance(data, dict):
        raise ScenarioError(f"{path}: top level must be a mapping")

    def need(cond: bool, message: str) -> None:
        if not cond:
            errors.append(message)

    sid = data.get("id")
    need(isinstance(sid, str) and bool(ID_RE.match(sid)), "id must be a lower_snake_case string")
    for key in ("title", "task"):
        need(isinstance(data.get(key), str) and bool(data[key].strip()), f"{key} must be a non-empty string")
    chaos = data.get("chaos", [])
    need(isinstance(chaos, list) and all(isinstance(c, str) for c in chaos), "chaos must be a list of flag names")
    if isinstance(chaos, list):
        unknown = sorted(set(map(str, chaos)) - set(FLAGS))
        need(not unknown, f"unknown chaos flag(s): {unknown}; valid: {sorted(FLAGS)}")

    approval = data.get("approval", "approve_all")
    if isinstance(approval, str):
        need(approval in APPROVAL_POLICIES, f"approval must be one of {APPROVAL_POLICIES} or a script mapping")
    elif isinstance(approval, dict):
        need(isinstance(approval.get("script"), list) and len(approval["script"]) > 0, "approval script must be a non-empty list")
    else:
        errors.append("approval must be a string or a mapping with a script")

    answers = data.get("answers") or {}
    need(isinstance(answers, dict), "answers must be a mapping")
    if isinstance(answers, dict):
        need(isinstance(answers.get("default", DEFAULT_ANSWER), str), "answers.default must be a string")
        rules = answers.get("rules", [])
        need(isinstance(rules, list), "answers.rules must be a list")
        for i, rule in enumerate(rules if isinstance(rules, list) else []):
            ok = (isinstance(rule, dict) and isinstance(rule.get("keywords"), list) and bool(rule["keywords"])
                  and isinstance(rule.get("reply"), str))
            need(ok, f"answers.rules[{i}] needs a non-empty keywords list and a reply string")

    expect = data.get("expect")
    need(isinstance(expect, dict), "expect must be a mapping with checker and params")
    checker, params = "", {}
    if isinstance(expect, dict):
        checker, params = str(expect.get("checker") or ""), expect.get("params") or {}
        need(checker in CHECKERS, f"expect.checker '{checker}' is unknown; valid: {sorted(CHECKERS)}")
        need(isinstance(params, dict), "expect.params must be a mapping")
    tags = data.get("tags", [])
    need(isinstance(tags, list) and all(isinstance(t, str) for t in tags), "tags must be a list of strings")

    if errors:
        raise ScenarioError(f"{path}: " + "; ".join(errors))
    return Scenario(
        id=sid, title=data["title"].strip(), task=data["task"].strip(), chaos=[str(c) for c in chaos],
        approval=approval, answers=dict(answers), checker=checker, params=dict(params), tags=list(tags), path=str(path),
    )


def load_scenarios(directory: Path | str = SCENARIOS_DIR) -> list[Scenario]:
    """Load and validate every ``*.yaml`` in ``directory`` (sorted by file name); ids must be unique."""
    scenarios: list[Scenario] = []
    for path in sorted(Path(directory).glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as err:
            raise ScenarioError(f"{path}: invalid YAML: {err}") from err
        scenarios.append(parse_scenario(data, path))
    ids = [s.id for s in scenarios]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ScenarioError(f"duplicate scenario id(s): {dupes}")
    return scenarios


class ScriptedHuman:
    """The ``AutoBroker`` policy of a scenario: answers approvals and questions without a person.

    Approvals: ``approve_all`` approves everything; ``deny_writes`` denies every approval request (the gate only
    asks for writes and irreversible actions); a script is consumed one entry per request (``true``/``false``,
    ``approve``/``deny`` or ``{approved, note}``) and then falls back to its ``default`` (approve).
    Questions: the first rule whose keyword appears in the question or its options wins, else the scenario default.
    Everything asked is recorded for the results.
    """

    def __init__(self, scenario: Scenario):
        self.scenario = scenario
        self.approvals: list[dict] = []
        self.questions: list[dict] = []
        self._script = list(scenario.approval["script"]) if isinstance(scenario.approval, dict) else []
        self._rules = (scenario.answers or {}).get("rules") or []
        self._default = (scenario.answers or {}).get("default", DEFAULT_ANSWER)

    def __call__(self, kind: str, payload: dict) -> dict:
        return self._approval(payload) if kind == "approval" else self._answer(payload)

    def _approval(self, payload: dict) -> dict:
        policy = self.scenario.approval
        if policy == "deny_writes":
            decision: dict = {"approved": False, "note": DENY_NOTE}
        elif isinstance(policy, dict):
            raw = self._script.pop(0) if self._script else policy.get("default", True)
            decision = raw if isinstance(raw, dict) else {"approved": raw is True or str(raw).lower() in ("approve", "approved", "yes")}
            if not decision["approved"]:
                decision.setdefault("note", DENY_NOTE)
        else:
            decision = {"approved": True}
        self.approvals.append({"action": str(payload.get("action", "")), "approved": bool(decision["approved"])})
        return decision

    def _answer(self, payload: dict) -> dict:
        question = str(payload.get("question", ""))
        text = (question + " " + " ".join(map(str, payload.get("options") or []))).lower()
        reply = next((r["reply"] for r in self._rules if any(str(k).lower() in text for k in r["keywords"])), None)
        if reply is None:
            reply = STUCK_ANSWER if "i'm stuck" in text else self._default
        self.questions.append({"question": question, "answer": reply})
        return {"answer": reply}
