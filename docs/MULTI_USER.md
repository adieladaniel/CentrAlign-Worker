# Multi-user mode: operating guide

How to set up, use and operate OneForAll Agent with PostgreSQL: organizations, roles, the approvals inbox, credentials, quotas, the queue and the worker processes. For the design see [ARCHITECTURE.md](ARCHITECTURE.md); for measured behaviour see [SCALING.md](SCALING.md); the [README](../README.md) has the short setup.

Contents: [1 What runs where](#1-what-runs-where) | [2 Setup details](#2-setup-details) | [3 People and roles](#3-people-and-roles) | [4 Organization settings](#4-organization-settings) | [5 Approvals](#5-approvals) | [6 Credentials and the environment](#6-credentials-and-the-environment) | [7 Runs, the queue and quotas](#7-runs-the-queue-and-quotas) | [8 Memory and usage](#8-memory-and-usage) | [9 Operating the processes](#9-operating-the-processes) | [10 HTTP API](#10-http-api)

---

## 1. What runs where

| Process | Command | Count | Does |
|---|---|---|---|
| API and dashboard | `python -m worker.server` (or `uvicorn worker.server:app`) | 1 or more | sign-in, queues runs, records approvals, streams events. Never executes a run. |
| Worker | `python -m worker.jobs` | 1 or more | claims queued runs and executes them, K at a time, in one shared Chromium (a context per run) |
| Simulated company | `python -m company_sim` | 1 | the organizations' mail, vendor portal and ERP, one isolated tenant per org |
| PostgreSQL | your install | 1 | everything else |

`python run.py --workers N` starts the simulator, the API and N workers on one machine. Mode is chosen by `DATABASE_URL` / `AUTH_MODE` (the README has the table). With no database the dashboard keeps its single-user behaviour.

## 2. Setup details

```bash
python scripts/bootstrap.py --demo-password "choose-a-password"   # database, migrations, demo orgs and users
python run.py --workers 2
python scripts/bootstrap.py --provision-sim                       # while the simulator runs; safe to repeat
```

- `bootstrap` connects to the `postgres` maintenance database to create the database named in `DATABASE_URL`, runs the Alembic migrations and creates the demo organizations: **Acme Corp** with `owner@`, `admin@`, `operator@`, `approver@`, `viewer@acme.test`, and **Globex** with `operator@` and `approver@globex.test`. Without `--demo-password` a random password is generated and shown once, when the users are created; re-running never changes existing passwords. `--reset` drops and recreates the `oneforall` database after a confirmation (only `oneforall` and `oneforall_test` can be reset).
- `--provision-sim` creates each demo org's simulated company through the simulator's admin API and stores the logins in the vault. If the simulator is down it prints a note and skips. Any org admin can do the same later under **Admin > Provision environment**; a run cannot start in an org that has no environment (HTTP 409).
- **Vault key.** Credentials are encrypted with `VAULT_KEY`. Without it a development key is generated into `.vault/.vault_key`; the API and every worker must see the same key (the same checkout, or the same `VAULT_KEY`). With `DEPLOY_MODE` on, the key is mandatory. `python -m worker.vault generate-key` prints a new one.
- `python -m worker.db check` shows the masked URL, the server version and the migration state; `python -m worker.db migrate` applies pending migrations. The API logs an error at start if the schema is behind the code.
- An admin adds people to an organization under **Admin > Members > Add member** (the generated initial password is shown once). There is no self-registration, no password change, no password reset, and no UI or API to create a new *organization*: organizations come from the seed (`scripts/bootstrap.py`) or from SQL, as the load test does.

## 3. People and roles

Roles are ordered viewer < approver < operator < admin < owner. A person can be a member of several organizations with a different role in each; the organization switcher in the header changes the session's active organization. A membership can be deactivated in one org without touching the others (that ends the person's sessions in that org).

| Can | viewer | approver | operator | admin | owner |
|---|---|---|---|---|---|
| See runs, timelines, reports, files, the approvals inbox, the Memory page, their own usage | yes | yes | yes | yes | yes |
| Answer the agent's questions | | yes | yes | yes | yes |
| Approve or deny actions | | yes | only if `operators_can_approve` | yes | yes |
| Start runs (and set a stricter approval mode for a run) | | | yes | yes | yes |
| Cancel any run of the org | | | yes | yes | yes |
| Members, organization settings, credentials, environment (provision, reset, chaos flags), usage report, System card, audit trail | | | | yes | yes |
| Edit, delete or expire organization memory | | | | yes | yes |
| Assign or change the owner role | | | | | yes |

Limits on changing people: nobody can change their own role or deactivate themselves; an admin cannot assign a role above admin or touch an owner.

## 4. Organization settings

Edited under **Admin > Organization settings** (`PUT /api/admin/settings`, admin role). An org stores only what was changed; everything else falls back to the default. Every change is validated and audited.

| Setting | Default | Range | Meaning |
|---|---|---|---|
| `approval_mode` | the deployment's `APPROVAL_MODE` (`confirm_writes`) | `auto`, `confirm_writes`, `confirm_all` | which actions need a human. A floor: an operator may ask for stricter at run start, never looser |
| `separation_of_duties` | on | on/off | the person who started a run cannot approve its irreversible actions |
| `sod_for_writes` | off | on/off | also not its writes (needs `separation_of_duties`) |
| `operators_can_approve` | off | on/off | operators may answer approvals |
| `approval_timeout_s` | 3600 | 30 to 604,800 | an unanswered approval expires after this long and the run ends `needs_attention` |
| `max_runs_per_day` | 50 | 0 to 100,000 (0 = unlimited) | runs queued per UTC day, counted when a run is queued and including cancelled ones |
| `max_concurrent_runs` | 2 | 1 to 50 | runs of the org executing at once; the others wait in the queue |
| `max_steps` | the deployment's `MAX_STEPS` (60) | 1 to 500 | step budget per run |
| `run_timeout_s` | the deployment's `RUN_TIMEOUT_S` (900) | 30 to 86,400 | active-time limit per run (human waits excluded) |
| `max_tokens_per_day` | 2,000,000 | 0 to 10 billion (0 = unlimited) | input plus output tokens per UTC day |
| `max_llm_calls_per_day` | 500 | 0 to 10 million (0 = unlimited) | LLM calls per UTC day |
| `gemini_models` | empty (the deployment's chain) | up to 12 model names | the org's own failover chain |
| `memory_recall_limit` | 5 | 0 to 20 | memories recalled per run; 0 turns organization memory off |
| `memory_ttl_days` | 180 | 0 to 3650 (0 = never) | a memory unused for this long expires |

In database mode `ACCESS_PASSWORD` and the global `MAX_RUNS_PER_DAY` are ignored.

## 5. Approvals

An approval (or a question to the user) is a row in `approvals`, so it survives restarts of the API and of the worker.

- **The inbox.** The **Approvals** page lists everything waiting in the org, across runs, with the exact action, the form values about to be submitted, the agent's reason and the memory behind it, and updates live over a WebSocket. A badge shows how many are waiting for you. For each card the server says what the viewer may do: approve, deny only, or nothing, and why ("Separation of duties: you started this run ...").
- **Who may answer.** Approve/deny: approver, admin, owner, and operators only when the org allows it. Separation of duties blocks only *approving* (a starter can still deny). Questions: approver and above.
- **First writer wins.** The answer is written with a conditional update; a second person gets 409 "already resolved". An answer after the deadline is refused ("expired").
- **Expiry.** After `approval_timeout_s` the request expires (also when the worker that waited has died: every worker's reaper sweeps overdue approvals). The run ends `needs_attention` with "approval expired after N min"; it never guesses.
- **After a crash.** If the worker dies while a card is open, the run is resumed elsewhere and re-attaches to the same card (same tool and arguments), or takes an answer that arrived meanwhile.
- **Audit.** Each answer is in the audit log with who answered, the risk, the tool and who started the run; the run's timeline shows "approved by <name>".

## 6. Credentials and the environment

- **Credentials page (admin).** Add logins, API keys and header credentials with an optional URL and host limit. Values are write-only: the page and the API show names, kinds, hosts and field *names*; leaving a secret field blank keeps the stored value. The agent can use a credential only through `browser_fill_secret` and `http_request(auth_credential=...)` and never sees it; a credential with a host is used only for that host.
- **Own Gemini key.** A credential named `gemini` (kind `api_key`) makes the org's runs use that key (with its own shared rate bucket and model health). The agent cannot see or use it.
- **Environment.** Each org has its own simulated company. **Provision environment** creates it and stores its logins as `mail`, `portal`, `erp` and `erp_api`. **Reset environment** wipes and reseeds that org's tenant only (refused while a run of the org is live); chaos flags are chosen when an admin resets the environment. The tenant's entry token is kept in the vault as an internal credential, hidden from the agent and the page, and pre-seeded as a cookie in every run's browser context, so a run always starts inside its own org's company.
- `python -m worker.vault` manages the single-user *file* vault only.

## 7. Runs, the queue and quotas

- **Starting.** `POST /api/runs` checks the role, the environment and the daily run quota (under a per-org lock, so concurrent requests cannot both pass), inserts the run as `queued` and wakes the workers. The response is immediate; the dashboard follows the run by WebSocket.
- **Queue order.** Highest priority first, then oldest. Each org is limited by `max_concurrent_runs`, enforced when a worker claims a run, so one org cannot take every slot. A queued run shows its position in its org's queue.
- **Statuses.** `queued`, `running`, `waiting_approval`, `waiting_user`, `verifying`, then `succeeded`, `failed`, `needs_attention` or `cancelled`. `needs_attention` means the system stopped honestly: blocked, a guard fired, the model was unreachable, the audit was inconclusive, an approval expired, the budget was reached, or the worker was lost too often.
- **Cancelling.** A queued run is cancelled at once. A running run gets a flag and a NOTIFY; its worker stops it at the next safe point, which also releases open approvals.
- **Resume.** If a worker dies, its runs return to the queue after the lease expires (`JOB_LEASE_S`, 30 s by default) and the next claim continues from the checkpoint, in a fresh browser. The independent audit after a resume is not blind: the read-only verifier may sign in through the vault (fill a login form's credentials and click "Sign in"), and can do nothing else. A run that keeps losing its worker (`JOB_MAX_ATTEMPTS`, 3) ends `needs_attention`. The run list reports whether a run was resumed.
- **Budgets.** Before each LLM call the org's daily token and call totals (UTC day) are compared with its limits; at the limit the run ends `needs_attention` with "Organization daily LLM budget reached ... resets at 00:00 UTC". One call may overshoot, because the check happens before the call.

## 8. Memory and usage

- **Memory page.** Everyone in the org can read and search the notes; admins can edit, delete and expire them. Notes are written only by verified runs (see the README, section 6); they are never created from this page.
- **Usage card (Admin).** Calls and tokens per day (7 days by default), the top users and models, and today against the budget. Every member can read their own usage today (`GET /api/usage/me`).
- **System card (Admin).** Queue depth, runs and waiting approvals per org, the workers currently holding a run (with heartbeat age), runs finished in the last 24 hours by status, and p50 and p95 run time. Admins whose e-mail is in `PLATFORM_ADMIN_EMAILS` see every org; others see their own.

## 9. Operating the processes

- **Start a worker:** `python -m worker.jobs [--concurrency K] [--worker-id NAME] [--drain] [--metrics-port PORT]`. `K` defaults to `WORKER_CONCURRENCY` (2). `BROWSER_MAX_CONTEXTS` (8) caps the browser contexts open at once in one process; a run waits up to `BROWSER_ACQUIRE_TIMEOUT_S` (60 s) for a free one, so keep `K` at or below it. About 0.9 to 1.0 GB of memory per worker with four runs open was measured ([SCALING.md](SCALING.md)). Workers share nothing but the database; start them on any machine that reaches it (the same `VAULT_KEY`, the same `SIM_BASE_URL`).
- **Stop a worker:** the first Ctrl+C / SIGTERM / Ctrl+Break stops claiming and hands its runs back to the queue (another worker resumes them; the attempt is not counted). With `--drain` it lets the running runs finish instead. A second signal stops at once.
- **More API copies** are stateless; put them behind a load balancer. Each holds one LISTEN connection and a connection pool.
- **Health:** `GET /healthz` (liveness), `GET /readyz` (database, migrations at head, simulator; 503 with reasons). A worker logs the same checks at start.
- **Metrics:** `GET /metrics` on the API (admin session or `Authorization: Bearer <METRICS_TOKEN>`), and per worker with `--metrics-port` (loopback only). Metric names and a scrape config are in [ARCHITECTURE.md](ARCHITECTURE.md#observability).
- **Logs:** `LOG_FORMAT=json` for one JSON object per line with `run_id`, `org_id`, `user_id`, `worker_id`, `request_id`.
- **Migrations:** `python -m worker.db migrate`. The migrations are additive.
- **Connections:** every process holds up to `DB_POOL_SIZE + DB_MAX_OVERFLOW` (20) connections plus one for LISTEN. Size PostgreSQL's `max_connections` for the sum, or put a pooler in front for the pooled connections (the LISTEN connection must bypass a transaction-pooling proxy).

## 10. HTTP API

Roles in the table are the minimum. A foreign organization's ids always answer 404. State-changing requests that a browser marks cross-site are refused (403).

| Endpoint | Role | Purpose |
|---|---|---|
| `POST /api/auth/login`, `POST /api/auth/logout` | public / any | sign in (sets the `ofa_session` cookie), sign out |
| `GET /api/auth/me`, `POST /api/auth/switch-org` | viewer | the user, active org, all orgs, approval policy and capabilities; change the active org |
| `GET /api/runs`, `GET /api/runs/{id}` | viewer | the org's runs (with queue position); one run with steps, plan, facts, pending approvals and report |
| `POST /api/runs` | operator | queue a run `{task, approval_mode?, headed?}` |
| `POST /api/runs/{id}/cancel` | operator | cancel |
| `POST /api/runs/{id}/respond` | viewer to call; rights checked inside | answer an approval or question `{request_id, approved? / answer?, note?, remember?}` |
| `GET /api/approvals?status=` | viewer | the inbox (default `pending`), with what the caller may do per item |
| `WS /api/approvals/stream`, `WS /api/runs/{id}/events` | session | push: "changed" pings for the inbox; replay then live events of a run |
| `GET /runs/{id}/files/...` | viewer | report.md, screenshots, downloads (after the org check) |
| `GET /api/memory`, `PATCH` / `DELETE /api/memory/{id}`, `POST /api/memory/expire` | viewer / admin | read and search; edit, delete, expire |
| `GET /api/usage/me` | viewer | the caller's usage today |
| `GET /api/admin/members`, `POST`, `PATCH /{user_id}` | admin | list, add, change role or active state |
| `GET` / `PUT /api/admin/settings` | admin | organization settings |
| `GET /api/admin/credentials`, `PUT` / `DELETE /{name}` | admin | the vault, metadata only |
| `GET /api/admin/environment`, `POST .../provision`, `POST .../reset` | admin | the org's simulated company |
| `GET /api/admin/audit`, `GET /api/admin/usage`, `GET /api/admin/system` | admin | audit trail, usage report, System card |
| `GET /api/scenarios`, `GET /api/sim/chaos`, `GET /api/config` | viewer | example tasks, the org's chaos flags, limits |
| `POST /api/sim/reset` | admin | redirects (307) to the org's environment reset in database mode |
| `GET /api/environment/enter` | operator | opens the org's company in the browser through the sim proxy (when `PUBLIC_SIM_PROXY` is on) |
| `GET /api/health`, `/healthz`, `/readyz`, `/metrics` | public / token | health and metrics (see above) |
