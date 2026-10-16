"""Working memory: facts the agent has discovered, each with provenance."""
from __future__ import annotations

from worker.agent.state import Fact, truncate

MAX_VALUE_CHARS = 400


def render_facts(facts: dict[str, Fact]) -> str:
    """Compact text block of facts for prompts, one per line."""
    if not facts:
        return "(nothing remembered yet)"
    lines = []
    for fact in facts.values():
        origin = ", ".join(part for part in (fact.source, f"step {fact.step}" if fact.step else "") if part)
        mark = "  (durable)" if fact.durable else ""
        lines.append(f"- {fact.key}: {truncate(fact.value, MAX_VALUE_CHARS)}" + (f"  [{origin}]" if origin else "") + mark)
    return "\n".join(lines)


class WorkingMemory:
    """Key/value store of discovered facts. ``step`` is kept current by the runner for provenance."""

    def __init__(self, facts: dict[str, Fact] | None = None):
        self.facts: dict[str, Fact] = facts if facts is not None else {}
        self.step = 0

    def remember(self, key: str, value: str, source: str = "", step: int | None = None, *, durable: bool = False,
                 scope: str = "") -> Fact:
        """Store (or overwrite) a fact. ``durable`` / ``scope``: see ``Fact``."""
        fact = Fact(key=key.strip(), value=str(value), source=source, step=self.step if step is None else step,
                    durable=bool(durable), scope=scope.strip())
        self.facts[fact.key] = fact
        return fact

    def render(self) -> str:
        """Compact text block for prompts."""
        return render_facts(self.facts)
