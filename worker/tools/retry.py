"""Exception-safe tool execution with automatic retry of transient failures."""
from __future__ import annotations

import asyncio
import re

import httpx

from worker.tools.base import Tool, ToolContext, ToolResult

BACKOFF_BASE_S = 0.5

_TRANSIENT_PATTERN = re.compile(
    r"timeout|timed out|connection (reset|refused|closed|aborted)|net::err_|detached|temporarily unavailable",
    re.IGNORECASE,
)


def is_transient_exception(exc: BaseException) -> bool:
    """Heuristic: timeouts, connection problems and 5xx responses are worth retrying."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return bool(_TRANSIENT_PATTERN.search(str(exc)))


def _retry_safe(tool: Tool, args: dict) -> bool:
    try:
        return tool.retry_safe(args)
    except Exception:  # noqa: BLE001 - when in doubt, do not retry
        return False


async def _run_once(tool: Tool, args: dict, ctx: ToolContext) -> ToolResult:
    try:
        return await tool.run(args, ctx)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - tool failures become observations for the agent
        message = f"{type(exc).__name__}: {exc}"
        return ToolResult(
            ok=False,
            observation=f"Tool '{tool.name}' failed: {message}",
            error=message,
            transient=is_transient_exception(exc),
        )


async def execute_with_retry(tool: Tool, args: dict, ctx: ToolContext) -> ToolResult:
    """Run ``tool`` and never raise. Transient failures are retried up to ``tool.max_retries`` times
    with exponential backoff; the final observation notes how many retries happened."""
    retries = 0
    result = await _run_once(tool, args, ctx)
    # State-changing calls are never retried blindly: a failure (timeout, 5xx, dropped connection) may have
    # happened after a partial write. The failure is returned as non-transient with guidance for the agent.
    if not result.ok and result.transient and not _retry_safe(tool, args):
        result.transient = False
        note = tool.no_retry_note(args)
        if note not in result.observation:
            result.observation += "\n" + note
        return result
    while not result.ok and result.transient and retries < tool.max_retries:
        await asyncio.sleep(BACKOFF_BASE_S * 2**retries)
        retries += 1
        result = await _run_once(tool, args, ctx)

    if retries:
        outcome = "succeeded" if result.ok else "still failing"
        result.observation += f"\n[{outcome} after {retries} automatic retr{'y' if retries == 1 else 'ies'}]"
    return result
