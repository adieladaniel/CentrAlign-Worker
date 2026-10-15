"""Working-memory tool: lets the agent record facts with provenance."""
from __future__ import annotations

from worker.tools.base import Tool, ToolContext, ToolResult


class RememberTool(Tool):
    """Store a discovered fact in the run's working memory (shown back to the agent every turn)."""

    name = "remember"
    description = (
        "Save an important fact so you do not lose it as the conversation scrolls: a value you will need later, "
        "an ID, a credential location, a decision. Always say where it came from. Saving a key again overwrites it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Short snake_case name, e.g. 'portal_login_email'."},
            "value": {"type": "string", "description": "The exact value."},
            "source": {"type": "string", "description": "Where you found it, e.g. 'read_file workspace/notes.txt'."},
        },
        "required": ["key", "value", "source"],
    }
    read_only_safe = False

    def describe_action(self, args: dict, ctx: ToolContext | None = None) -> str:
        return f"Remember {args.get('key')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        if ctx.memory is None:
            return ToolResult(ok=False, observation="Working memory is not available.", error="no_memory")
        fact = ctx.memory.remember(args["key"], args["value"], args.get("source", ""))
        if ctx.emit is not None:
            await ctx.emit("memory_updated", {"key": fact.key, "value": fact.value, "source": fact.source, "step": fact.step})
        return ToolResult(ok=True, observation=f"Remembered '{fact.key}'.", data={"key": fact.key})
