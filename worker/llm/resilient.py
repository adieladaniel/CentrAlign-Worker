"""Resilience wrapper shared by all providers: throttling, retries, accounting, tool-arg validation."""
from __future__ import annotations

import asyncio
import inspect
import random
import time
from typing import Any, Awaitable, Callable, Literal

from jsonschema import Draft202012Validator

from worker.llm.base import (
    LLMClient,
    LLMResponse,
    Message,
    RateLimitError,
    ToolCall,
    ToolSpec,
    TransientLLMError,
    Usage,
)


def validate_tool_calls(calls: list[ToolCall], tools: list[ToolSpec]) -> dict[str, str]:
    """Return ``{call_id: error}`` for calls with an unknown tool name or args violating the JSON schema."""
    specs = {t.name: t for t in tools}
    problems: dict[str, str] = {}
    for call in calls:
        spec = specs.get(call.name)
        if spec is None:
            problems[call.id] = f"Unknown tool '{call.name}'. Available tools: {', '.join(sorted(specs))}."
            continue
        errors = sorted(Draft202012Validator(spec.parameters).iter_errors(call.args), key=lambda e: list(e.path))
        if errors:
            details = "; ".join(f"{'.'.join(map(str, e.path)) or '<args>'}: {e.message}" for e in errors[:5])
            problems[call.id] = f"Invalid arguments for '{call.name}': {details}"
    return problems


class ResilientLLM(LLMClient):
    """Wraps any LLMClient with a min-interval throttle, retry/backoff, usage stats and arg validation.

    Retries RateLimitError and TransientLLMError with exponential backoff plus jitter, honoring the
    server's ``retry_after`` hint (at most ``max_retries`` times). Connectivity errors
    (``TransientLLMError.network``: DNS, refused connection, ...) are different: they are retried on a *time
    budget* (``network_retry_s``, capped backoff up to ``max_network_delay_s``) so a run survives a few minutes
    without internet. ``on_retry(info)`` (sync or async) is called before each network wait; an exception it
    raises aborts the call (the runner uses this to honor cancellation). Tool calls that name an unknown tool or violate the tool's schema
    trigger one corrective re-prompt; if the second answer is still invalid it is returned as-is.
    """

    def __init__(
        self,
        inner: LLMClient,
        *,
        min_interval_s: float = 4.0,
        max_retries: int = 5,
        base_delay_s: float = 2.0,
        max_delay_s: float = 90.0,
        network_retry_s: float = 300.0,
        max_network_delay_s: float = 30.0,
        on_retry: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.inner = inner
        self.name = inner.name
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s
        self.network_retry_s = network_retry_s
        self.max_network_delay_s = max_network_delay_s
        self.on_retry = on_retry
        self._sleep = sleep
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last_call_at: float | None = None
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.retries = 0

    @property
    def active_model(self) -> str | None:
        """The model currently serving calls, when the wrapped client reports one (failover clients do)."""
        return getattr(self.inner, "active_model", None)

    @property
    def model_switches(self) -> int:
        return getattr(self.inner, "model_switches", 0)

    @property
    def stats(self) -> dict[str, Any]:
        """Cumulative usage counters (plus ``model`` / ``model_switches`` when the wrapped client has a model chain)."""
        stats: dict[str, Any] = {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "retries": self.retries,
        }
        if self.active_model is not None:
            stats["model"] = self.active_model
            stats["model_switches"] = self.model_switches
        return stats

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.2,
        tool_choice: Literal["auto", "any", "none"] = "auto",
    ) -> LLMResponse:
        response = await self._call(messages, tools, temperature, tool_choice)
        if not tools or tool_choice == "none" or not response.tool_calls:
            return response
        problems = validate_tool_calls(response.tool_calls, tools)
        if not problems:
            return response

        repair = list(messages) + [Message(role="assistant", content=response.text, tool_calls=response.tool_calls)]
        for call in response.tool_calls:
            error = problems.get(call.id, "Not executed because another call in this turn was invalid. Re-issue it.")
            repair.append(Message(role="tool", content=f"ERROR: {error}", tool_call_id=call.id, name=call.name))
        second = await self._call(repair, tools, temperature, tool_choice)
        second.usage = Usage(
            input_tokens=response.usage.input_tokens + second.usage.input_tokens,
            output_tokens=response.usage.output_tokens + second.usage.output_tokens,
        )
        return second

    async def _call(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        temperature: float,
        tool_choice: Literal["auto", "any", "none"],
    ) -> LLMResponse:
        attempt = 0
        net_attempts = 0
        net_since: float | None = None  # when the current network outage was first seen
        while True:
            await self._throttle()
            try:
                response = await self.inner.complete(messages, tools, temperature=temperature, tool_choice=tool_choice)
            except (RateLimitError, TransientLLMError) as err:
                if isinstance(err, TransientLLMError) and err.network:
                    now = self._clock()
                    net_since = now if net_since is None else net_since
                    waited = now - net_since
                    if waited >= self.network_retry_s:
                        raise
                    delay = min(self._network_backoff(net_attempts), self.network_retry_s - waited)
                    net_attempts += 1
                    self.retries += 1
                    await self._notify_retry({
                        "kind": "network", "attempt": net_attempts, "retry_in_s": round(delay, 1),
                        "waited_s": round(waited, 1), "budget_s": self.network_retry_s, "error": str(err),
                    })
                    await self._sleep(delay)
                    continue
                if attempt >= self.max_retries:
                    raise
                self.retries += 1
                await self._sleep(self._backoff(attempt, err.retry_after))
                attempt += 1
                continue
            self.calls += 1
            self.input_tokens += response.usage.input_tokens
            self.output_tokens += response.usage.output_tokens
            return response

    async def _throttle(self) -> None:
        """Serialize calls and enforce the minimum interval between call starts."""
        async with self._lock:
            if self._last_call_at is not None:
                wait = self.min_interval_s - (self._clock() - self._last_call_at)
                if wait > 0:
                    await self._sleep(wait)
            self._last_call_at = self._clock()

    async def _notify_retry(self, info: dict[str, Any]) -> None:
        if self.on_retry is None:
            return
        result = self.on_retry(info)
        if inspect.isawaitable(result):
            await result

    def _network_backoff(self, attempt: int) -> float:
        return min(self.base_delay_s * 2 ** min(attempt, 20), self.max_network_delay_s) * random.uniform(0.5, 1.0)

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after + random.uniform(0.0, 1.0), self.max_delay_s)
        return min(self.base_delay_s * 2**attempt, self.max_delay_s) * random.uniform(0.5, 1.0)
