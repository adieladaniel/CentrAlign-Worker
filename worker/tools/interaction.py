"""Interaction tool: ask the human a clarifying question. Approvals are enforced by policy, not by a tool."""
from __future__ import annotations

from worker.tools.base import Tool, ToolContext, ToolResult


class AskUserTool(Tool):
    """Pause the run until the user answers (routed through ``ctx.extras['interact']``)."""

    name = "ask_user"
    description = (
        "Ask the human a question and wait for the answer. Use it only when information is genuinely missing or "
        "ambiguous in a way that changes the outcome (e.g. two equally plausible matches), or when continuing would "
        "be unsafe. First look in the environment (files, pages). Ask one precise question; offer options when possible."
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}, "description": "Optional suggested answers."},
        },
        "required": ["question"],
    }
    read_only_safe = False

    def describe_action(self, args: dict, ctx: ToolContext | None = None) -> str:
        return f"Ask the user: {args.get('question')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        interact = ctx.extras.get("interact")
        if interact is None:
            return ToolResult(ok=False, observation="No human is reachable to answer questions.", error="no_user")
        response = await interact("question", {"question": args["question"], "options": args.get("options") or []})
        answer = response.get("answer") if isinstance(response, dict) else response
        if answer is None or not str(answer).strip():
            return ToolResult(
                ok=False,
                observation="No answer was received. Use your best judgement, or finish with status 'blocked'.",
                error="no_answer",
            )
        return ToolResult(ok=True, observation=f"User answered: {answer}", data={"answer": str(answer)})
