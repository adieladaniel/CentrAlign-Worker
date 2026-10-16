"""API tests for the worker server: scripted LLM, no browser, no simulated company."""
from __future__ import annotations

import itertools
import json
import time

import pytest
from fastapi.testclient import TestClient

from worker.config import Settings
from worker.llm.base import LLMError, LLMResponse, ToolCall, Usage
from worker.llm.fake import ScriptedLLM
from worker.server import create_app, load_scenarios, synthesize_events
from worker.tools.control import FinishTool, UpdatePlanTool
from worker.tools.files import ListFilesTool, ReadFileTool, WriteFileTool
from worker.tools.interaction import AskUserTool
from worker.tools.memory_tools import RememberTool
from worker.tools.registry import ToolRegistry

AUDITOR_PREFIX = "You are an independent auditor"
_ids = itertools.count(1)


def call(name: str, **args) -> ToolCall:
    return ToolCall(id=f"call_{next(_ids)}", name=name, args=args)


def reply(text: str = "", *calls: ToolCall) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=list(calls), usage=Usage(10, 5))


def verdict(passed: bool) -> LLMResponse:
    return reply("", call("submit_verdict", passed=passed, summary="audit ok",
                          checks=[{"criterion": "File exists", "passed": passed, "observed": "saw it"}]))


def routed_llm(worker: list[LLMResponse], auditor: list[LLMResponse] = ()) -> ScriptedLLM:
    worker_q, auditor_q = list(worker), list(auditor)

    def responder(messages, tools):
        queue = auditor_q if messages[0].content.startswith(AUDITOR_PREFIX) else worker_q
        return queue.pop(0)

    return ScriptedLLM(responder)


def small_registry(_settings: Settings) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (ListFilesTool(), ReadFileTool(), WriteFileTool(), RememberTool(), UpdatePlanTool(), FinishTool(), AskUserTool()):
        registry.register(tool)
    return registry


@pytest.fixture
def make_client(tmp_path):
    clients: list[TestClient] = []

    def factory(llm_factory, **settings_overrides) -> TestClient:
        settings = Settings(
            LLM_PROVIDER="fake", RUNS_DIR=str(tmp_path / "runs"), WORKSPACE_DIR=str(tmp_path / "workspace"),
            SIM_BASE_URL="http://127.0.0.1:9", LLM_MIN_INTERVAL_S=0.0, **settings_overrides,
        )
        (tmp_path / "workspace").mkdir(exist_ok=True)
        client = TestClient(create_app(settings, llm_factory=llm_factory, registry_factory=small_registry))
        client.__enter__()  # keep one event loop alive so the background run survives between requests
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


def read_until(ws, wanted: str) -> list[dict]:
    """Collect events from the websocket up to and including the first one of type ``wanted``."""
    seen = []
    while True:
        event = ws.receive_json()
        seen.append(event)
        if event["type"] == wanted:
            return seen
        assert event["type"] != "error", event


def wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def is_idle(client: TestClient) -> bool:
    """True once no run is active (the runs list reports terminal statuses only). Avoids the slow sim probe of /api/health."""
    return all(r["status"] in ("succeeded", "failed", "needs_attention", "cancelled") for r in client.get("/api/runs").json())


def write_script() -> list[LLMResponse]:
    return [
        reply("Planning.", call("update_plan", goal_summary="Save a note", success_criteria=["File exists"],
                                items=[{"text": "Write the note", "status": "in_progress"}])),
        reply("Writing the note.", call("write_file", path="note.txt", content="hello")),
        reply("", call("finish", status="success", summary="Note saved.",
                       outputs=[{"name": "path", "value": "note.txt"}],
                       evidence=[{"claim": "file written", "source": "write_file"}])),
    ]


def test_run_end_to_end_with_approval_over_websocket(make_client, tmp_path):
    llm = routed_llm(write_script(), [reply("", call("read_file", path="note.txt")), verdict(True)])
    client = make_client(lambda s: llm)

    run_id = client.post("/api/runs", json={"task": "Save a note"}).json()["run_id"]
    with client.websocket_connect(f"/api/runs/{run_id}/events") as ws:
        events = read_until(ws, "approval_requested")
        approval = events[-1]["payload"]
        assert approval["tool"] == "write_file" and approval["risk"] == "write"
        assert approval["args"] == {"path": "note.txt", "content": "hello"}
        assert any(e["type"] == "plan_updated" for e in events)
        assert events[0]["type"] == "run_started"

        # Only one run at a time while this one waits for the human.
        assert client.post("/api/runs", json={"task": "another"}).status_code == 409
        assert client.get("/api/runs").json()[0]["status"] == "waiting_approval"

        bad = client.post(f"/api/runs/{run_id}/respond", json={"request_id": "nope", "approved": True})
        assert bad.status_code == 404
        ok = client.post(f"/api/runs/{run_id}/respond", json={"request_id": approval["request_id"], "approved": True})
        assert ok.status_code == 200 and ok.json()["kind"] == "approval"
        again = client.post(f"/api/runs/{run_id}/respond", json={"request_id": approval["request_id"], "approved": True})
        assert again.status_code in (404, 409)

        rest = read_until(ws, "run_finished")
    types = [e["type"] for e in rest]
    assert "approval_resolved" in types and "verification_started" in types and "verification_finished" in types
    finished = rest[-1]["payload"]
    assert finished["status"] == "succeeded"
    assert finished["report"]["outputs"] == {"path": "note.txt"}
    assert finished["report"]["verification"]["passed"] is True
    assert (tmp_path / "workspace" / "note.txt").read_text() == "hello"

    # Seq numbers are contiguous, and the same events are on disk.
    log_lines = [json.loads(line) for line in (tmp_path / "runs" / run_id / "events.jsonl").read_text().splitlines()]
    assert [e["seq"] for e in log_lines] == list(range(1, len(log_lines) + 1))  # 1-based, like a DB sequence
    assert log_lines[-1]["type"] == "run_finished"

    # A late subscriber gets the whole history replayed, then the stream closes.
    assert wait_for(lambda: is_idle(client))
    with client.websocket_connect(f"/api/runs/{run_id}/events") as ws2:
        replayed = read_until(ws2, "replay_done")
    assert [e["type"] for e in replayed[:-1]] == [e["type"] for e in log_lines]

    # History endpoints.
    runs = client.get("/api/runs").json()
    assert runs[0]["id"] == run_id and runs[0]["status"] == "succeeded" and runs[0]["task"] == "Save a note"
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["report"]["status"] == "succeeded" and detail["report_url"].endswith("report.md")
    report_md = client.get(detail["report_url"])
    assert report_md.status_code == 200 and "Note saved." in report_md.text
    assert client.get("/api/runs/unknown1").status_code == 404
    assert client.post(f"/api/runs/{run_id}/cancel").status_code == 409  # finished runs cannot be cancelled


def test_question_is_answered_through_respond_and_run_can_be_cancelled(make_client):
    llm = routed_llm([
        reply("", call("ask_user", question="Which vendor?", options=["A", "B"])),
        reply("", call("finish", status="blocked", summary="Need more info.")),
    ])
    client = make_client(lambda s: llm)
    run_id = client.post("/api/runs", json={"task": "Ambiguous", "approval_mode": "auto"}).json()["run_id"]
    with client.websocket_connect(f"/api/runs/{run_id}/events") as ws:
        question = read_until(ws, "question_asked")[-1]["payload"]
        assert question["question"] == "Which vendor?" and question["options"] == ["A", "B"]
        resp = client.post(f"/api/runs/{run_id}/respond", json={"request_id": question["request_id"], "answer": "A"})
        assert resp.json()["kind"] == "question"
        events = read_until(ws, "run_finished")
    answered = [e for e in events if e["type"] == "tool_finished" and e["payload"]["tool"] == "ask_user"]
    assert answered and "User answered: A" in answered[0]["payload"]["observation"]
    assert events[-1]["payload"]["status"] == "needs_attention"


def test_cancel_releases_a_pending_approval(make_client):
    llm = routed_llm(write_script())
    client = make_client(lambda s: llm)
    run_id = client.post("/api/runs", json={"task": "Save a note"}).json()["run_id"]
    with client.websocket_connect(f"/api/runs/{run_id}/events") as ws:
        read_until(ws, "approval_requested")
        assert client.post(f"/api/runs/{run_id}/cancel").json()["status"] == "cancelling"
        last = read_until(ws, "run_finished")[-1]
    assert last["payload"]["status"] == "cancelled"
    assert wait_for(lambda: is_idle(client))
    # A new run can start afterwards.
    assert client.post("/api/runs", json={"task": "again"}).status_code == 200


def test_start_run_reports_llm_configuration_errors(make_client):
    def broken(_settings):
        raise LLMError("GEMINI_API_KEY is not set")

    client = make_client(broken)
    resp = client.post("/api/runs", json={"task": "x"})
    assert resp.status_code == 400 and "GEMINI_API_KEY" in resp.json()["detail"]
    assert client.post("/api/runs", json={"task": "  "}).status_code == 422


def test_run_files_are_served_and_path_traversal_is_blocked(make_client, tmp_path):
    client = make_client(lambda s: routed_llm([]))
    run_dir = tmp_path / "runs" / "abc12345"
    (run_dir / "screenshots").mkdir(parents=True)
    (run_dir / "screenshots" / "a.png").write_bytes(b"\x89PNG fake")
    (tmp_path / "runs" / "secret.txt").write_text("top secret")
    ok = client.get("/runs/abc12345/files/screenshots/a.png")
    assert ok.status_code == 200 and ok.content.startswith(b"\x89PNG")
    for evil in ("../secret.txt", "..%2Fsecret.txt", "screenshots/../../secret.txt", "%2e%2e/secret.txt"):
        assert client.get(f"/runs/abc12345/files/{evil}").status_code == 404
    assert client.get("/runs/abc12345/files/missing.png").status_code == 404
    assert client.get("/runs/..%2F..%2Fx/files/a").status_code in (400, 404)


def test_scenarios_health_and_sim_proxy_errors(make_client):
    client = make_client(lambda s: routed_llm([]))
    scenarios = client.get("/api/scenarios").json()
    assert len(scenarios) >= 3
    assert all({"id", "title", "task", "chaos"} <= set(s) for s in scenarios)
    assert any("INI-20502" in s["task"] for s in scenarios)
    health = client.get("/api/health").json()
    assert health["provider"] == "fake" and health["sim_reachable"] is False and health["approval_mode"] == "confirm_writes"
    assert client.get("/api/sim/chaos").status_code == 502


def test_load_scenarios_from_yaml(tmp_path):
    (tmp_path / "one.yaml").write_text("id: one\ntitle: First\ntask: Do the thing\nchaos: [slow_pages]\n", encoding="utf-8")
    (tmp_path / "bad.yaml").write_text("- not a mapping", encoding="utf-8")
    (tmp_path / "notask.yaml").write_text("id: x\n", encoding="utf-8")
    assert load_scenarios(tmp_path) == [{"id": "one", "title": "First", "task": "Do the thing", "chaos": ["slow_pages"]}]
    assert len(load_scenarios(tmp_path / "missing")) >= 3  # built-in fallback


def test_history_without_event_log_is_synthesized_from_run_json(make_client, tmp_path):
    state = {
        "run_id": "cli00001", "task": "Old CLI run", "status": "succeeded", "started_at": "2026-01-01T10:00:00+00:00",
        "finished_at": "2026-01-01T10:01:00+00:00", "goal_summary": "g", "success_criteria": ["c"],
        "plan": [{"id": 1, "text": "step", "status": "done", "note": ""}],
        "facts": {"k": {"key": "k", "value": "v", "source": "s", "step": 1}},
        "steps": [
            {"index": 1, "kind": "tool", "tool": "read_file", "args": {}, "risk": "read", "ok": True, "observation": "x",
             "error": None, "evidence": [], "duration_ms": 5, "retries": 0, "timestamp": "t", "thought": "", "turn": 1},
            {"index": 2, "kind": "approval", "tool": "write_file", "args": {"path": "a"}, "risk": "write", "ok": True,
             "observation": "APPROVED: Write a file", "thought": "", "timestamp": "t", "turn": 2},
            {"index": 3, "kind": "verify", "tool": "read_file", "args": {}, "risk": "read", "ok": True, "observation": "y",
             "error": None, "evidence": [], "duration_ms": 5, "retries": 0, "timestamp": "t", "thought": "", "turn": 0},
        ],
        "usage": {"llm_calls": 3, "input_tokens": 10, "output_tokens": 5},
        "final_report": {"status": "succeeded", "summary": "Done", "outputs": {}, "evidence": [], "stats": {}, "artifacts": [],
                         "verification": {"ran": True, "passed": True, "inconclusive": False, "checks": [], "summary": "ok"}},
    }
    types = [e["type"] for e in synthesize_events(state)]
    assert types[0] == "run_started" and types[-1] == "run_finished"
    for expected in ("plan_updated", "memory_updated", "tool_finished", "approval_requested", "approval_resolved",
                     "verification_started", "verification_finished", "usage"):
        assert expected in types
    assert types.index("verification_started") < types.index("verification_finished") < types.index("run_finished")

    run_dir = tmp_path / "runs" / "cli00001"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
    client = make_client(lambda s: routed_llm([]))
    with client.websocket_connect("/api/runs/cli00001/events") as ws:
        events = read_until(ws, "replay_done")
    assert events[-2]["type"] == "run_finished"
