"""Durable approvals and cross-process events (``worker/agent/pg_broker.py``, ``worker/approvals_pg.py``,
``worker/live.py``, ``worker/runs_api.py``).

Two ``create_app`` instances stand for two API processes, workers are asyncio tasks, all over the one test database.
Approvals are answered through one process and received by a worker that never shared memory with it.
"""
from __future__ import annotations

import asyncio
import uuid

from dbapp import (
    Gates,
    Planner,
    audit_actions,
    blocked,
    call,
    event_types,
    finished,
    kill_worker,
    new_worker,
    pending_request,
    registry_with,
    reply,
    session_cookie,
    sign_in,
    start_run,
    wait_until,
    wire_script,
    write_script,
    ws_connect,
)
from sqlalchemy import text

from worker.store_pg import PgRunStore

QUICK = {"JOB_LEASE_S": 5.0, "JOB_POLL_S": 0.2, "JOB_REAPER_INTERVAL_S": 0.2,
         "APPROVAL_POLL_S": 60, "LIVE_FALLBACK_POLL_S": 60}  # polling is too slow to matter: only NOTIFY can be this fast


async def approval_rows(sm, run_id: str) -> list[dict]:
    async with sm() as session:
        rows = (await session.execute(text(
            "select id, kind, status, response, responded_by, consumed_at, expires_at from approvals "
            "where run_id = :id order by requested_at"), {"id": uuid.UUID(run_id)})).all()
    return [dict(r._mapping) for r in rows]


def answer(request: dict, approved: bool = True, **extra) -> dict:
    return {"request_id": request["request_id"], "approved": approved, **extra}


# --------------------------------------------------------------------------- answering from another process


async def test_an_approval_answered_through_another_api_process_reaches_the_worker(world, db_app):
    app_a, planner = db_app(**QUICK)
    planner.add(*write_script())
    starter = await sign_in(app_a, "operator@acme.test")  # app A also runs the in-process worker
    run_id = await start_run(starter)
    request = await pending_request(starter, run_id)
    rows = await approval_rows(world.sm, run_id)
    assert len(rows) == 1 and request["request_id"] == str(rows[0]["id"]) and rows[0]["status"] == "pending"
    assert request["kind"] == "approval" and rows[0]["expires_at"] is not None  # the org's approval timeout

    app_b, _ = db_app(**QUICK)  # a second API process that has never heard of this run (it could even be a restart of A)
    assert app_b is not app_a and app_b.state.sessions == {}
    approver = await sign_in(app_b, "approver@acme.test", worker=False)
    assert (await approver.get(f"/api/runs/{run_id}")).json()["pending"][0]["request_id"] == request["request_id"]
    resp = await approver.post(f"/api/runs/{run_id}/respond", json=answer(request, note="fine by me"))
    assert resp.status_code == 200 and resp.json() == {"status": "ok", "kind": "approval"}

    detail = await finished(starter, run_id, timeout=8)  # seconds, not the 60 s poll: the NOTIFY woke the worker
    assert detail["steps"][0]["kind"] == "approval" and detail["steps"][0]["observation"].startswith("APPROVED by Alice Approver")
    assert detail["steps"][0]["thought"] == "fine by me" and detail["steps"][1]["tool"] == "write_file"
    row = (await approval_rows(world.sm, run_id))[0]
    assert row["status"] == "approved" and row["responded_by"] == world.user_ids["approver@acme.test"]
    assert row["response"]["approved"] is True and row["response"]["by"]["name"] == "Alice Approver" and row["consumed_at"]
    events = await PgRunStore(world.sm, world.org_ids["acme"]).read_events(run_id)
    asked = next(e for e in events if e["type"] == "approval_requested")
    resolved = next(e for e in events if e["type"] == "approval_resolved")
    assert asked["payload"]["request_id"] == request["request_id"]  # the id in events and UI is the row id
    assert resolved["payload"]["answered"] is True and resolved["payload"]["response"]["by"]["email"] == "approver@acme.test"
    actions = await audit_actions(world.sm)
    assert "approval.respond" in actions and "run.start" in actions


async def test_the_first_answer_wins_and_a_second_one_is_a_409(world, db_app):
    app_a, planner = db_app(**QUICK)
    app_b, _ = db_app(**QUICK)
    planner.add(*write_script())
    planner.add(*write_script())
    starter = await sign_in(app_a, "operator@acme.test")
    run_id = await start_run(starter)
    request = await pending_request(starter, run_id)
    one, two = await sign_in(app_a, "approver@acme.test", worker=False), await sign_in(app_b, "admin@acme.test", worker=False)

    sequential = await one.post(f"/api/runs/{run_id}/respond", json=answer(request))
    assert sequential.status_code == 200
    again = await two.post(f"/api/runs/{run_id}/respond", json=answer(request, approved=False, note="too late"))
    assert again.status_code == 409 and "already resolved" in again.json()["detail"]
    assert (await one.post(f"/api/runs/{run_id}/respond", json=answer(request))).status_code == 409
    await finished(starter, run_id)
    assert (await approval_rows(world.sm, run_id))[0]["status"] == "approved"  # the late denial changed nothing

    race_run = await start_run(starter)  # two approvers on two processes answer at the same moment
    race = await pending_request(starter, race_run)
    results = await asyncio.gather(one.post(f"/api/runs/{race_run}/respond", json=answer(race, True)),
                                   two.post(f"/api/runs/{race_run}/respond", json=answer(race, False)))
    assert sorted(r.status_code for r in results) == [200, 409]
    detail = await finished(starter, race_run)
    won = next(r for r in results if r.status_code == 200)
    assert won.json()["kind"] == "approval"
    (stored,) = await approval_rows(world.sm, race_run)
    assert stored["status"] in ("approved", "denied") and stored["response"]["approved"] == (stored["status"] == "approved")
    assert detail["steps"][0]["observation"].split(" ")[0] == stored["status"].upper()  # the run saw the winner's answer


# --------------------------------------------------------------------------- expiry and cancellation


async def test_an_unanswered_approval_expires_and_the_run_stops_with_an_explicit_reason(world, db_app):
    app, planner = db_app(**QUICK)
    planner.add(*write_script())
    async with world.sm() as session, session.begin():
        await session.execute(text("update orgs set settings = settings || '{\"approval_timeout_s\": 1}'::jsonb where slug = 'acme'"))
    starter = await sign_in(app, "operator@acme.test")
    run_id = await start_run(starter)
    detail = await finished(starter, run_id, timeout=10)

    assert detail["status"] == "needs_attention"
    step = detail["steps"][0]
    assert step["kind"] == "approval" and step["ok"] is False and step["thought"] == "approval expired after 1 s"
    assert step["observation"].startswith("DENIED: ")
    assert "approval expired after 1 s" in detail["report"]["summary"] and "Stopped with partial progress" in detail["report"]["summary"]
    assert len(planner.created) == 1 and len(planner.created[0].calls) == 1  # the agent was not asked to carry on
    (row,) = await approval_rows(world.sm, run_id)
    assert row["status"] == "expired"
    events = await PgRunStore(world.sm, world.org_ids["acme"]).read_events(run_id)
    resolved = next(e for e in events if e["type"] == "approval_resolved")
    assert resolved["payload"]["answered"] is False and resolved["payload"]["reason"] == "approval expired after 1 s"

    late = await sign_in(app, "approver@acme.test", worker=False)
    reply_ = await late.post(f"/api/runs/{run_id}/respond", json={"request_id": str(row["id"]), "approved": True})
    assert reply_.status_code == 409 and "expired" in reply_.json()["detail"]


async def test_an_overdue_answer_is_refused_even_before_the_worker_noticed(world, db_app):
    app, planner = db_app(**QUICK)
    planner.add(*write_script())
    starter = await sign_in(app, "operator@acme.test")
    run_id = await start_run(starter)
    request = await pending_request(starter, run_id)
    async with world.sm() as session, session.begin():  # the deadline passes (without the worker having looked yet)
        await session.execute(text("update approvals set expires_at = now() - interval '1 second' where id = :id"),
                              {"id": uuid.UUID(request["request_id"])})
    late = await sign_in(app, "approver@acme.test", worker=False)
    refused = await late.post(f"/api/runs/{run_id}/respond", json=answer(request))
    assert refused.status_code == 409 and "expired" in refused.json()["detail"]
    detail = await finished(starter, run_id, timeout=10)  # and the worker's poll/deadline logic then ends the run
    assert detail["status"] == "needs_attention" and "expired" in detail["report"]["summary"]


async def test_cancelling_the_run_cancels_its_open_approvals(world, db_app):
    app, planner = db_app(**QUICK)
    planner.add(*write_script())
    starter = await sign_in(app, "operator@acme.test")
    run_id = await start_run(starter)
    request = await pending_request(starter, run_id)
    assert (await starter.post(f"/api/runs/{run_id}/cancel")).status_code == 200
    detail = await finished(starter, run_id, timeout=8)
    assert detail["status"] == "cancelled" and detail["pending"] == []
    (row,) = await approval_rows(world.sm, run_id)
    assert row["status"] == "cancelled" and row["response"] == {"reason": "the run was cancelled"}
    approver = await sign_in(app, "approver@acme.test", worker=False)
    assert (await approver.post(f"/api/runs/{run_id}/respond", json=answer(request))).status_code == 409


# --------------------------------------------------------------------------- crashes while waiting


async def two_workers_one_dies_while_waiting(world, db_app, second_script):
    """A run waits for approval on worker A, which dies; returns everything a test needs to continue with worker B."""
    gates = Gates()
    planner = Planner()
    planner.add(*write_script())
    planner.add(*second_script)
    app, _ = db_app(**{**QUICK, "JOB_LEASE_S": 1.5}, registry_factory=registry_with(gates))
    app.state.llm_factory = planner
    starter = await sign_in(app, "operator@acme.test", worker=False)
    a = new_worker(app, "w-a")
    await a.start()
    run_id = await start_run(starter)
    request = await pending_request(starter, run_id)
    await kill_worker(a)
    return app, starter, run_id, request, planner


async def test_a_resumed_run_reattaches_to_its_open_approval_instead_of_asking_twice(world, db_app):
    again = [reply("Saving again.", call("write_file", path="note.txt", content="hello")), blocked()]
    app, starter, run_id, request, planner = await two_workers_one_dies_while_waiting(world, db_app, again)
    b = new_worker(app, "w-b")
    await b.start()
    # B reaps the dead lease, resumes the run, whose agent asks for the very same write: it finds the open request
    assert await wait_until(lambda: b.handles.get(run_id) is not None and b.handles[run_id].runner is not None
                            and b.handles[run_id].runner.state.status == "waiting_approval"
                            and b.handles[run_id].runner._turn >= 2, 15)
    rows = await approval_rows(world.sm, run_id)
    assert len(rows) == 1 and str(rows[0]["id"]) == request["request_id"] and rows[0]["status"] == "pending"  # no duplicate
    pending = (await starter.get(f"/api/runs/{run_id}")).json()["pending"]
    assert [p["request_id"] for p in pending] == [request["request_id"]]

    approver = await sign_in(app, "approver@acme.test", worker=False)
    assert (await approver.post(f"/api/runs/{run_id}/respond", json=answer(request))).status_code == 200
    detail = await finished(starter, run_id, timeout=10)
    writes = [s for s in detail["steps"] if s["tool"] == "write_file" and s["kind"] == "tool"]
    assert len(writes) == 1 and writes[0]["ok"]  # approved once, executed once
    assert [s["kind"] for s in detail["steps"] if s["kind"] in ("approval", "system")] == ["system", "approval"]
    types = await event_types(world.sm, run_id)
    assert types.count("approval_requested") == 2 and types.count("approval_resolved") == 1 and "run_resumed" in types
    assert len(await approval_rows(world.sm, run_id)) == 1
    assert len(planner.created) == 2


async def test_an_answer_given_while_no_worker_waited_is_taken_by_the_resumed_run(world, db_app):
    again = [reply("Saving again.", call("write_file", path="note.txt", content="hello")), blocked()]
    app, starter, run_id, request, _ = await two_workers_one_dies_while_waiting(world, db_app, again)
    approver = await sign_in(app, "approver@acme.test", worker=False)
    assert (await approver.post(f"/api/runs/{run_id}/respond", json=answer(request, note="go"))).status_code == 200
    (row,) = await approval_rows(world.sm, run_id)
    assert row["status"] == "approved" and row["consumed_at"] is None  # nobody has read it yet

    b = new_worker(app, "w-b")
    await b.start()
    detail = await finished(starter, run_id, timeout=15)
    rows = await approval_rows(world.sm, run_id)
    assert len(rows) == 1 and rows[0]["consumed_at"] is not None  # taken now, not asked again
    approval_step = next(s for s in detail["steps"] if s["kind"] == "approval")
    assert approval_step["observation"].startswith("APPROVED by Alice Approver") and approval_step["thought"] == "go"
    assert [s["tool"] for s in detail["steps"] if s["kind"] == "tool"].count("write_file") == 1


async def test_a_resumed_agent_that_asks_for_something_else_supersedes_the_old_request(world, db_app):
    different = [reply("Something else.", call("write_file", path="other.txt", content="x")), blocked()]
    app, starter, run_id, request, _ = await two_workers_one_dies_while_waiting(world, db_app, different)
    b = new_worker(app, "w-b")
    await b.start()
    assert await wait_until(_two_rows(world.sm, run_id), 15)
    old, new = await approval_rows(world.sm, run_id)
    assert str(old["id"]) == request["request_id"] and old["status"] == "cancelled"
    assert old["response"] == {"reason": "superseded after the run resumed"} and new["status"] == "pending"
    assert (await starter.post(f"/api/runs/{run_id}/cancel")).status_code == 200
    await finished(starter, run_id, timeout=8)


def _two_rows(sm, run_id):
    async def check() -> bool:
        return len(await approval_rows(sm, run_id)) == 2

    return check


# --------------------------------------------------------------------------- the inbox


async def test_the_inbox_lists_pending_approvals_with_rights_for_each_role(world, db_app):
    app, planner = db_app(**QUICK)
    planner.add(*wire_script())  # run 1, started by an operator: irreversible
    planner.add(*wire_script())  # run 2, started by an admin
    planner.add(*write_script())  # globex
    op = await sign_in(app, "operator@acme.test")
    admin = await sign_in(app, "admin@acme.test", worker=False)
    run_op = await start_run(op, "Pay the operator's invoice")
    await pending_request(op, run_op)
    run_admin = await start_run(admin, "Pay the admin's invoice")
    await pending_request(admin, run_admin)
    globex = await sign_in(app, "operator@globex.test", worker=False)
    run_globex = await start_run(globex, "Globex secret errand")
    await pending_request(globex, run_globex)

    def by_run(items):
        return {i["run_id"]: i for i in items}

    clients = {role: await sign_in(app, f"{role}@acme.test", worker=False) for role in ("owner", "approver", "viewer")}
    clients.update(operator=op, admin=admin)
    views = {role: by_run((await c.get("/api/approvals?status=pending")).json()) for role, c in clients.items()}
    for role, items in views.items():
        assert set(items) == {run_op, run_admin}, role  # the whole org's queue, nothing of globex
    item = views["owner"][run_op]
    assert item["kind"] == "approval" and item["risk"] == "irreversible" and item["tool"] == "wire_money"
    assert item["action"] and item["run_task"] == "Pay the operator's invoice" and item["requested_by"]["email"] == "operator@acme.test"
    assert item["request_id"] == item["id"] and item["payload"]["args"] == {"amount": "100 EUR"} and item["status"] == "pending"
    assert 0 <= item["age_s"] < 30 and 3000 < item["expires_in_s"] <= 3600 and item["expires_at"]

    def rights(role, run):
        i = views[role][run]
        return i["can_respond"], i["can_approve"], i["reason"]

    assert rights("owner", run_op)[:2] == (True, True) and rights("approver", run_admin)[:2] == (True, True)
    assert rights("admin", run_op)[:2] == (True, True)
    can_respond, can_approve, reason = rights("admin", run_admin)  # started it himself: may deny, not approve
    assert (can_respond, can_approve) == (True, False) and "Separation of duties" in reason
    can_respond, can_approve, reason = rights("operator", run_op)
    assert (can_respond, can_approve) == (False, False) and "operator" in reason and "organization settings" in reason
    can_respond, can_approve, reason = rights("viewer", run_op)
    assert (can_respond, can_approve) == (False, False) and "viewer" in reason

    assert (await admin.put("/api/admin/settings", json={"operators_can_approve": True})).status_code == 200
    views["operator"] = by_run((await op.get("/api/approvals")).json())
    assert rights("operator", run_admin)[:2] == (True, True)  # now an operator may approve someone else's run
    assert rights("operator", run_op)[:2] == (True, False)  # ... but not their own irreversible action

    foreign = (await globex.get("/api/approvals")).json()
    assert [i["run_id"] for i in foreign] == [run_globex] and foreign[0]["can_respond"] is False  # a globex operator, no right
    other_org_approver = await sign_in(app, "approver@globex.test", worker=False)
    assert [i["run_id"] for i in (await other_org_approver.get("/api/approvals")).json()] == [run_globex]

    approver = clients["approver"]  # resolved approvals leave the pending list but stay available
    assert (await approver.post(f"/api/runs/{run_op}/respond",
                                json=answer({"request_id": views["owner"][run_op]["id"]}, False, note="no"))).status_code == 200
    assert await wait_until(_pending_ids(approver, {run_admin}), 5)
    resolved = (await approver.get("/api/approvals?status=denied")).json()
    assert [i["run_id"] for i in resolved] == [run_op] and resolved[0]["can_respond"] is False
    assert {i["run_id"] for i in (await approver.get("/api/approvals?status=all")).json()} >= {run_op, run_admin}
    assert (await approver.get("/api/approvals?status=bogus")).status_code == 422
    await approver.post(f"/api/runs/{run_admin}/respond", json=answer({"request_id": views["owner"][run_admin]["id"]}, False))
    await finished(admin, run_admin)


def _pending_ids(client, expected: set):
    async def check() -> bool:
        return {i["run_id"] for i in (await client.get("/api/approvals")).json()} == expected

    return check


async def test_questions_appear_in_the_inbox_for_roles_that_can_answer_them(world, db_app):
    app, planner = db_app(**QUICK)
    planner.add(reply("I need to know.", call("ask_user", question="Which cost center?")), blocked())
    op = await sign_in(app, "operator@acme.test")
    run_id = await start_run(op)
    await pending_request(op, run_id)
    viewer, approver = await sign_in(app, "viewer@acme.test", worker=False), await sign_in(app, "approver@acme.test", worker=False)
    (seen,) = (await viewer.get("/api/approvals")).json()
    assert seen["kind"] == "question" and seen["action"] == "Which cost center?" and seen["can_respond"] is False
    assert "Viewers cannot answer" in seen["reason"]
    (mine,) = (await approver.get("/api/approvals")).json()
    assert mine["can_respond"] is True and mine["can_approve"] is True
    done = await approver.post(f"/api/runs/{run_id}/respond", json={"request_id": mine["id"], "answer": "CC-7"})
    assert done.status_code == 200 and done.json()["kind"] == "question"
    await finished(op, run_id)
    (row,) = await approval_rows(world.sm, run_id)
    assert row["status"] == "answered" and row["response"]["answer"] == "CC-7"


async def test_the_inbox_stream_pushes_a_change_when_an_approval_is_raised_or_resolved(world, db_app):
    app, planner = db_app(**QUICK)
    planner.add(*write_script())
    op = await sign_in(app, "operator@acme.test")
    approver = await sign_in(app, "approver@acme.test", worker=False)
    other = await sign_in(app, "approver@globex.test", worker=False)

    anonymous = await ws_connect(app, "/api/approvals/stream", timeout=1.0)
    assert not anonymous.accepted and anonymous.close_code == 1008
    mine = asyncio.create_task(ws_connect(app, "/api/approvals/stream", session_cookie(approver), timeout=3.5))
    theirs = asyncio.create_task(ws_connect(app, "/api/approvals/stream", session_cookie(other), timeout=3.5))
    await wait_until(lambda: app.state.hub is not None and sum(len(q) for q in app.state.hub._org_watchers.values()) == 2, 5)
    run_id = await start_run(op)
    request = await pending_request(op, run_id)
    await asyncio.sleep(0.5)
    await approver.post(f"/api/runs/{run_id}/respond", json=answer(request))
    mine, theirs = await mine, await theirs
    assert mine.types[0] == "hello" and mine.types.count("changed") >= 2  # raised, then answered
    assert theirs.types == ["hello"]  # nothing leaks to another org
    await finished(op, run_id)


# --------------------------------------------------------------------------- live events across processes


def seqs(result) -> list[int]:
    return [m["seq"] for m in result.messages if m["seq"] > 0]


async def test_a_websocket_gets_the_events_a_worker_produces_with_replay_and_no_gap_or_duplicate(world, db_app):
    gates = Gates()
    app, planner = db_app(**QUICK, registry_factory=registry_with(gates))
    planner.add(reply("Look.", call("list_files")), reply("Wait.", call("hold", gate="mid")), reply("Again.", call("list_files")), blocked())
    app_b, _ = db_app(**QUICK)  # the WebSockets connect to a different API process than the one that queued the run
    op = await sign_in(app, "operator@acme.test")
    run_id = await start_run(op)
    assert await gates.reached("mid")
    watcher = await sign_in(app_b, "viewer@acme.test", worker=False)
    early = asyncio.create_task(ws_connect(app_b, f"/api/runs/{run_id}/events", session_cookie(watcher), timeout=15))
    await wait_until(lambda: run_id in app_b.state.hub._feeds, 5)
    await asyncio.sleep(0.3)
    gates.gate("mid").set()
    early = await early  # the server closes the socket after run_finished
    late = await ws_connect(app_b, f"/api/runs/{run_id}/events", session_cookie(watcher), timeout=5)  # replay only

    stored = await PgRunStore(world.sm, world.org_ids["acme"]).read_events(run_id)
    assert seqs(early) == [e["seq"] for e in stored] == list(range(1, len(stored) + 1))  # all of it, once, in order
    assert seqs(late) == seqs(early) and early.messages[-1]["type"] == "run_finished"
    marker = early.types.index("replay_done")
    assert early.messages[marker]["payload"] == {"live": True} and 0 < marker < len(early.messages) - 1  # some replayed, some live
    assert late.messages[-1]["type"] == "replay_done" and late.messages[-1]["payload"] == {"live": False}
    assert app_b.state.hub._feeds == {}  # nobody watches any more


async def test_the_live_stream_survives_a_dropped_listen_connection(world, db_app):
    app, _ = db_app(**QUICK)
    op = await sign_in(app, "operator@acme.test", worker=False)
    run_id = await start_run(op, "Watched while the connection drops")
    store = PgRunStore(world.sm, world.org_ids["acme"])
    await store.append_event(run_id, "thought", {"run_id": run_id, "text": "before"})
    hub = app.state.hub
    watcher = asyncio.create_task(ws_connect(app, f"/api/runs/{run_id}/events", session_cookie(op), timeout=15))
    assert await wait_until(lambda: run_id in hub._feeds and hub.listener.connected, 5)

    await store.append_event(run_id, "thought", {"run_id": run_id, "text": "live, delivered by NOTIFY"})
    assert await wait_until(lambda: hub._feeds[run_id].cursor == 3, 5)
    pid = hub.listener.backend_pid
    async with world.sm() as session, session.begin():  # the server drops the listening connection ...
        await session.execute(text("select pg_terminate_backend(:pid)"), {"pid": pid})
    for n in range(3):  # ... events are stored meanwhile, their NOTIFYs are lost ...
        await store.append_event(run_id, "thought", {"run_id": run_id, "text": f"while down {n}"})
    assert await wait_until(lambda: hub.listener.connects == 2 and hub.listener.connected, 10)  # ... it reconnects ...
    await store.append_event(run_id, "run_finished", {"run_id": run_id, "status": "cancelled", "report": {}})
    result = await watcher

    texts = [m["payload"].get("text") for m in result.messages if m["type"] == "thought"]
    assert texts == ["before", "live, delivered by NOTIFY", "while down 0", "while down 1", "while down 2"]  # ... and catches up
    assert seqs(result) == list(range(1, 8)) and result.messages[-1]["type"] == "run_finished"


async def test_the_hub_only_serves_runs_of_the_callers_org(world, db_app):
    app, _ = db_app(**QUICK)
    op = await sign_in(app, "operator@acme.test", worker=False)
    run_id = await start_run(op)
    stranger = await sign_in(app, "operator@globex.test", worker=False)
    result = await ws_connect(app, f"/api/runs/{run_id}/events", session_cookie(stranger), timeout=3)
    assert result.types == ["error"] and result.messages[0]["payload"] == {"message": "Unknown run"}
    assert app.state.hub._feeds == {}
