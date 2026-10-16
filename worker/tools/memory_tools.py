"""Memory tools: ``remember`` (this run's working memory, optionally a candidate for the organization's long-term memory)
and ``recall`` (search that long-term memory).

Long-term memory lives behind ``ctx.extras["org_memory"]``, an object with ``async recall(query, *, limit, scope)``
(``worker.memory_store.OrgMemoryStore`` in database mode), so these tools do not know about storage. Without it
(file mode, or an organization that turned memory off) ``recall`` says so and a durable fact stays in this run only.
"""
from __future__ import annotations

from worker.agent.context import KNOWLEDGE_HEADER, render_knowledge
from worker.tools.base import Tool, ToolContext, ToolResult

NOT_ENABLED = "Organization memory is not enabled."
DEFAULT_RECALL_LIMIT = 5


class RememberTool(Tool):
    """Store a discovered fact in the run's working memory (shown back to the agent every turn)."""

    name = "remember"
    description = (
        "Save an important fact so you do not lose it as the conversation scrolls: a value you will need later, "
        "an ID, a credential location, a decision. Always say where it came from. Saving a key again overwrites it. "
        "Set durable=true only for reusable how-to knowledge about the systems you work in (for example 'the ERP "
        "new-bill form needs the date as YYYY-MM-DD'), never for task data such as amounts, names, IDs or dates; a "
        "durable fact is shared with your organization's future runs, and only if this run passes independent "
        "verification. Never put secrets in it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Short snake_case name, e.g. 'portal_login_email'."},
            "value": {"type": "string", "description": "The exact value."},
            "source": {"type": "string", "description": "Where you found it, e.g. 'read_file workspace/notes.txt'."},
            "scope": {"type": "string", "description": "Optional: the system a durable fact concerns, e.g. 'erp' or 'portal'."},
            "durable": {"type": "boolean", "description": "Default false. True: reusable how-to knowledge for the organization."},
        },
        "required": ["key", "value", "source"],
    }
    read_only_safe = False

    def describe_action(self, args: dict, ctx: ToolContext | None = None) -> str:
        return f"Remember {args.get('key')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        if ctx.memory is None:
            return ToolResult(ok=False, observation="Working memory is not available.", error="no_memory")
        durable = args.get("durable") in (True, "true", "True")
        fact = ctx.memory.remember(args["key"], args["value"], args.get("source", ""), durable=durable,
                                   scope=str(args.get("scope") or ""))
        if ctx.emit is not None:
            await ctx.emit("memory_updated", {"key": fact.key, "value": fact.value, "source": fact.source, "step": fact.step,
                                              "durable": fact.durable, "scope": fact.scope})
        note = ""
        if durable:
            note = (" It is a candidate for organization memory (saved only if this run is verified)."
                    if ctx.extras.get("org_memory") is not None
                    else " Organization memory is not enabled, so it stays in this run only.")
        return ToolResult(ok=True, observation=f"Remembered '{fact.key}'.{note}", data={"key": fact.key})


class RecallTool(Tool):
    """Search the organization's long-term memory (knowledge saved by past verified runs)."""

    name = "recall"
    description = (
        "Search your organization's long-term memory: how-to knowledge about its systems, saved by earlier runs that "
        "were independently verified. Use it when you are about to work in an unfamiliar form or system, or got stuck. "
        "Keyword search (not semantic): use the words a note would contain. Treat results as hints and check them "
        "against what you see."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords, e.g. 'erp bill date format'."},
            "scope": {"type": "string", "description": "Optional: only notes about this system, e.g. 'erp'."},
        },
        "required": ["query"],
    }

    def describe_action(self, args: dict, ctx: ToolContext | None = None) -> str:
        return f"Recall organization memory: {args.get('query')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        memory = ctx.extras.get("org_memory")
        if memory is None:
            return ToolResult(ok=True, observation=NOT_ENABLED, data={"enabled": False, "items": []})
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, observation="Give some keywords to search for.", error="empty_query")
        limit = int(ctx.extras.get("org_memory_limit") or DEFAULT_RECALL_LIMIT)
        items = await memory.recall(query, limit=limit, scope=str(args.get("scope") or "").strip() or None)
        if not items:
            return ToolResult(ok=True, observation=f"Nothing in organization memory matches '{query}'.",
                              data={"enabled": True, "items": []})
        text = f"{KNOWLEDGE_HEADER}\n{render_knowledge(items)}"
        return ToolResult(ok=True, observation=text, data={"enabled": True, "items": items})
