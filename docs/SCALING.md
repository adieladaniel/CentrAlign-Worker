# Scaling: what was tested, what it showed, what limits it

This page reports a real load test of the multi-user stack on one machine, with the numbers it produced. Nothing here is
extrapolated except where a sentence says so. The raw results are in [loadtest-results.json](loadtest-results.json) (summary
tables in [loadtest-results.md](loadtest-results.md)); both are written by [`scripts/loadtest.py`](../scripts/loadtest.py).

## What was tested

```
 simulated people (asyncio, real HTTP logins + WebSockets)
   operators ──POST /api/runs──┐   approvers ──/api/approvals + stream, respond──┐   viewers ──run pages, event streams, probes──┐
                                ▼                                                 ▼                                              ▼
                       API process (uvicorn, port 8601) ─────────────── PostgreSQL 18 (database oneforall_load, dropped afterwards)
                                                                          ▲  job queue (SKIP LOCKED + leases), approvals, events, usage, memory
                       worker 1 ... worker N (python -m worker.jobs --concurrency K, metrics on 9601+)
                         └─ one Chromium each, one isolated browser context per run
                                          ▼
                       simulated company (port 8602): one isolated tenant (SQLite) per org
```

* **Model.** `LLM_PROVIDER=policy`: [`worker/llm/policy.py`](../worker/llm/policy.py) is a deterministic, rule-based stand-in for
  a model. It reads the same prompt a model would (page snapshots such as `[7] button "Save bill"`, the working memory) and
  drives the **real** tools: vault logins (`browser_fill_secret`), the portal, the PDF, the ERP form, the approval gate on
  "Save bill", re-opening the bill, `remember` (facts plus one durable how-to note), `finish`, and the independent audit
  (the same class answers as the auditor). Every run therefore exercises the queue, browser pool, vault, approvals, events,
  organization memory and verification for real. No Gemini quota was used.
  Each call sleeps `--think-ms` (800 ms, jittered +-40%) to imitate model latency, passes through the shared LLM rate limiter
  (one token bucket for all runs and processes, like one API key) and is recorded as usage with token counts estimated from the text.
* **Work.** Per org (its own simulated company) a mix of "find the latest invoice from *vendor*, enter it in the ERP" and
  "find invoice *NUMBER* ..., enter it", distinct targets first, then repeats (`--dup-rate 0.1` of the runs repeat an earlier
  task of the same org, so "the bill already exists" and "somebody is creating it right now" both happen).
  A run is about 14 to 16 LLM calls, 24 to 33 tool calls (auditor included), one approval (a retried save needs a second) and one independent audit.
* **People.** Operators submit at a Poisson arrival rate (0.5 to 1 run/s, faster than the stack can serve, so queues build).
  Approvers of the *same* org watch their inbox (push stream plus 2 s polling) and approve after a random human delay of
  0.5 to 3 s; separation of duties is on, so nobody approves their own run. Viewers open run pages and two live WebSocket
  event streams per run, and every operator, approver and viewer keeps probing the *other* orgs' runs.
* **Chaos.** `--chaos-rate` of the runs get a flag armed in their org's company while they execute: a one-time HTTP 500 on the
  ERP form, an expiring ERP session, or both. Flags belong to a company, so the fault hits whichever run of that org reaches the
  form (or its fourth page) first. The retried save needs a second approval.
* **Failure injection.** `--kill-worker-at 50%` hard-kills worker 1 and its browsers (a crash) when half of the runs have
  finished; its runs wait for the lease to expire (15 s here), are re-queued by another worker's reaper and **resume from
  their checkpoint** in a fresh browser. `--restart-api-at` hard-restarts the API.
* **Judging.** Success is not what the agent says. After the run each org's ERP is read through the simulator's admin API and
  compared with the invoices the org's tasks asked for (the `evals/checkers.py` logic): a succeeded run counts only if exactly
  one bill exists for its invoice and every field matches. Isolation is checked in two ways: every response and
  WebSocket frame that any simulated user receives is scanned for another org's ids, run ids, e-mail domain and tenant name,
  and explicit cross-org probes (read a foreign run, its report, its event stream, answer or cancel it) must all be refused.

## Hardware and software

| | |
|---|---|
| Machine | one Windows 11 Pro laptop/desktop (10.0.22621) running everything: database, API, workers, browsers, simulated company **and the load generator**, plus whatever else the developer had open |
| CPU | Intel64 Family 6 Model 154 (12th gen Core), 10 physical / 16 logical cores |
| RAM | 15.7 GB total, about 5.6 GB free when the tests started (peak use 10.7 to 11.4 GB of the whole machine) |
| Software | Python 3.11.9, PostgreSQL 18 (local, default settings), Playwright 1.63 headless Chromium, SQLAlchemy asyncpg pool 10+10 per process |
| Settings | per-org limit `max_concurrent_runs` 4, run quota and token/call quotas unlimited, `JOB_LEASE_S` 15, `JOB_REAPER_INTERVAL_S` 5, shared LLM limit 25 calls/s (burst 25) unless the profile says otherwise |

## Profiles and results

| Profile | Command (all start with `python scripts/loadtest.py`) |
|---|---|
| **baseline** | `--profile baseline --orgs 3 --users 4 --runs 30 --workers 2 --concurrency 4` |
| **stress** (stress + recovery) | `--profile stress --orgs 5 --users 4 --runs 60 --workers 3 --concurrency 4 --chaos-rate 0.2 --kill-worker-at 50%` |
| **soak** | `--profile soak --orgs 6 --users 5 --runs 280 --workers 3 --concurrency 4 --chaos-rate 0.25 --arrival-rate 1.0 --kill-worker-at 30% --respawn-after-s 10 --restart-api-at 55%` |
| **llm-bound** | `--profile llm-bound --orgs 3 --runs 24 --workers 2 --concurrency 4 --llm-rate 2 --llm-burst 2` |
| leakcheck | `--profile leakcheck --orgs 4 --users 4 --runs 200 --workers 2 --concurrency 4 --arrival-rate 1.0` |

The machine has 15.7 GB of RAM and had enough free memory for the full stress profile (3 workers x 4 browser contexts), so nothing was scaled down.

### Outcomes and correctness

| | baseline | stress | soak | llm-bound |
|---|---|---|---|---|
| Runs submitted / succeeded | 30 / 30 | 60 / 60 | 280 / 280 | 24 / 24 |
| Ground-truth pass (exactly one faithful bill) | 30/30 | 60/60 | 280/280 | 24/24 |
| False successes, duplicate bills, stray bills | 0, 0, 0 | 0, 0, 0 | 0, 0, 0 | 0, 0, 0 |
| Chaos-flagged runs / runs whose save was retried (second approval) | 0 / 0 | 10 / 6 | 81 / 17 | 0 / 0 |
| Worker killed (runs in flight) | no | yes (4) -> all 4 resumed and succeeded | yes (4) -> all 4 succeeded | no |
| API restarted | no | no | yes, back after 1.8 s | no |
| Isolation: responses scanned / leaks | 7,355 / 0 | 14,970 / 0 | 47,508 / 0 | 6,878 / 0 |
| Cross-org probes / not refused | 311 / 0 | 825 / 0 | 4,050 / 0 | 674 / 0 |
| WebSocket streams / events out of order | 60 / 0 | 120 / 0 | 560 / 0 (180 dropped, see below) | 48 / 0 |
| Org concurrency cap exceeded (samples) | 0 | 0 | 0 | 0 |
| Deadlocks | 0 | 0 | 0 | 0 |

The soak's 180 dropped WebSocket streams are the event streams that were open when the API was killed; they are the expected result of the restart (the other 380 ended with `run_finished`).

### Timings (seconds)

| | baseline | stress | soak | llm-bound |
|---|---|---|---|---|
| End to end, submit to done: p50 / p95 / max | 25.6 / 36.0 / 42.3 | 34.1 / 62.9 / 69.3 | 97.1 / 124.4 / 127.9 | 103.6 / 153.0 / 154.0 |
| Execution (claimed to done): p50 / p95 | 21.2 / 24.0 | 21.1 / 39.1 | 16.1 / 27.2 | 58.0 / 76.5 |
| Queue wait: p50 / p95 | 6.0 / 17.0 | 12.5 / 41.7 | 81.1 / 108.5 | 45.4 / 97.9 |
| Approval wait (request to answer): p50 / p95 | 1.46 / 2.76 | 1.66 / 2.80 | 1.71 / 2.94 | 1.65 / 2.84 |
| of which the simulated human's delay: p50 | 1.44 | 1.64 | 1.67 | 1.62 |
| Throughput, runs/min finished | 19.7 | 25.5 | 38.8 | 7.2 |
| Execution slots (workers x concurrency) | 8 | 12 | 12 | 8 |

The approval wait is almost exactly the human's think time: the platform's own part (notification to the approver's inbox, the
answer, the NOTIFY back to the waiting run) is 0.02 to 0.07 s at p50/p95, and the approver saw a new request within 10 to 20 ms of its creation. Queue wait in the soak is large on purpose: runs arrived at 1 per second while the stack finishes
0.65 per second.

### Resources

| | baseline | stress | soak | llm-bound |
|---|---|---|---|---|
| Worker process tree (Python + Chromium) peak, per worker | 925 / 838 MB | 860 / 957 / 947 MB | 979 / 999 / 901 MB | 915 / 811 MB |
| Memory per open browser context (regression of RSS on active contexts) | 134 to 149 MB | 140 to 165 MB | 114 to 131 MB | 117 to 144 MB |
| API process peak | 137 MB | 144 MB | 165 MB | 137 MB |
| Simulated company peak | 75 MB | 77 MB | 86 MB | 75 MB |
| Machine CPU, average / peak (16 logical cores) | 2.7% / 14% | 3.0% / 18% | 3.5% / 17% | 2.5% / 12% |
| PostgreSQL: connections (max seen) / transactions per s | 21 / 245 | 25 / 357 | 35 / 491 | 24 / 121 |
| LLM calls (per run) / peak calls per s of the limit | 479 (16.0) / 6.4 of 25 | 963 (16.1) / 9.6 of 25 | 4,023 (14.4) / 10.4 of 25 | 384 (16.0) / 2.1 of 2 |
| LLM rate-limiter wait: p50 / p95 | 0.001 / 0.074 | 0.001 / 0.073 | 0.001 / 0.077 | 1.2 / 9.3 |
| Tokens (in / out), estimated | 2.35M / 46k | 4.72M / 93k | 18.8M / 399k | 1.89M / 37k |

A worker with four runs open uses about 0.9 to 1.0 GB, so on this 16 GB machine 3 workers x 4 contexts fit with room to spare
and the practical ceiling is about 8 to 10 workers x 4 contexts before the machine itself, not the software, runs out of memory.
That figure is an estimate from the per-worker numbers above, not a measurement.

Memory over time (`leakcheck`, 200 runs in 7.4 minutes on 2 workers x 4): a regression of each worker's memory on
its open contexts and on time shows a drift of about 13.7 MB/min (second half 9 to 10 MB/min); the Python process alone
drifts about 3 MB/min and the browsers the rest. The soak (3 workers, 7.2 minutes) shows 13.5 to 16.5 MB/min. That is roughly
0.4 MB per run, small, and could be Chromium's caches, but a 7-minute test cannot tell a leak from a plateau. The
drift figures of the short profiles (baseline, stress) are warm-up noise and should be ignored. Before a long-running
deployment, run a soak of an hour or more; the cheap mitigation if it is a real leak is to recycle a worker's Chromium
after some number of contexts.

## What limits throughput

1. **Execution slots, with a cheap model.** In the soak the 12 slots were busy the whole time (queue never empty) while the
   machine used 3.5% CPU, the LLM limiter was 60% idle (10.4 of 25 calls/s), the database ran 491 transactions per
   second with 35 connections and no deadlock, and the API answered `POST /api/runs` in 16 ms at p50 (47 ms p95). Throughput was
   `slots x 60 / (execution time + about 1 s of claim and wrap-up)`: 12 x 60 / 18 = 40 runs/min against 38.8 measured. A run
   is mostly *waiting* (model latency, page loads, humans), which is why a slot costs memory (about 0.2 to 0.25 GB with its share
   of the browser) rather than CPU.
2. **The model's rate, as soon as it is the scarce thing.** The `llm-bound` profile gives all runs one bucket of 2 calls/s. The
   stack then finished 7.2 runs/min, exactly the limiter's ceiling (2 calls/s x 60 / 16 calls per run = 7.5), with a limiter wait of
   1.2 s at p50 and 9.3 s at p95 and unchanged correctness. A real Gemini key is in this regime: the free tier allows about
   10 requests per minute, so one run (16 or more calls) needs a couple of minutes of quota, and a paid key with perhaps 1,000
   requests per minute allows about 60 runs/min, which is more than 12 slots can use. Real calls also take 2 to 6 s instead of
   0.8 s, so a run lasts 1 to 2 minutes and a slot yields about 0.5 to 1 run per minute: 12 slots would need only about 100 to
   200 calls/min. In production the model quota, not this software, sets throughput. The per-org daily call and token budgets
   (`max_llm_calls_per_day`, `max_tokens_per_day`) are the tool for sharing that quota fairly.
3. **Browser memory per context** (about 115 to 165 MB marginal, 0.9 to 1.0 GB per four-context worker). It caps contexts per machine
   (`BROWSER_MAX_CONTEXTS`, default 8) long before CPU does.
4. **The database** was not the limit at 490 transactions per second and 35 connections. The pieces most likely to bite next are
   connections (every API and worker process holds a pool of up to 20, plus one LISTEN connection each) and the single
   `llm_limits` row that every LLM call locks for a few milliseconds (it did not show up here at 25 calls/s; at hundreds of
   calls per second shard the bucket per key or move it to Redis).

Per-org fairness held: with `max_concurrent_runs` 4 an org never had more than 4 runs executing in any sample, while other
orgs' runs kept flowing.

## Recovery

* **Hard worker kill** (worker and browsers killed with their runs mid-flight, in both stress and soak): the 4 runs each time were
  re-queued by a surviving worker's reaper once the 15 s lease expired, resumed from their checkpoint in a fresh browser (the
  agent logs in again and checks whether its write already happened before repeating it) and all finished `succeeded`
  with exactly one correct bill each. The cost is the lease timeout plus a re-login.
* **Graceful worker stop** (Ctrl+Break, tried in an exploratory run, not part of the recorded profiles): the worker handed its 4 runs
  back immediately and they finished elsewhere.
* **API restart** (hard kill, back after 1.7 to 2.6 s; in one exploratory run with 3 approvals pending): sessions and approvals live in
  PostgreSQL, so every simulated user carried on, every pending approval was still answerable and all runs finished correctly.
  Live event streams that were open on the dead process drop (clients must reconnect and replay).

## Bugs the load test found

1. **A finished run could be declared "lease lost" (fixed).** The runner saves a run's final status, then the worker still writes the
   report's last events and the organization-memory commit before it stops that run's heartbeat. A heartbeat in that window found
   the run "not active" and treated it as a stolen lease: it logged `the lease is gone; cancelling it locally`, increased
   the lost-lease metric, fenced the worker's last writes, **cancelled the task in the middle of the memory commit** and
   never released the lease. About 1 run in 60 was hit. `jobqueue.heartbeat` now tells a run that has ended (final status, lease
   still in this worker's name) from a lost lease (re-queued or given up on by the reaper, or another owner), and answers
   `False` for the former. Regression tests: `tests/test_queue.py::test_a_heartbeat_that_lands_just_after_the_run_ended_is_not_a_lost_lease`
   (fails without the fix, as the original run did) and `::test_heartbeat_distinguishes_a_finished_run_from_a_lost_lease`.
2. **After a resume the independent audit could not see the portal (agent-side fix; a real-model risk).** A run resumed in a new
   browser has no portal session, and the auditor's tools are read-only on purpose (it cannot type a login). In the first stress
   run 3 of the 4 resumed runs ended `failed` ("Could not open the invoice in the vendor portal") although their bills were
   correct, because the policy agent answered the audit feedback by giving up. A careful agent opens the source again, which signs
   it in, and finishes again; `PolicyLLM` now does that once. Regression test:
   `tests/test_policy_llm.py::test_the_worker_recovers_when_the_auditor_cannot_see_the_portal`. The platform behaves as designed,
   but with a real model the same sequence is likely, and a deployment should either rely on that second audit round
   (`MAX_VERIFY_ROUNDS = 2`) or make the runner re-establish sessions after a resume.
   **Fixed in the platform since:** the read-only verifier can now sign in (it may fill vault credentials into a login form and click
   "Sign in", which is classified READ, and nothing else), and its prompt tells it to. After a resume the first audit passes without
   the second-round workaround, which remains only as a fallback. Regression test:
   `tests/test_policy_llm.py::test_the_auditor_signs_in_when_its_browser_is_fresh_and_passes_the_first_round`. The numbers on this page
   are unchanged; they were measured before this fix, with the workaround in place.
3. `worker/db/bootstrap.py` now also allows dropping the throwaway `oneforall_load*` databases (the test database
   allow-list is otherwise unchanged).

Things that were checked and held: no cross-org leak in about 118,000 scanned responses and frames and 7,900 probes, no duplicate or stray bill in
any ERP (with 10% of the runs repeating a task of their own org, some of them concurrently), no deadlock, no event out of order or from a foreign run, the per-org
concurrency cap, quotas not exceeded, approvals surviving an API restart, runs surviving a worker crash.

## How to scale further

* **More workers and machines.** Workers share nothing but the database: start `python -m worker.jobs` anywhere that can reach
  PostgreSQL. Slots scale linearly until memory or the model quota ends it. Put workers on memory-optimized machines (about 1 GB
  per four contexts) and keep each worker's Chromium bounded (`BROWSER_MAX_CONTEXTS`, recycling after N contexts).
* **More API copies** are stateless; add them behind a load balancer. Each one holds one LISTEN connection and a pool.
* **A pgbouncer** (transaction pooling) in front of PostgreSQL once the sum of pools exceeds `max_connections`
  (35 here for 4 processes; 40 workers would want 400+). The LISTEN/NOTIFY connections must bypass the pooler, so give them their
  own small budget (one per API copy and per worker) and keep `DB_POOL_SIZE` per process small.
* **Object storage for artifacts** (screenshots, downloads, reports): today they live in the worker's local `RUNS_DIR`, so a
  resumed run on another machine loses its earlier files and the API can only serve what is on its own disk. Use the
  `artifact_dir` / `artifact_file` methods of the `RunStore` seam (there is no separate `ArtifactStore` class yet) with GCS/S3 and serve by signed URL.
* **The model quota** is the first wall in production: use a paid key, an org-level key per customer (supported through the
  vault) and per-org budgets; raise `LLM_REFILL_PER_S` only to the provider's real limit.
* **Move the rate-limit bucket** off a single PostgreSQL row (Redis, or one bucket per key and per worker share) beyond a few
  hundred calls per second.
* **Partition or archive** `run_events` and `run_steps` (about 90 to 110 events per run); at 40 runs/min that is about 4,000 event rows/min.

## Honest caveats

* The model is a **policy**, not Gemini. It never makes a mistake a model makes (misreading a page, an odd tool call, a
  hallucinated value), never asks a question, and it needs only 16 calls per run. Real runs are longer, noisier and slower per call,
  so absolute latencies here are optimistic; the *structure* (queueing, leases, approvals, recovery, isolation, per-slot memory)
  is what the test proves.
* One machine runs the load generator, database, API, workers, browsers and simulated company; they compete for the same
  CPU and RAM, and 5.6 GB of RAM was free at the start. Network latency between components is zero.
  Cloud Postgres adds round trips to every claim, heartbeat, event and approval.
* The approvers' human delay (0.5 to 3 s) is far shorter than a person's; real approval waits are minutes and keep a browser
  context open the whole time, so with slow humans the **slot** (and its memory) is held by a run that is idle.
* Chaos flags are per company, not per run, so a fault lands on whichever run of the org gets there first.
* The runs are one task family (invoice entry), repeated; the ERP and portal are simulations with small data and no
  real-world latency.
* Memory numbers are RSS of process trees sampled every 2 s and include shared pages counted per process, so they overstate the total
  slightly.
* Short tests cannot prove the absence of slow leaks (see "Memory over time").

Reproduce: `python scripts/loadtest.py --orgs 3 --users 4 --runs 30 --workers 2 --concurrency 4` (about 100 s; it creates and drops its
own `oneforall_load` database, uses ports 8601 and 8602 and 9601+, and stops everything it started by PID, also on Ctrl+C).
