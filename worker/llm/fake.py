"""Deterministic scripted LLM for tests."""
from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from worker.llm.base import LLMClient, LLMResponse, Message, ToolSpec

Responder = Callable[[list[Message], "list[ToolSpec] | None"], LLMResponse]


class ScriptedLLM(LLMClient):
    """Returns pre-baked responses in order, or delegates to a callable. Records every call.

    ``script`` is either a list of LLMResponse (consumed in order; an exhausted script raises)
    or a callable ``(messages, tools) -> LLMResponse``.
    """

    name = "fake"

    def __init__(self, script: list[LLMResponse] | Responder):
        self._script = script
        self._index = 0
        self.calls: list[dict] = []

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.2,
        tool_choice: Literal["auto", "any", "none"] = "auto",
    ) -> LLMResponse:
        self.calls.append(
            {"messages": list(messages), "tools": tools, "temperature": temperature, "tool_choice": tool_choice}
        )
        if callable(self._script):
            return self._script(messages, tools)
        if self._index >= len(self._script):
            raise AssertionError(f"ScriptedLLM exhausted after {len(self._script)} responses")
        response = self._script[self._index]
        self._index += 1
        return response
