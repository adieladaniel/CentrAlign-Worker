"""Final report: structured dict for the UI/API plus a human-readable markdown rendering."""
from __future__ import annotations

from pathlib import Path

from worker.agent.state import NON_FAULT_ERRORS, RunState
from worker.agent.verifier import Verdict


def collect_artifacts(state: RunState, run_dir: Path) -> list[str]:
    """Evidence files (screenshots, etc.) referenced by steps, plus anything in the run's downloads folder."""
    found = {path for step in state.steps for path in step.evidence}
    downloads = run_dir / "downloads"
    if downloads.is_dir():
        found.update(p.relative_to(run_dir).as_posix() for p in downloads.rglob("*") if p.is_file())
    return sorted(found)


def compute_stats(state: RunState, approvals: dict[str, int]) -> dict:
    """Run statistics shown on the report card and in eval tables."""
    tool_steps = [s for s in state.steps if s.kind in ("tool", "ask_user")]
    stats = {
        "steps": sum(1 for s in state.steps if s.kind == "tool"),
        "llm_calls": state.usage["llm_calls"],
        "tokens": state.usage["input_tokens"] + state.usage["output_tokens"],
        "tool_failures": sum(1 for s in tool_steps if not s.ok and s.error not in NON_FAULT_ERRORS),
        "retries": sum(s.retries for s in tool_steps),
        "approvals": approvals["approved"],
        "denials": approvals["denied"],
    }
    if state.usage.get("model"):
        stats["model"] = state.usage["model"]
        stats["model_switches"] = state.usage.get("model_switches", 0)
    return stats


def build_report(
    state: RunState,
    *,
    status: str,
    claim: dict | None,
    verdict: Verdict | None,
    reason: str,
    approvals: dict[str, int],
    run_dir: Path,
) -> dict:
    """Assemble the final report. ``reason`` explains abnormal endings (guards, errors) and is used as the summary
    when the agent never produced one."""
    done = sum(1 for p in state.plan if p.status == "done")
    progress = f"Progress: {done}/{len(state.plan)} plan items done." if state.plan else "No plan was recorded."
    claimed = (claim or {}).get("summary", "")
    if status == "succeeded":
        summary = claimed
    elif claimed:
        summary = f"{claimed}\n\n{reason}".strip()
    else:
        summary = f"{reason} {progress}".strip()
    return {
        "status": status,
        "summary": summary,
        "outputs": (claim or {}).get("outputs", {}),
        "evidence": (claim or {}).get("evidence", []),
        "verification": {
            "ran": verdict is not None,
            "passed": bool(verdict and verdict.passed),
            "inconclusive": bool(verdict and verdict.inconclusive),
            "checks": verdict.checks if verdict else [],
            "summary": verdict.summary if verdict else "",
        },
        "facts": {k: f.value for k, f in state.facts.items()},
        "stats": compute_stats(state, approvals),
        "artifacts": collect_artifacts(state, run_dir),
    }


def render_markdown(state: RunState, report: dict) -> str:
    """Human-readable version of the report."""
    lines = [f"# Run {state.run_id}: {report['status'].upper()}", "", f"**Task:** {state.task}", "", "## Summary", report["summary"] or "(none)"]
    if report["outputs"]:
        lines += ["", "## Outputs"] + [f"- **{k}:** {v}" for k, v in report["outputs"].items()]
    if report["evidence"]:
        lines += ["", "## Evidence"] + [f"- {e.get('claim')} (source: {e.get('source')})" for e in report["evidence"]]
    verification = report["verification"]
    lines += ["", "## Verification"]
    if not verification["ran"]:
        lines.append("Not run (the agent did not claim success).")
    else:
        outcome = "INCONCLUSIVE" if verification["inconclusive"] else ("PASSED" if verification["passed"] else "FAILED")
        lines.append(f"Independent audit: **{outcome}**. {verification['summary']}")
        lines += [f"- [{'x' if c['passed'] else ' '}] {c['criterion']}: {c['observed']}" for c in verification["checks"]]
    if state.plan:
        lines += ["", "## Plan"] + [f"- [{'x' if p.status == 'done' else ' '}] {p.text} ({p.status})" for p in state.plan]
    stats = report["stats"]
    lines += ["", "## Stats", ", ".join(f"{k}: {v}" for k, v in stats.items())]
    if report["artifacts"]:
        lines += ["", "## Artifacts"] + [f"- {a}" for a in report["artifacts"]]
    return "\n".join(lines) + "\n"
