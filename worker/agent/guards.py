"""Loop guards: step budget, wall-clock timeout, consecutive failures and repetition detection.

Soft problems (failure streaks, repeated actions) are handled in two stages: a one-time nudge to the
agent, then escalation to the human. Hard limits (budget, timeout) stop the run.

The wall-clock limit counts *active* time only: the runner pauses the clock while it waits for a human, and the
accumulated active time is part of the saved state, so it carries over when a run is resumed.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from worker.agent.policy import INTERNAL_TOOLS
from worker.agent.state import NON_FAULT_ERRORS, Step

REPEAT_WINDOW = 6
REPEAT_THRESHOLD = 3
TOTAL_STEP_FACTOR = 3  # bookkeeping steps count against a looser overall cap


@dataclass
class GuardEvent:
    """A guard firing: ``level`` says what the runner must do (nudge the agent, ask the user, or stop)."""

    guard: Literal["failures", "repetition", "budget", "timeout"]
    level: Literal["nudge", "escalate", "stop"]
    message: str


def _signature(step: Step) -> str:
    """Stable fingerprint of a call: tool name plus case/whitespace-normalised args."""
    args = re.sub(r"\s+", " ", json.dumps(step.args, sort_keys=True, default=str)).lower()
    return f"{step.tool}:{args}"


def _digest(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()


class Guards:
    """Tracks progress signals across steps and reports when the agent needs a nudge, help or a stop."""

    def __init__(
        self,
        max_steps: int,
        max_consecutive_failures: int,
        timeout_s: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_steps = max_steps
        self.hard_failures = max_consecutive_failures
        self.soft_failures = min(self.hard_failures, max(2, math.ceil(self.hard_failures / 2)))
        self.timeout_s = timeout_s
        self._clock = clock
        self._active_s = 0.0  # active time accumulated before the current stretch
        self._since: float | None = clock()  # start of the current stretch; None while paused
        self._failures = 0
        self._failure_nudged = False
        self._repeat_nudged = False
        self._recent: deque[tuple[str, str]] = deque(maxlen=REPEAT_WINDOW)

    @property
    def active_seconds(self) -> float:
        """Run time so far, excluding paused stretches (waiting for a human)."""
        return self._active_s + (0.0 if self._since is None else self._clock() - self._since)

    def pause_clock(self) -> None:
        """Stop counting wall-clock time (the run is waiting for a human)."""
        if self._since is not None:
            self._active_s, self._since = self.active_seconds, None

    def resume_clock(self) -> None:
        """Start counting wall-clock time again."""
        if self._since is None:
            self._since = self._clock()

    def to_dict(self) -> dict:
        """Serializable counters, for resuming a run. The clock itself is not saved, only the active time."""
        return {
            "active_seconds": self.active_seconds,
            "failures": self._failures,
            "failure_nudged": self._failure_nudged,
            "repeat_nudged": self._repeat_nudged,
            "recent": [list(item) for item in self._recent],
        }

    @classmethod
    def from_dict(
        cls,
        data: dict,
        max_steps: int,
        max_consecutive_failures: int,
        timeout_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> Guards:
        """Rebuild guards from ``to_dict`` output; limits come from the current settings. The clock is running."""
        guards = cls(max_steps, max_consecutive_failures, timeout_s, clock)
        guards._active_s = float(data.get("active_seconds", 0.0))
        guards._failures = int(data.get("failures", 0))
        guards._failure_nudged = bool(data.get("failure_nudged", False))
        guards._repeat_nudged = bool(data.get("repeat_nudged", False))
        guards._recent.extend((sig, digest) for sig, digest in data.get("recent", []))
        return guards

    def record(self, step: Step) -> None:
        """Feed a finished tool step. Denials, skipped calls and rejected finishes are not malfunctions, so they don't count as failures."""
        if step.tool == "ask_user":
            return
        if step.ok:
            self._failures = 0
            self._failure_nudged = False
        elif step.error not in NON_FAULT_ERRORS:
            self._failures += 1
        if step.error == "malformed_call":  # the model's glitch, not a repeated action: only the failure streak counts it
            return
        self._recent.append((_signature(step), _digest(step.observation)))

    def check_limits(self, work_steps: int, total_steps: int) -> GuardEvent | None:
        """Hard stops: step budget and wall-clock timeout."""
        if self.active_seconds > self.timeout_s:
            return GuardEvent("timeout", "stop", f"Run exceeded the time limit of {self.timeout_s:.0f}s.")
        if work_steps >= self.max_steps or total_steps >= self.max_steps * TOTAL_STEP_FACTOR:
            return GuardEvent("budget", "stop", f"Step budget of {self.max_steps} steps exhausted.")
        return None

    def check_progress(self) -> GuardEvent | None:
        """Detect failure streaks and repeated identical actions that change nothing."""
        if self._failures >= self.hard_failures:
            return GuardEvent(
                "failures", "escalate", f"{self._failures} consecutive tool failures; the agent is stuck."
            )
        if self._failures >= self.soft_failures and not self._failure_nudged:
            self._failure_nudged = True
            return GuardEvent(
                "failures",
                "nudge",
                f"{self._failures} consecutive failures. Step back: re-read the current page/state, check the "
                "error messages for what is actually wrong, and consider a different approach (another tool, "
                "or an API/HTTP path if the UI is blocked). Do not repeat the same action unchanged.",
            )
        counts: dict[tuple[str, str], int] = {}
        for item in self._recent:
            counts[item] = counts.get(item, 0) + 1
        if any(n >= REPEAT_THRESHOLD for n in counts.values()):
            self._recent.clear()
            if not self._repeat_nudged:
                self._repeat_nudged = True
                return GuardEvent(
                    "repetition",
                    "nudge",
                    f"You repeated the same action {REPEAT_THRESHOLD} times and the result did not change. "
                    "Something is not working or you are not making progress: change your approach.",
                )
            return GuardEvent("repetition", "escalate", "The agent keeps repeating the same action without progress.")
        return None

    def reset_after_guidance(self) -> None:
        """Forget failure/repetition history once the human has weighed in."""
        self._failures = 0
        self._failure_nudged = False
        self._repeat_nudged = False
        self._recent.clear()


def counts_toward_budget(step: Step) -> bool:
    """Steps that act on the outside world use the step budget; bookkeeping tools do not."""
    return step.kind == "tool" and step.tool not in INTERNAL_TOOLS
