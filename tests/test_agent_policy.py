"""Tests for the approval policy and the interaction broker."""
from __future__ import annotations

import asyncio

import pytest

from worker.agent.broker import AutoBroker, InteractionBroker
from worker.agent.policy import ApprovalPolicy
from worker.tools.base import Risk, Tool, ToolResult
from worker.tools.memory_tools import RememberTool


def make_tool(name: str, risk: Risk = Risk.READ, read_only_safe: bool = True) -> Tool:
    class _Tool(Tool):
        parameters = {"type": "object", "properties": {}}
        description = "test"

        async def run(self, args, ctx):
            return ToolResult(True, "ok")

    _Tool.name, _Tool.risk, _Tool.read_only_safe = name, risk, read_only_safe
    return _Tool()


# (mode, risk) -> needs approval, for a normal (non-internal) read-only-safe tool
MATRIX = [
    ("auto", Risk.READ, False),
    ("auto", Risk.WRITE, False),
    ("auto", Risk.IRREVERSIBLE, True),
    ("confirm_writes", Risk.READ, False),
    ("confirm_writes", Risk.WRITE, True),
    ("confirm_writes", Risk.IRREVERSIBLE, True),
    ("confirm_all", Risk.READ, False),
    ("confirm_all", Risk.WRITE, True),
    ("confirm_all", Risk.IRREVERSIBLE, True),
]


@pytest.mark.parametrize("mode,risk,expected", MATRIX)
def test_mode_by_risk_matrix(mode, risk, expected):
    tool = make_tool("some_tool", risk)
    assert ApprovalPolicy(mode).needs_approval(tool, {}, risk) is expected


def test_default_mode_is_confirm_writes():
    assert ApprovalPolicy().mode == "confirm_writes"


@pytest.mark.parametrize("mode", ["auto", "confirm_writes", "confirm_all"])
def test_internal_tools_never_need_approval(mode):
    policy = ApprovalPolicy(mode)
    assert not policy.needs_approval(RememberTool(), {}, Risk.READ)
    # even if an internal tool were misclassified, bookkeeping never blocks
    assert not policy.needs_approval(make_tool("update_plan", Risk.WRITE, read_only_safe=False), {}, Risk.WRITE)


def test_confirm_all_also_gates_reads_that_are_not_read_only_safe():
    tool = make_tool("type_text", Risk.READ, read_only_safe=False)
    assert ApprovalPolicy("confirm_all").needs_approval(tool, {}, Risk.READ)
    assert not ApprovalPolicy("confirm_writes").needs_approval(tool, {}, Risk.READ)


def test_approve_similar_is_cached_per_tool_and_risk():
    policy = ApprovalPolicy("confirm_writes")
    save, other = make_tool("save", Risk.WRITE), make_tool("other", Risk.WRITE)
    assert policy.needs_approval(save, {}, Risk.WRITE)
    policy.record_response(save, Risk.WRITE, {"approved": True, "remember": True})
    assert not policy.needs_approval(save, {"x": 1}, Risk.WRITE)
    assert policy.needs_approval(other, {}, Risk.WRITE)  # a different tool is still gated


def test_plain_approval_or_denial_is_not_cached():
    policy = ApprovalPolicy("confirm_writes")
    save = make_tool("save", Risk.WRITE)
    policy.record_response(save, Risk.WRITE, {"approved": True})
    policy.record_response(save, Risk.WRITE, {"approved": False, "remember": True})
    assert policy.needs_approval(save, {}, Risk.WRITE)


def test_irreversible_calls_are_always_asked_even_after_approve_similar():
    policy = ApprovalPolicy("auto")
    delete = make_tool("delete", Risk.IRREVERSIBLE)
    policy.record_response(delete, Risk.IRREVERSIBLE, {"approved": True, "remember": True})
    assert policy.needs_approval(delete, {}, Risk.IRREVERSIBLE)


async def test_broker_resolve_roundtrip_and_announcement():
    broker = InteractionBroker()
    announced: list[str] = []

    async def on_created(request_id: str) -> None:
        announced.append(request_id)

    waiter = asyncio.create_task(broker.request("approval", {"action": "x"}, on_created=on_created))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(announced) == 1 and broker.pending()[0]["kind"] == "approval"
    assert broker.resolve(announced[0], {"approved": True})
    assert await waiter == {"approved": True}
    assert not broker.resolve(announced[0], {"approved": False})  # already resolved
    assert broker.pending() == []


async def test_broker_timeout_raises_and_cleans_up():
    broker = InteractionBroker()
    with pytest.raises(asyncio.TimeoutError):
        await broker.request("question", {"question": "?"}, timeout=0.01)
    assert broker.pending() == []


async def test_auto_broker_records_requests_and_supports_async_policies():
    async def policy(kind, payload):
        return {"answer": "42"} if kind == "question" else {"approved": False, "note": "no"}

    broker = AutoBroker(policy)
    assert await broker.request("question", {"question": "?"}) == {"answer": "42"}
    assert (await broker.request("approval", {}))["approved"] is False
    assert [k for k, _ in broker.requests] == ["question", "approval"]
    assert await AutoBroker.approve_all("yes").request("question", {}) == {"answer": "yes"}
