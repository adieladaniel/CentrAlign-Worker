"""Control tools: ``update_plan`` and ``finish``. The runner applies their effects (plan, final report)."""
from __future__ import annotations

from dataclasses import asdict

from worker.agent.state import PLAN_STATUSES, PlanItem, render_plan
from worker.tools.base import Tool, ToolContext, ToolResult

FINISH_STATUSES = ("success", "failed", "blocked")


class UpdatePlanTool(Tool):
    """Replace the plan checklist (and optionally record the goal and success criteria)."""

    name = "update_plan"
    description = (
        "Create or replace your plan: a short ordered checklist with a status per item. Call it on your first turn "
        "(together with goal_summary and success_criteria), whenever you finish an item, and whenever reality "
        "forces a re-plan. Always send the complete list."
    )
    parameters = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "The complete plan, in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "status": {"type": "string", "enum": list(PLAN_STATUSES)},
                        "note": {"type": "string", "description": "Optional short note, e.g. why blocked."},
                    },
                    "required": ["text", "status"],
                },
            },
            "goal_summary": {"type": "string", "description": "One sentence restating the end goal (first call)."},
            "success_criteria": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concrete, checkable conditions that mean the task is done (first call; resend to change).",
            },
        },
        "required": ["items"],
    }
    read_only_safe = False

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        plan = [
            PlanItem(
                id=i,
                text=str(item.get("text", "")).strip(),
                status=item.get("status") if item.get("status") in PLAN_STATUSES else "pending",
                note=str(item.get("note", "")),
            )
            for i, item in enumerate(args.get("items", []), start=1)
        ]
        data: dict = {"plan": [asdict(p) for p in plan]}
        if args.get("goal_summary"):
            data["goal_summary"] = str(args["goal_summary"])
        if args.get("success_criteria"):
            data["success_criteria"] = [str(c) for c in args["success_criteria"]]
        return ToolResult(ok=True, observation=f"Plan updated:\n{render_plan(plan)}", data=data)


class FinishTool(Tool):
    """Declare the task finished. The runner intercepts it: success claims are independently verified."""

    name = "finish"
    description = (
        "End the task. Call it only after YOU have checked the outcome. status 'success' triggers an independent "
        "verification of your claim; use 'failed' or 'blocked' honestly when the goal cannot be achieved. "
        "Give a short summary for a busy human, the key output values, and evidence (claim -> where you saw it). "
        "Call it alone in its turn, after you have seen the result of your last action."
    )
    parameters = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": list(FINISH_STATUSES)},
            "summary": {"type": "string", "description": "2-5 sentences: what was done, the result, anything notable."},
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "source": {"type": "string", "description": "Page URL, file, API response or step where you saw it."},
                    },
                    "required": ["claim", "source"],
                },
            },
            "outputs": {
                "type": "array",
                "description": "Key result values as name/value pairs, e.g. record IDs and extracted figures.",
                "items": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
                    "required": ["name", "value"],
                },
            },
        },
        "required": ["status", "summary"],
    }
    read_only_safe = False

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, observation="Finish recorded.", data={"finish": normalize_finish(args)})


def normalize_finish(args: dict) -> dict:
    """Coerce finish arguments into ``{status, summary, evidence: [..], outputs: {name: value}}``."""
    outputs = args.get("outputs") or []
    if isinstance(outputs, list):
        outputs = {str(o.get("name")): o.get("value") for o in outputs if isinstance(o, dict)}
    return {
        "status": args.get("status", "failed"),
        "summary": str(args.get("summary", "")),
        "evidence": [e for e in args.get("evidence") or [] if isinstance(e, dict)],
        "outputs": outputs if isinstance(outputs, dict) else {},
    }
