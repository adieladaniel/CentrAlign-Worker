"""Safety policy: decides which tool calls must be approved by a human before they run."""
from __future__ import annotations

from typing import Literal

from worker.tools.base import Risk, Tool

ApprovalMode = Literal["auto", "confirm_writes", "confirm_all"]

# Bookkeeping tools that never touch the outside world.
INTERNAL_TOOLS = frozenset({"remember", "update_plan", "finish", "ask_user"})


class ApprovalPolicy:
    """Maps (tool, args, risk) to "needs approval".

    * ``auto``: only IRREVERSIBLE calls.
    * ``confirm_writes``: WRITE and IRREVERSIBLE calls.
    * ``confirm_all``: everything except internal bookkeeping tools and read-only-safe READ calls.

    "Approve similar" answers are cached per (tool, risk) for the run, except for IRREVERSIBLE calls,
    which are always asked individually.
    """

    def __init__(self, mode: ApprovalMode = "confirm_writes"):
        self.mode = mode
        self._approved_similar: set[tuple[str, Risk]] = set()

    def to_dict(self) -> dict:
        """Serializable form (mode and the "approve similar" cache), for resuming a run."""
        return {"mode": self.mode, "approved_similar": sorted([name, risk.value] for name, risk in self._approved_similar)}

    @classmethod
    def from_dict(cls, data: dict) -> ApprovalPolicy:
        """Rebuild from ``to_dict`` output."""
        policy = cls(data.get("mode", "confirm_writes"))
        policy._approved_similar = {(name, Risk(risk)) for name, risk in data.get("approved_similar", [])}
        return policy

    def needs_approval(self, tool: Tool, args: dict, risk: Risk) -> bool:
        """True when the call must be approved before it executes."""
        if tool.name in INTERNAL_TOOLS:
            return False
        if risk is not Risk.IRREVERSIBLE and (tool.name, risk) in self._approved_similar:
            return False
        if self.mode == "auto":
            return risk is Risk.IRREVERSIBLE
        if self.mode == "confirm_writes":
            return risk in (Risk.WRITE, Risk.IRREVERSIBLE)
        return not (risk is Risk.READ and tool.read_only_safe)

    def record_response(self, tool: Tool, risk: Risk, response: dict) -> None:
        """Cache an approval that carried ``remember: true`` so similar calls skip the prompt."""
        if response.get("approved") and response.get("remember") and risk is not Risk.IRREVERSIBLE:
            self._approved_similar.add((tool.name, risk))
