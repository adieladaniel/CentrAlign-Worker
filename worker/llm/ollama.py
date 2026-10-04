"""Ollama adapter (local fallback) using the /api/chat endpoint with tools."""
from __future__ import annotations

import json
import uuid
from typing import Literal

import httpx

from worker.llm.base import (
    LLMClient,
    LLMError,
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
    TransientLLMError,
    Usage,
    is_network_error,
)


def to_ollama_messages(messages: list[Message]) -> list[dict]:
    """Convert neutral messages into Ollama chat messages."""
    out: list[dict] = []
    for msg in messages:
        item: dict = {"role": msg.role, "content": msg.content}
        if msg.role == "assistant" and msg.tool_calls:
            item["tool_calls"] = [{"function": {"name": c.name, "arguments": c.args}} for c in msg.tool_calls]
        if msg.role == "tool" and msg.name:
            item["tool_name"] = msg.name
        out.append(item)
    return out


def to_ollama_tools(tools: list[ToolSpec]) -> list[dict]:
    """Convert ToolSpecs to Ollama's OpenAI-style tool list."""
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
        for t in tools
    ]


def parse_ollama_response(data: dict) -> LLMResponse:
    """Convert an /api/chat (non-streaming) JSON reply into an LLMResponse."""
    message = data.get("message") or {}
    calls: list[ToolCall] = []
    for raw in message.get("tool_calls") or []:
        fn = raw.get("function", {})
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        calls.append(ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=fn.get("name", ""), args=args))
    return LLMResponse(
        text=message.get("content") or "",
        tool_calls=calls,
        usage=Usage(input_tokens=data.get("prompt_eval_count", 0) or 0, output_tokens=data.get("eval_count", 0) or 0),
        raw=data,
    )


class OllamaClient(LLMClient):
    """LLMClient talking to a local Ollama server. ``tool_choice="any"`` is advisory (Ollama has no equivalent)."""

    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout_s: float = 180.0):
        self.model = model
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.2,
        tool_choice: Literal["auto", "any", "none"] = "auto",
    ) -> LLMResponse:
        payload: dict = {
            "model": self.model,
            "messages": to_ollama_messages(messages),
            "stream": False,
            "options": {"temperature": temperature},
        }
        if tools and tool_choice != "none":
            payload["tools"] = to_ollama_tools(tools)
        try:
            resp = await self._http.post("/api/chat", json=payload)
        except httpx.TransportError as err:
            raise TransientLLMError(f"Ollama unreachable: {err}", network=is_network_error(err)) from err
        if resp.status_code >= 500:
            raise TransientLLMError(f"Ollama server error {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise LLMError(f"Ollama error {resp.status_code}: {resp.text[:200]}")
        return parse_ollama_response(resp.json())
