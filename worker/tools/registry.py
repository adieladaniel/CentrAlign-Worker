"""Registry of available tools."""
from __future__ import annotations

from worker.llm.base import ToolSpec
from worker.tools.base import Tool


class ToolRegistry:
    """Name-indexed collection of tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        """Add a tool; duplicate names are rejected."""
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        """Return the tool called ``name`` or None."""
        return self._tools.get(name)

    def names(self) -> list[str]:
        """Registered tool names in registration order."""
        return list(self._tools)

    def specs(self, names: list[str] | None = None) -> list[ToolSpec]:
        """Model-facing specs for all tools, or only for ``names`` (unknown names are ignored)."""
        selected = self._tools.values() if names is None else [self._tools[n] for n in names if n in self._tools]
        return [t.spec() for t in selected]

    def subset(self, read_only: bool = True) -> ToolRegistry:
        """A new registry with only ``read_only_safe`` (or ``verifier_allowed``) tools, or a full copy when ``read_only``
        is False."""
        sub = ToolRegistry()
        for tool in self._tools.values():
            if tool.read_only_safe or tool.verifier_allowed or not read_only:
                sub.register(tool)
        return sub

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)
