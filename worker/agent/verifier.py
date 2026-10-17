"""Independent verification: a fresh-context auditor re-observes the real system with read-only tools."""
from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import date

from worker.agent.prompts import (
    MALFORMED_CALL_NOTE,
    VERIFIER_MISSING_CHECKS,
    VERIFIER_NO_TOOL_NUDGE,
    VERIFIER_SYSTEM_PROMPT,
)
from worker.agent.state import truncate
from worker.llm.base import LLMClient, LLMError, MalformedCallError, Message, ToolCall
from worker.tools.base import Risk, Tool, ToolContext, ToolResult
from worker.tools.registry import ToolRegistry
from worker.tools.retry import execute_with_retry

# Raised from 12: besides one check per criterion the auditor must now also look through the lists of matching entities
# and the other sources (mailbox vs portal) for a competing candidate, which takes several reads on top of the record check.
DEFAULT_MAX_STEPS = 16
OBSERVATION_LIMIT = 3000
MAX_MALFORMED_REPLIES = 2  # consecutive broken auditor replies tolerated before the audit is called inconclusive

# The checks every verdict must carry besides the per-criterion ones: submit_verdict field -> criterion name in ``checks``.
MANDATORY_CHECKS = {"ambiguity": "Ambiguity", "completeness": "Completeness"}

StepCallback = Callable[[str, dict, ToolResult, int], Awaitable[None]]

_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "observed": {"type": "string", "description": "What you actually saw in the systems."},
    },
    "required": ["passed", "observed"],
}


class SubmitVerdictTool(Tool):
    """Sentinel tool the auditor calls to end verification."""

    name = "submit_verdict"
    description = (
        "Submit your final verdict. passed is true only if EVERY success criterion was directly observed to hold AND "
        "both mandatory checks passed. Provide one check per criterion with what you actually observed, plus the "
        "mandatory `ambiguity` check (does a name in the task match several entities with a competing candidate that "
        "the worker did not ask about?) and `completeness` check (did the worker consider every source that could hold "
        "the latest / all / requested item?)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "passed": {"type": "boolean"},
            "checks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "criterion": {"type": "string"},
                        "passed": {"type": "boolean"},
                        "observed": {"type": "string", "description": "What you actually saw in the system."},
                    },
                    "required": ["criterion", "passed", "observed"],
                },
            },
            "ambiguity": {**_CHECK_SCHEMA, "description": "Mandatory: false when another matching entity has a competing "
                          "candidate and the worker did not ask the user which one was meant."},
            "completeness": {**_CHECK_SCHEMA, "description": "Mandatory: false when a source the worker did not consider "
                             "holds a newer or competing candidate."},
            "summary": {"type": "string", "description": "Short summary; if failed, say exactly what is wrong."},
        },
        "required": ["passed", "checks", "ambiguity", "completeness", "summary"],
    }
    read_only_safe = False

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, observation="Verdict recorded.")


@dataclass
class Verdict:
    """Outcome of one verification round. ``inconclusive`` means the auditor could not complete its check."""

    passed: bool
    checks: list[dict] = field(default_factory=list)
    summary: str = ""
    inconclusive: bool = False
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def failed_checks(self) -> list[dict]:
        """Checks that did not pass."""
        return [c for c in self.checks if not c.get("passed")]


def _questions_text(questions: list[dict]) -> str:
    """The worker's questions to the user and the answers, one block per question (what the ambiguity check needs)."""
    if not questions:
        return "(none: the worker never asked the user anything)"
    lines = []
    for q in questions:
        options = f" (options: {', '.join(map(str, q['options']))})" if q.get("options") else ""
        lines.append(f"- Q: {q.get('question', '')}{options}\n  A: {q.get('answer') or '(no answer received)'}")
    return "\n".join(lines)


def _briefing(
    task: str, criteria: list[str], claim: dict, facts_text: str, questions: list[dict] | None = None,
    sources: str = "",
) -> str:
    claimed = {k: claim.get(k) for k in ("summary", "outputs", "evidence")}
    return (
        f"Today's date: {date.today().isoformat()}\n\n"
        f"ORIGINAL TASK:\n{task}\n\n"
        "SUCCESS CRITERIA:\n" + ("\n".join(f"- {c}" for c in criteria) or "- (none stated: derive checks from the task)")
        + f"\n\nWORKER'S CLAIM (unverified):\n{json.dumps(claimed, indent=2, ensure_ascii=False, default=str)}"
        + f"\n\nWORKER'S WORKING-MEMORY FACTS (unverified):\n{facts_text}\n\n"
        f"QUESTIONS THE WORKER ASKED THE USER, AND THE ANSWERS:\n{_questions_text(questions or [])}\n\n"
        f"SYSTEMS AND SOURCES AVAILABLE TO YOU:\n{sources or '(not described: use list_credentials and the start page)'}\n\n"
        "Re-check the real system now, including the mandatory ambiguity and completeness checks, then call submit_verdict."
    )


class Verifier:
    """Runs a separate, short LLM conversation that audits a success claim with the read-only tool subset."""

    def __init__(self, llm: LLMClient, registry: ToolRegistry, max_steps: int = DEFAULT_MAX_STEPS):
        self._llm = llm
        self._tools = registry.subset(read_only=True)
        self._verdict_spec = SubmitVerdictTool().spec()
        self.max_steps = max_steps

    async def verify(
        self,
        *,
        task: str,
        criteria: list[str],
        claim: dict,
        facts_text: str,
        ctx: ToolContext,
        on_step: StepCallback | None = None,
        questions: list[dict] | None = None,
        sources: str = "",
    ) -> Verdict:
        """Audit the claim. ``ctx.extras['read_only']`` is set for the duration and restored afterwards.

        ``questions``: what the worker asked the user (``{"question", "options", "answer"}``), so the auditor can tell
        whether an ambiguity was put to the user. ``sources``: a description of the systems the auditor can look at.
        A verdict without the mandatory ``ambiguity`` / ``completeness`` checks is sent back once (it gets one extra
        step for that); if it still lacks them the audit is inconclusive."""
        tool_names = ", ".join(self._tools.names())
        described = f"{sources}\n" if sources else ""
        messages = [
            Message(role="system", content=VERIFIER_SYSTEM_PROMPT),
            Message(role="user", content=_briefing(task, criteria, claim, facts_text, questions,
                                                   f"{described}- Your read-only tools: {tool_names}")),
        ]
        usage = Verdict(passed=False)
        redactor = ctx.extras.get("redactor")
        previous = ctx.extras.get("read_only")
        ctx.extras["read_only"] = True
        budget, turn, malformed, resubmitted = self.max_steps, 0, 0, False
        try:
            while turn < budget:
                last_turn = turn == budget - 1
                turn += 1
                specs = [self._verdict_spec] if last_turn else self._tools.specs() + [self._verdict_spec]
                outgoing = redactor.redact_messages(messages) if redactor else messages  # claim and facts come from the worker
                try:
                    response = await self._llm.complete(outgoing, specs, tool_choice="any" if last_turn else "auto")
                except MalformedCallError as err:
                    malformed += 1
                    if malformed > MAX_MALFORMED_REPLIES:
                        return self._inconclusive(usage, f"The auditor's model kept producing malformed tool calls: {err}")
                    if not messages[-1].content.endswith(MALFORMED_CALL_NOTE):
                        messages[-1] = replace(messages[-1], content=f"{messages[-1].content}\n\n{MALFORMED_CALL_NOTE}")
                    continue
                except LLMError as err:
                    return self._inconclusive(usage, f"The auditor's LLM call failed: {err}")
                malformed = 0
                usage.llm_calls += 1
                usage.input_tokens += response.usage.input_tokens
                usage.output_tokens += response.usage.output_tokens

                verdict_call = next((c for c in response.tool_calls if c.name == "submit_verdict"), None)
                if verdict_call is not None:
                    missing = self._missing_checks(verdict_call.args)
                    if not missing:
                        return self._parse(verdict_call, usage)
                    if resubmitted:
                        return self._inconclusive(usage, f"The auditor's verdict lacked the mandatory check(s): {', '.join(missing)}.")
                    resubmitted, budget = True, budget + 1
                    messages += [
                        Message(role="assistant", content=response.text, tool_calls=[verdict_call]),
                        Message(role="tool", content="ERROR: " + VERIFIER_MISSING_CHECKS.format(missing=", ".join(missing)),
                                tool_call_id=verdict_call.id, name=verdict_call.name),
                    ]
                    continue
                if not response.tool_calls:
                    messages += [
                        Message(role="assistant", content=response.text),
                        Message(role="user", content=VERIFIER_NO_TOOL_NUDGE),
                    ]
                    continue
                messages.append(Message(role="assistant", content=response.text, tool_calls=response.tool_calls))
                for call in response.tool_calls:
                    messages.append(await self._run_tool(call, ctx, on_step))
            return self._inconclusive(usage, "The auditor ran out of steps without a verdict.")
        finally:
            if previous is None:
                ctx.extras.pop("read_only", None)
            else:
                ctx.extras["read_only"] = previous

    async def _run_tool(self, call: ToolCall, ctx: ToolContext, on_step: StepCallback | None) -> Message:
        tool = self._tools.get(call.name)
        started = time.monotonic()
        if tool is None:
            result = ToolResult(False, f"Tool '{call.name}' is not available to the auditor (read-only tools only).", "unavailable")
        elif tool.classify(call.args, ctx) is not Risk.READ:
            result = ToolResult(False, "Refused: the auditor may only perform read-only actions.", "read_only_mode")
        else:
            result = await execute_with_retry(tool, call.args, ctx)
        if on_step is not None:
            await on_step(call.name, call.args, result, int((time.monotonic() - started) * 1000))
        content = truncate(result.observation, OBSERVATION_LIMIT)
        return Message(role="tool", content=content if result.ok else f"ERROR: {content}", tool_call_id=call.id, name=call.name)

    @staticmethod
    def _missing_checks(args: dict) -> list[str]:
        """Names of the mandatory checks (``ambiguity`` / ``completeness``) the verdict lacks or filled in incompletely."""
        missing = []
        for key in MANDATORY_CHECKS:
            value = args.get(key)
            if not (isinstance(value, dict) and isinstance(value.get("passed"), bool) and str(value.get("observed") or "").strip()):
                missing.append(key)
        return missing

    @staticmethod
    def _parse(call: ToolCall, usage: Verdict) -> Verdict:
        checks = [
            {"criterion": str(c.get("criterion", "")), "passed": bool(c.get("passed")), "observed": str(c.get("observed", ""))}
            for c in call.args.get("checks") or []
            if isinstance(c, dict)
        ]
        mandatory = [
            {"criterion": name, "passed": bool(call.args[key]["passed"]), "observed": str(call.args[key]["observed"])}
            for key, name in MANDATORY_CHECKS.items()
        ]
        # The mandatory checks sit in the same list as the criterion checks, so events, report and evals show them alike.
        passed = bool(call.args.get("passed")) and bool(checks) and all(c["passed"] for c in checks + mandatory)
        summary = str(call.args.get("summary", ""))
        if call.args.get("passed") and not passed:
            summary = f"{summary} (Downgraded to failed: the verdict had no checks or contained a failed check.)".strip()
        return Verdict(passed, checks + mandatory, summary, False, usage.llm_calls, usage.input_tokens, usage.output_tokens)

    @staticmethod
    def _inconclusive(usage: Verdict, reason: str) -> Verdict:
        return Verdict(False, [], reason, True, usage.llm_calls, usage.input_tokens, usage.output_tokens)
