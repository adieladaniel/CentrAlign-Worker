"""The job queue and worker processes of database mode (``worker/jobqueue.py``, ``worker/jobs.py``).

Workers run in-process as asyncio tasks (several of them stand in for several processes: they share nothing but the
database) with scripted LLMs and fake tools; the API is driven over ``httpx.ASGITransport``. Needs the test database.
"""
from __future__ import annotations

import asyncio
import uuid
from collections import Counter

from dbapp import (
    Gates,
    Planner,
    blocked,
    call,
    event_types,
    finished,
    kill_worker,
    new_worker,
    registry_with,
    reply,
    run_row,
    sign_in,
    start_run,
    wait_until,
)
from sqlalchemy import text

from worker import jobqueue, metrics
from worker.agent.prompts import RESUME_NOTE
from worker.llm.fake import ScriptedLLM
from worker.store_pg import PgRunStore
from worker.tools.base import Tool, ToolContext, ToolResult


def fast(**overrides):
    """Settings that make polling and the reaper quick enough for tests. The lease stays generous: a loaded test machine
    must not make a healthy worker look dead (tests about dead workers shorten it themselves)."""
    return {"JOB_LEASE_S": 5.0, "JOB_POLL_S": 0.2, "JOB_REAPER_INTERVAL_S": 0.2, **overrides}


async def until_run(sm, run_id: str, **expected):
    """Wait until the run row shows the expected column values."""
    last: dict = {}

    async def matches() -> bool:
        last.update(await run_row(sm, run_id))
        return all(last[k] == v for k, v in expected.items())

    assert await wait_until(matches, 15), f"run {run_id[:8]} is {last}, expected {expected}"
    return last


# --------------------------------------------------------------------------- enqueue, claim, finish


async def test_enqueue_only_queues_and_a_worker_claims_and_finishes_the_run(world, db_app):
    app, _ = db_app(**fast())
    operator = await sign_in(app, "operator@acme.test", worker=False)
    run_id = await start_run(operator, "Queue me")

    await asyncio.sleep(0.5)  # no worker exists: the API process executes nothing
    assert await run_row(world.sm, run_id) == {**await run_row(world.sm, run_id), "status": "queued", "attempts": 0,
                                               "lease_owner": None}
    row = (await operator.get("/api/runs")).json()[0]
    assert row["status"] == "queued" and row["queue_position"] == 1 and row["active"] is True
    assert "worker" not in row  # who holds a run is for admins
    assert await event_types(world.sm, run_id) == ["run_queued"]

    worker = new_worker(app, "w-solo")
    await worker.start()
    detail = await finished(operator, run_id)
    assert detail["status"] == "needs_attention" and detail["queue_position"] is None
    row = await run_row(world.sm, run_id)
    assert (row["attempts"], row["lease_owner"], row["resume_requested"]) == (1, None, False)  # lease released
    types = await event_types(world.sm, run_id)
    assert types[:2] == ["run_queued", "run_started"] and types[-1] == "run_finished"
    assert worker.claimed == [run_id]
    admin = await sign_in(app, "admin@acme.test", worker=False)
    assert "worker" in (await admin.get("/api/runs")).json()[0]


async def test_two_workers_and_six_runs_run_each_exactly_once_within_the_org_limit(world, db_app):
    gates = Gates()
    active: Counter = Counter()
    peak: Counter = Counter()

    class Probe(Tool):
        name = "probe"
        description = "Does some work and records how many runs of the org work at the same time."
        parameters = {"type": "object", "properties": {}}

        async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
            org = ctx.extras["vault_scope"].owner
            active[org] += 1
            peak[org] = max(peak[org], active[org])
            await asyncio.sleep(0.4)
            active[org] -= 1
            return ToolResult(ok=True, observation="worked")

    def llm_factory(_settings):
        steps = iter([reply("work", call("probe")), blocked()])
        return ScriptedLLM(lambda messages, tools: next(steps))

    app, _ = db_app(**fast(), registry_factory=registry_with(gates, Probe()))
    app.state.llm_factory = llm_factory
    acme = await sign_in(app, "operator@acme.test", worker=False)
    globex = await sign_in(app, "operator@globex.test", worker=False)
    admin = await sign_in(app, "admin@acme.test", worker=False)
    assert (await admin.put("/api/admin/settings", json={"max_concurrent_runs": 2})).status_code == 200

    acme_runs = [await start_run(acme, f"acme task {n}") for n in range(6)]
    globex_runs = [await start_run(globex, f"globex task {n}") for n in range(2)]
    workers = [new_worker(app, "w-a", concurrency=4), new_worker(app, "w-b", concurrency=4)]
    for worker in workers:
        await worker.start()
    for client, ids in ((acme, acme_runs), (globex, globex_runs)):
        for run_id in ids:
            await finished(client, run_id, timeout=40)

    claimed = [rid for worker in workers for rid in worker.claimed]
    assert sorted(claimed) == sorted(acme_runs + globex_runs)  # every run claimed once, by exactly one worker
    for run_id in acme_runs + globex_runs:
        assert (await run_row(world.sm, run_id))["attempts"] == 1
        assert (await event_types(world.sm, run_id)).count("run_started") == 1
    acme_org, globex_org = str(world.org_ids["acme"]), str(world.org_ids["globex"])
    assert peak[acme_org] == 2  # it did run in parallel ...
    assert peak[globex_org] >= 1 and max(peak.values()) <= 2  # ... but never above the org's limit
    assert all(count == 0 for count in active.values())


async def test_the_best_queued_run_goes_first(world, db_app):
    app, _ = db_app(**fast())
    operator = await sign_in(app, "operator@acme.test", worker=False)
    first, second, urgent = [await start_run(operator, f"task {n}") for n in range(3)]
    async with world.sm() as session, session.begin():
        await session.execute(text("update runs set priority = 5 where id = :id"), {"id": uuid.UUID(urgent)})
    positions = {r["id"]: r["queue_position"] for r in (await operator.get("/api/runs")).json()}
    assert positions == {urgent: 1, first: 2, second: 3}
    worker = new_worker(app, "w-one", concurrency=1)  # one slot: runs are taken strictly in queue order
    await worker.start()
    for run_id in (first, second, urgent):
        await finished(operator, run_id)
    assert worker.claimed == [urgent, first, second]


# --------------------------------------------------------------------------- quota


async def test_the_daily_quota_is_never_exceeded_by_concurrent_enqueues(world, db_app):
    app, _ = db_app(**fast())
    admin = await sign_in(app, "admin@acme.test", worker=False)
    assert (await admin.put("/api/admin/settings", json={"max_runs_per_day": 4})).status_code == 200
    clients = [await sign_in(app, "operator@acme.test", worker=False), await sign_in(app, "operator2@acme.test", worker=False)]
    responses = await asyncio.gather(*[clients[n % 2].post("/api/runs", json={"task": f"burst {n}"}) for n in range(10)])
    codes = Counter(r.status_code for r in responses)
    assert codes == {200: 4, 429: 6}, codes
    assert all("limit of 4 runs" in r.json()["detail"] and int(r.headers["retry-after"]) > 0 for r in responses if r.status_code == 429)
    async with world.sm() as session:
        stored = await session.scalar(text("select count(*) from runs where org_id = :org"), {"org": world.org_ids["acme"]})
    assert stored == 4  # queued runs count; nothing slipped past the limit
    config = (await clients[0].get("/api/config")).json()
    assert (config["max_runs_per_day"], config["runs_left_today"]) == (4, 0)


# --------------------------------------------------------------------------- crashes: lease, reaper, resume


def script_for(*steps):
    """A ``llm_factory`` whose k-th LLM plays the k-th script (a list of LLMResponse)."""
    planner = Planner()
    for step in steps:
        planner.add(*step)
    return planner


async def test_a_lost_worker_is_noticed_and_another_worker_resumes_the_run_from_its_checkpoint(world, db_app):
    gates = Gates()
    planner = script_for(
        [reply("Look around.", call("list_files")), reply("Wait for the colleague.", call("hold", gate="never"))],
        [reply("Back again, look around.", call("list_files")), blocked()])
    app, _ = db_app(**fast(JOB_LEASE_S=1.5), registry_factory=registry_with(gates))
    app.state.llm_factory = planner
    operator = await sign_in(app, "operator@acme.test", worker=False)
    run_id = await start_run(operator, "Survive a crash")

    doomed = new_worker(app, "w-doomed")
    await doomed.start()
    assert await gates.reached("never")
    before = (await operator.get(f"/api/runs/{run_id}")).json()
    assert before["status"] == "running" and [s["tool"] for s in before["steps"]] == ["list_files"]
    assert (await run_row(world.sm, run_id))["lease_owner"] == "w-doomed"

    await kill_worker(doomed)  # the process dies: no release, no hand-back
    assert (await run_row(world.sm, run_id))["lease_owner"] == "w-doomed"
    rescuer = new_worker(app, "w-rescuer")
    await rescuer.start()
    detail = await finished(operator, run_id, timeout=20)

    steps = detail["steps"]
    assert [s["index"] for s in steps] == list(range(1, len(steps) + 1))  # continuous: nothing lost, nothing repeated
    assert steps[0]["tool"] == "list_files"  # the first worker's work is kept
    assert steps[1]["kind"] == "system" and steps[1]["error"] == "resumed" and steps[1]["thought"] == RESUME_NOTE
    assert [s["tool"] for s in steps[2:]] == ["list_files", "finish"]
    assert [s["turn"] for s in steps] == sorted(s["turn"] for s in steps)  # the turn counter carried on
    row = await run_row(world.sm, run_id)
    assert (row["attempts"], row["resume_count"], row["lease_owner"]) == (2, 1, None)
    types = await event_types(world.sm, run_id)
    assert types.count("run_requeued") == 1 and types.count("run_resumed") == 1 and types.count("run_started") == 1
    assert types.index("run_requeued") < types.index("run_resumed") and types[-1] == "run_finished"
    store = PgRunStore(world.sm, world.org_ids["acme"])
    requeued = next(e for e in await store.read_events(run_id) if e["type"] == "run_requeued")
    assert requeued["payload"]["worker"] == "w-doomed" and requeued["payload"]["reason"] == "worker_lost"
    assert rescuer.claimed == [run_id] and doomed.claimed == [run_id]
    resumed_prompt = "\n".join(m.content for m in planner.created[1].calls[0]["messages"])
    assert RESUME_NOTE in resumed_prompt  # the new agent is told to re-check the world


async def test_a_run_that_keeps_losing_its_worker_ends_as_needs_attention(world, db_app):
    gates = Gates()
    planner = script_for(*[[reply("Wait.", call("hold", gate="never"))]] * 2)
    app, _ = db_app(**fast(JOB_MAX_ATTEMPTS=2, JOB_LEASE_S=1.5), registry_factory=registry_with(gates))
    app.state.llm_factory = planner
    operator = await sign_in(app, "operator@acme.test", worker=False)
    run_id = await start_run(operator, "Doomed twice")

    first = new_worker(app, "w-1")
    await first.start()
    assert await gates.reached("never", 1)
    await kill_worker(first)
    second = new_worker(app, "w-2")
    await second.start()
    assert await gates.reached("never", 2, timeout=20)  # requeued after attempt 1 (1 < 2), claimed again
    assert (await run_row(world.sm, run_id))["attempts"] == 2
    await kill_worker(second)
    third = new_worker(app, "w-3")  # only its reaper is needed: attempts (2) are used up
    await third.start()
    detail = await finished(operator, run_id, timeout=20)

    assert detail["status"] == "needs_attention" and "lost too many times" in detail["report"]["summary"]
    assert third.claimed == []
    row = await run_row(world.sm, run_id)
    assert row["lease_owner"] is None and row["finished_at"] is not None
    assert (await event_types(world.sm, run_id))[-1] == "run_finished"


async def test_a_heartbeat_that_finds_the_lease_gone_cancels_the_local_run_and_stops_its_writes(world, db_app):
    gates = Gates()
    app, _ = db_app(**fast(JOB_LEASE_S=0.6), registry_factory=registry_with(gates))
    app.state.llm_factory = script_for([reply("Wait.", call("hold", gate="held"))])
    operator = await sign_in(app, "operator@acme.test", worker=False)
    run_id = await start_run(operator)
    worker = new_worker(app, "w-slow")
    await worker.start()
    assert await gates.reached("held")
    handle = worker.handles[run_id]
    events_before = len(await event_types(world.sm, run_id))

    async with world.sm() as session, session.begin():  # someone else took the run (e.g. after a long pause of this worker)
        await session.execute(text("update runs set lease_owner = 'intruder' where id = :id"), {"id": uuid.UUID(run_id)})
    assert await wait_until(lambda: handle.lost and handle.task.done(), 10)
    assert handle.runner._cancelled.is_set() and run_id not in worker.handles

    row = await run_row(world.sm, run_id)  # the doomed local runner wrote nothing: not a status, not a lease, not an event
    assert (row["status"], row["lease_owner"], row["finished_at"]) == ("running", "intruder", None)
    assert len(await event_types(world.sm, run_id)) == events_before


async def test_a_heartbeat_that_lands_just_after_the_run_ended_is_not_a_lost_lease(world, db_app, monkeypatch):
    """Found by the load test (about 1 run in 60): the runner saves the final status, then the worker still writes the
    report's last events and the organization memory before it stops the heartbeat. A beat inside that window used to
    find the run 'not active', declare the lease lost, fence the writes, cancel the task and skip the memory commit."""
    committed: list[str] = []

    async def slow_commit(memory, state, **kwargs):
        await asyncio.sleep(1.2)  # many heartbeats (the lease is 0.6 s) happen while the finished run is being wrapped up
        committed.append(state.run_id)

    monkeypatch.setattr("worker.jobs.commit_run_memory", slow_commit)
    lost_before = metrics.sample("oneforall_worker_leases_lost_total")
    app, _ = db_app(**fast(JOB_LEASE_S=0.6))
    app.state.llm_factory = script_for([blocked()])
    operator = await sign_in(app, "operator@acme.test", worker=False)
    run_id = await start_run(operator)
    worker = new_worker(app, "w-late-beat")
    await worker.start()

    await until_run(world.sm, run_id, status="needs_attention", lease_owner=None)  # finished AND the lease was released
    assert committed == [run_id]  # the memory commit ran to the end instead of being cancelled
    assert not worker.recent[run_id].lost and metrics.sample("oneforall_worker_leases_lost_total") == lost_before


async def test_heartbeat_distinguishes_a_finished_run_from_a_lost_lease(world, db_app):
    app, _ = db_app(**fast())
    operator = await sign_in(app, "operator@acme.test", worker=False)
    run_id = await start_run(operator)
    sm, rid = world.sm, uuid.UUID(run_id)

    async def set_run(**columns):
        sets = ", ".join(f"{k} = :{k}" for k in columns)
        async with sm() as session, session.begin():
            await session.execute(text(f"update runs set {sets} where id = :id"), {"id": rid, **columns})

    beat = lambda who="w1": jobqueue.heartbeat(sm, run_id, who, lease_s=30)  # noqa: E731
    assert await beat() is None  # queued: nobody holds it
    await set_run(status="running", lease_owner="w1")
    assert await beat() is False and await beat("w2") is None  # active: extended for its owner only
    await set_run(status="succeeded")
    assert await beat() is False  # over, still leased by us: not lost
    assert await beat("w2") is None  # over, but leased by somebody else: not ours
    await set_run(lease_owner=None)
    assert await beat() is None  # the reaper clears the owner when it re-queues or gives up on a run: that IS lost
    await set_run(status="queued")
    assert await beat() is None  # re-queued behind a slow worker's back
    assert await jobqueue.heartbeat(sm, uuid.uuid4().hex, "w1", lease_s=30) is None  # unknown run


# --------------------------------------------------------------------------- cancel


async def test_cancel_through_the_api_reaches_the_worker_by_notify(world, db_app):
    gates = Gates()
    app, _ = db_app(**fast(JOB_LEASE_S=60), registry_factory=registry_with(gates))  # heartbeats are far away: only NOTIFY can be fast
    app.state.llm_factory = script_for([reply("Wait.", call("hold", gate="held")), blocked()])
    operator = await sign_in(app, "operator@acme.test", worker=False)
    worker = new_worker(app, "w-listen")
    await worker.start()
    run_id = await start_run(operator)
    assert await gates.reached("held")

    resp = await operator.post(f"/api/runs/{run_id}/cancel")
    assert resp.status_code == 200 and resp.json() == {"status": "cancelling"}
    assert (await run_row(world.sm, run_id))["cancel_requested"] is True
    gates.gate("held").set()  # the tool ends; the runner sees the cancellation at its next safe point
    detail = await finished(operator, run_id, timeout=5)
    assert detail["status"] == "cancelled"
    assert (await operator.post(f"/api/runs/{run_id}/cancel")).status_code == 409  # finished: nothing to cancel


async def test_cancelling_a_queued_run_ends_it_at_once_and_no_worker_ever_takes_it(world, db_app):
    app, _ = db_app(**fast())
    operator = await sign_in(app, "operator@acme.test", worker=False)
    run_id = await start_run(operator, "Never mind")
    assert (await operator.post(f"/api/runs/{run_id}/cancel")).json() == {"status": "cancelled"}
    detail = (await operator.get(f"/api/runs/{run_id}")).json()
    assert detail["status"] == "cancelled" and detail["active"] is False and detail["report_url"]
    assert "waiting in the queue" in detail["report"]["summary"]
    assert (await event_types(world.sm, run_id))[-1] == "run_finished"
    worker = new_worker(app, "w-late")
    await worker.start()
    await asyncio.sleep(0.6)
    assert worker.claimed == [] and (await run_row(world.sm, run_id))["attempts"] == 0
    other = await sign_in(app, "operator@globex.test", worker=False)  # another org's run is simply unknown
    assert (await other.post(f"/api/runs/{run_id}/cancel")).status_code == 404


# --------------------------------------------------------------------------- shutdown


async def test_graceful_shutdown_hands_running_runs_back_and_another_worker_continues_them(world, db_app):
    gates = Gates()
    planner = script_for([reply("Look.", call("list_files")), reply("Wait.", call("hold", gate="held"))],
                         [reply("Continue.", call("list_files")), blocked()])
    app, _ = db_app(**fast(), registry_factory=registry_with(gates))
    app.state.llm_factory = planner
    operator = await sign_in(app, "operator@acme.test", worker=False)
    run_id = await start_run(operator, "Hand me over")
    leaving = new_worker(app, "w-leaving")
    await leaving.start()
    assert await gates.reached("held")

    await leaving.stop()  # Ctrl+C: stop claiming, hand the running run back
    row = await run_row(world.sm, run_id)
    assert (row["status"], row["lease_owner"], row["resume_requested"], row["attempts"]) == ("queued", None, True, 0)
    assert leaving.handles == {}
    assert (await event_types(world.sm, run_id))[-1] == "run_requeued"
    requeued = (await PgRunStore(world.sm, world.org_ids["acme"]).read_events(run_id))[-1]
    assert requeued["payload"]["reason"] == "worker shutting down" and requeued["payload"]["worker"] == "w-leaving"

    staying = new_worker(app, "w-staying")
    await staying.start()
    detail = await finished(operator, run_id, timeout=15)
    steps = detail["steps"]
    assert [s["index"] for s in steps] == list(range(1, len(steps) + 1)) and steps[0]["tool"] == "list_files"
    assert steps[1]["error"] == "resumed" and [s["tool"] for s in steps[2:]] == ["list_files", "finish"]
    assert (await run_row(world.sm, run_id))["attempts"] == 1  # handing a run back does not use up an attempt


async def test_draining_lets_the_running_run_finish_before_the_worker_stops(world, db_app):
    gates = Gates()
    app, _ = db_app(**fast(), registry_factory=registry_with(gates))
    app.state.llm_factory = script_for([reply("Wait.", call("hold", gate="held")), blocked()])
    operator = await sign_in(app, "operator@acme.test", worker=False)
    worker = new_worker(app, "w-drain")
    await worker.start()
    run_id = await start_run(operator)
    assert await gates.reached("held")

    stopping = asyncio.create_task(worker.stop(drain=True))
    await asyncio.sleep(0.5)
    assert not stopping.done()  # it waits for the run
    queued_later = await start_run(operator, "arrives while draining")
    gates.gate("held").set()
    await asyncio.wait_for(stopping, 10)
    assert (await finished(operator, run_id))["status"] == "needs_attention"
    assert (await run_row(world.sm, queued_later))["status"] == "queued"  # a draining worker claims nothing new


async def test_a_worker_shuts_down_cleanly_with_nothing_to_do(world, db_app):
    app, _ = db_app(**fast())
    worker = new_worker(app, "w-idle")
    await worker.start()
    assert worker.listener.connected
    await asyncio.wait_for(worker.stop(), 5)
    assert not worker.listener.connected and worker.handles == {}
