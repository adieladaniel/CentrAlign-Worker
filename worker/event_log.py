"""Event-log helpers shared by the file-mode and database-mode servers."""
from __future__ import annotations


def synthesize_events(state: dict) -> list[dict]:
    """Rebuild an event log from a stored run state (for runs that have no event log, e.g. stored before event logging)."""
    events: list[dict] = []
    run_id = state.get("run_id", "")

    def add(event_type: str, payload: dict, ts: str = "") -> None:
        events.append({"seq": len(events) + 1, "type": event_type, "ts": ts or state.get("started_at", ""),
                       "payload": {"run_id": run_id, **payload}})

    add("run_started", {"task": state.get("task", ""), "approval_mode": "unknown"})
    if state.get("plan"):
        add("plan_updated", {"plan": state["plan"], "goal_summary": state.get("goal_summary", ""),
                             "success_criteria": state.get("success_criteria", [])})
    for fact in (state.get("facts") or {}).values():
        add("memory_updated", {"key": fact.get("key"), "value": fact.get("value"), "source": fact.get("source"),
                               "step": fact.get("step", 0)})
    report = state.get("final_report") or {}
    verification = report.get("verification") or {}
    verifying = False
    for step in state.get("steps") or []:
        kind, ts = step.get("kind"), step.get("timestamp", "")
        if kind == "verify" and not verifying:
            verifying = True
            add("verification_started", {"criteria": state.get("success_criteria", [])}, ts)
        if kind != "verify" and verifying:
            verifying = False
            add("verification_finished", {"passed": verification.get("passed", False),
                                          "inconclusive": verification.get("inconclusive", False),
                                          "checks": verification.get("checks", []),
                                          "summary": verification.get("summary", "")}, ts)
        if kind in ("tool", "ask_user", "verify"):
            add("tool_finished", {**step, "phase": "verify" if kind == "verify" else "work"}, ts)
        elif kind == "approval":
            action = str(step.get("observation", "")).partition(": ")[2] or step.get("tool", "")
            add("approval_requested", {"request_id": f"hist-{step.get('index')}", "tool": step.get("tool"), "action": action,
                                       "args": step.get("args", {}), "risk": step.get("risk", "write"), "context": {}}, ts)
            add("approval_resolved", {"response": {"approved": bool(step.get("ok")), "note": step.get("thought", "")},
                                      "answered": True}, ts)
        elif kind == "system" and step.get("error") == "resumed":
            add("run_resumed", {"message": step.get("thought", "")}, ts)
        elif kind == "system":
            add("guard_triggered", {"guard": step.get("error", ""), "level": "nudge", "message": step.get("thought", "")}, ts)
    if verifying or (verification.get("ran") and not any(e["type"] == "verification_finished" for e in events)):
        if not verifying:
            add("verification_started", {"criteria": state.get("success_criteria", [])})
        add("verification_finished", {"passed": verification.get("passed", False),
                                      "inconclusive": verification.get("inconclusive", False),
                                      "checks": verification.get("checks", []), "summary": verification.get("summary", "")})
    usage = state.get("usage") or {}
    if usage:
        add("usage", {**usage, "llm_retries": 0})
    if report:
        add("run_finished", {"status": state.get("status"), "report": report}, state.get("finished_at") or "")
    return events
