# CentrAlign Worker: an autonomous AI task worker

Give it a business task in plain language, for example *"Find the latest invoice from Company X, extract the amount and due date, enter it into our internal system, and tell me once it is done."* It plans the work, drives a real browser and other tools, recovers from failures, asks for approval before changing anything, checks its own result with an independent verifier, and reports with evidence.

It runs against a simulated company (mail inbox, vendor billing portal, internal ERP) that contains deliberate traps, so every behaviour is exercised against real, failing, ambiguous systems instead of mocks. The agent runtime itself is task-agnostic: nothing in `worker/agent/` or `worker/tools/` knows about invoices.

> Demo video: <add link>

## Contents

1. [Setup and run](#1-setup-and-run)
2. [Architecture](#2-architecture)
3. [Design decisions and why](#3-design-decisions-and-why)
4. [Demo](#4-demo)
5. [Requirement-to-implementation map](#5-requirement-to-implementation-map)
6. [Evaluation results](#6-evaluation-results)
7. [Known limitations](#7-known-limitations)
8. [What I would build next](#8-what-i-would-build-next)
9. [Assumptions](#9-assumptions)
10. [Models, APIs, frameworks and components used](#10-models-apis-frameworks-and-components-used)
11. [Project structure](#11-project-structure)
12. [Reference: simulated company](#12-reference-simulated-company)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Setup and run

Requirements: Python 3.11+ (developed on 3.11.9 / Windows 11), about 400 MB of disk for Chromium, and a free Gemini API key.

### 1.1 Install

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium     # on Linux you may also need: python -m playwright install-deps chromium
```

### 1.2 Get a free Gemini API key and configure `.env`

1. Open <https://aistudio.google.com/apikey> and sign in with a Google account.
2. Click **Create API key**. No billing details are needed for the free tier.
3. Copy the template and paste the key:

```powershell
copy .env.example .env      # macOS/Linux: cp .env.example .env
```

```ini
LLM_PROVIDER=gemini
GEMINI_API_KEY=your-key-here
GEMINI_MODELS=gemini-3.5-flash,gemini-3.6-flash,gemini-2.5-flash   # fallback chain (the default lives in worker/config.py)
```

All other settings have working defaults and are documented in [.env.example](.env.example).

### 1.3 Start everything

```bash
python run.py            # add --open to launch the browser tab for you
```

This starts the simulated company on <http://localhost:8001> and the worker dashboard on **<http://localhost:8000>**, waits for both to be healthy, and stops both on Ctrl+C. The simulator creates its SQLite database and invoice PDFs on first start (`company_sim/data/`).

In the dashboard: pick an example task (or type your own), optionally switch on **Headed browser** to watch Chromium work, and press **Run task**. Approval cards and clarification questions appear inline. The simulated apps can be browsed by hand at <http://localhost:8001> (credentials are in [section 12](#12-reference-simulated-company)).

### 1.4 CLI alternative (no dashboard)

With the simulator running (`python -m company_sim` in another terminal, or `python run.py`):

```bash
python -m worker.agent "Find the latest invoice from Company X, enter it into our internal system, and tell me when done." --headed
python -m worker.agent "..." --approval confirm_all      # auto | confirm_writes | confirm_all
```

Events print to the terminal, and approvals and questions are answered at the prompt (`y` / `n` / `a` for "always for similar"). Exit code is 0 only when the run `succeeded`. The same run files are written to `runs/<id>/`.

### 1.5 Tests

```bash
pytest                   # 245 tests, no API key and no network needed (LLM replaced by a scripted fake)
```

The suite covers the simulator, tool framework and retry logic, the browser tool and snapshot against local HTML fixtures (needs Chromium), risk classification and approval policy, context building, guards, the runner end to end with a scripted LLM (including verifier failure and recovery), the Gemini and Ollama adapters, and the server API. It takes about a minute because several tests launch Chromium.

### 1.6 Evals

```bash
python -m evals.run                                  # every scenario in evals/scenarios/*.yaml
python -m evals.run --only invoice_happy,deny_write  # a subset
python -m evals.run --list                           # show scenarios and their chaos flags
python -m evals.run --repeat 3 --sleep-between 20    # repeat runs; pause between runs for free-tier quotas
python -m evals.run --parallel 3                     # 3 scenarios at once, each in its own simulated-company tenant
```

`--parallel N` implies `--tenant-per-scenario`: every scenario gets a freshly provisioned, isolated copy of the simulated company (own data, logins and chaos flags, reached through an entry link `/t/<token>/`, with its own workspace folder holding that tenant's `credentials.json`), so scenarios cannot disturb each other and share one browser. Without these flags the scenarios run one after the other on the default tenant, as before. Tenants live in `company_sim/data/tenants/`; the sim's `/admin/*` endpoints can be locked with `SIM_ADMIN_TOKEN` (send it as `X-Admin-Token`; set the same variable for the evals and the worker).

Other flags: `--headed`, `--provider gemini|ollama`, `--sim-url`, `--scenarios-dir`. The runner starts the simulator itself if nothing is listening on `SIM_BASE_URL`, resets it with each scenario's chaos flags, runs the real agent in-process with scripted approvals and answers, then judges the result from the simulator's data. It needs a valid `GEMINI_API_KEY`, makes real LLM calls, and takes several minutes on the free tier. Results print as a table and are written to `evals/results/latest.md` (plus a timestamped JSON). Exit code: 0 all passed, 1 some failed, 2 setup problem. See [section 6](#6-evaluation-results).

### 1.7 Optional: local models with Ollama

```bash
ollama pull qwen2.5:3b
# .env
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen2.5:3b
OLLAMA_URL=http://localhost:11434
```

The Ollama adapter uses `/api/chat` with native tool calling behind the same interface as Gemini. Expect much weaker reliability from small models; see [decision 3.12](#312-gemini-free-tier-behind-a-provider-adapter).

### 1.8 Multi-user mode (PostgreSQL)

Set `DATABASE_URL` and the dashboard turns into a multi-user, multi-organization app: people sign in with e-mail and password, belong to organizations with a role, and only ever see their own organization's runs. Without `DATABASE_URL` nothing changes (one shared `ACCESS_PASSWORD` or no auth, runs in files). The switch is `AUTH_MODE` (`auto` = database mode when `DATABASE_URL` is set; `file` ignores the database for sign-in; `db` insists on it).

```bash
python scripts/bootstrap.py                  # create the database, migrate, seed two demo orgs and their users
python scripts/bootstrap.py --provision-sim  # also give each demo org its own simulated company (the simulator must be running)
python run.py                                # simulator + dashboard; sign in at http://localhost:8000
```

The demo logins are `owner@acme.test`, `admin@acme.test`, `operator@acme.test`, `approver@acme.test`, `viewer@acme.test` (organization **Acme Corp**), and `operator@globex.test`, `approver@globex.test` (organization **Globex**). The demo password is not fixed: `bootstrap` generates one and prints it **once**, the first time it creates the users (or choose it with `--demo-password`); rerunning it never changes existing passwords. Admins add real users under **Admin > Members** (the generated initial password is shown once).

| Role | Watch runs | Start / cancel runs | Approve actions | Answer agent questions | Admin pages (members, settings, credentials, environment) |
|---|---|---|---|---|---|
| viewer | yes (read-only) | no | no | no | no |
| approver | yes | no | yes | yes | no |
| operator | yes | yes | only if the org sets `operators_can_approve` | yes | no |
| admin | yes | yes | yes | yes | yes |
| owner | yes | yes | yes | yes | yes, and the only role that can create or change owners |

**Separation of duties.** With the org setting `separation_of_duties` (on by default) the person who started a run cannot approve its irreversible actions; with `sod_for_writes` also not its writes. They can always deny. The approval card says who may approve and why its buttons are disabled, and the timeline shows "approved by <name>".

**Per-organization environment.** Every org has its own simulated company (mail, vendor portal, ERP) with its own logins, kept in the encrypted credential vault. The agent can use them without ever seeing them, and its browser is pinned to the org's company, so a run can never land in another org's data. Admins manage credentials on the **Credentials** page (secret fields are write-only) and the run quota (`max_runs_per_day`), approval policy and limits under **Admin > Organization settings**.

Sessions are server-side and revocable (`SESSION_IDLE_TTL_HOURS`, `SESSION_ABSOLUTE_TTL_DAYS`), and failed sign-ins are throttled per e-mail and per address. Runs still execute inside the API process, one at a time per organization.

---

## 2. Architecture

```
 Browser tab                                                     Terminal
 Dashboard (vanilla JS)                                          python -m worker.agent "..."
      |  REST + WebSocket (events, approvals)                         |
      v                                                               v
 +----------------------- Worker (FastAPI :8000) ----------------------------+
 |  server.py: run sessions, event log (events.jsonl), InteractionBroker      |
 |                                                                            |
 |  AgentRunner  (worker/agent/runner.py)  one per task, task-agnostic        |
 |  +-------------------------------------------------------------------+     |
 |  | context.py  rebuild prompt from RunState every turn               |     |
 |  | LLM (ResilientLLM -> Gemini | Ollama): returns tool calls         |     |
 |  | policy.py   risk class + approval mode -> approval gate (human)   |     |
 |  | retry.py    execute tool, auto-retry transient errors             |     |
 |  | state.py    Step / Fact / PlanItem recorded + saved to run.json   |     |
 |  | guards.py   failure streak, repetition, step budget, timeout      |     |
 |  | verifier.py fresh-context auditor, read-only tools, verdict       |     |
 |  +-------------------------------------------------------------------+     |
 |        |                                                                   |
 |  Tools: browser_* (Playwright)  list/read/write_file  http_request         |
 |         remember  update_plan  ask_user  finish                            |
 +--------|-------------------------------------------------------------------+
          | Chromium (DOM snapshot with numbered elements), httpx, files
          v
 +---------------- Simulated company (FastAPI :8001) -----------------+
 |  /mail    inbox + PDF attachments + reply (Sent folder)            |
 |  /portal  vendor billing portal (login, paginated, unsorted list)  |
 |  /erp     "Ledger" AP bills: UI form + REST API (X-API-Key)        |
 |  /admin   chaos flags, reset, ground-truth state (used by evals)   |
 |  SQLite + reportlab PDFs, deterministic seed                       |
 +---------------------------------------------------------------------+
```

One turn of the loop: build context from state, ask the LLM for tool calls, classify each call's risk, ask the human if policy requires it, execute with transient-retry, record the step, check guards. When the agent calls `finish(status="success")`, a separate verifier re-checks the real systems before the run is allowed to count as succeeded. Details, sequence diagram, event model and the reliability matrix are in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## 3. Design decisions and why

### 3.1 A task-agnostic runtime, tested with several unrelated tasks

The agent loop, prompts, tools and verifier contain no invoice logic. The task arrives as user text; the agent infers success criteria and a plan on its first turn (`update_plan`). Task-specific knowledge lives only in the simulated company and in the evals. The proof is the eval suite: invoice entry, replying to a vendor email, and a multi-item "backfill this month" task run on identical code. Outside `evals/` and the simulator, invoice wording appears only in the fallback preset list the dashboard shows when no scenario YAML is found ([worker/server.py](worker/server.py)) and in file-name examples inside tool descriptions.

### 3.2 Text DOM snapshots with element refs, not screenshots

Every browser action returns a compact text observation: URL, title, alerts, page text with tables as pipe rows, and numbered interactive elements (`[7] button "Save bill"`). Refs are stamped into the page as `data-agent-ref` and renumbered on every snapshot, so a stale ref produces a clear error instead of a wrong click. Why not vision:

- **Cost and free tier:** a snapshot is capped at about 3,500 characters of text plus 80 element lines per view (very roughly a thousand tokens); images cost more and burn the free quota and rate limit faster.
- **Reliability:** the agent clicks `[11]`, not a pixel coordinate. Form values, select options (with the valid list), `INVALID: <message>` markers, and validation alerts are exact text, not something to be read off a picture.
- **Portability:** works with any model that has tool calling, including text-only local models.
- Screenshots are still taken on state-changing actions and saved as human-readable evidence (not sent to the model).

The cost is that canvas-only UIs, iframes and visually encoded information are invisible to the agent (see limitations).

### 3.3 Rebuild context from state every turn, instead of a growing chat history

[worker/agent/context.py](worker/agent/context.py) builds each prompt from the explicit `RunState`: system prompt; task and environment; one-line summaries of older steps (capped at 60 lines); the last 6+ steps as whole turns with properly paired tool-call and tool-result messages (only the latest turn's observations are shown in full); and a state block with goal, success criteria, plan, working memory, verifier feedback and guard notices. Reasons: the token cost stays bounded across a 40-step run on a free tier, old page dumps do not drown the signal, and cutting history by whole turns avoids orphaned tool results that providers reject. A test property-checks the pairing invariant over many histories.

### 3.4 Working memory with provenance

The agent calls `remember(key, value, source)` when it learns something it will need later; the state block shows it back every turn with its source and step number. This is what lets old observations be dropped safely, and the provenance is shown on approval cards, passed to the verifier, and exported in the report. It is also a hallucination check: a value with no source is visibly suspicious.

### 3.5 Risk classification plus a runtime-enforced approval policy

The LLM never decides whether to ask. Each tool declares a risk, and calls can be reclassified by arguments: `http_request` GET is read, POST/PUT/PATCH write, DELETE irreversible; browser clicks are classified from the element's role and accessible name (a button named "Save bill" is a write, "Delete" or "Pay" is irreversible, "Next" or "Search" is a read); pressing Enter or `type(submit=true)` is classified by the form's submit buttons. [worker/agent/policy.py](worker/agent/policy.py) maps risk plus `APPROVAL_MODE` to a pause, and the runner blocks on a human answer before the call executes. The card shows the exact action and the memory behind it. A denial returns to the agent as an observation, and it must adapt. Prompt-only "please ask before writing" fails the moment the model forgets; this cannot be skipped. Honest caveat: browser classification is heuristic on labels (see limitations).

### 3.6 An independent verifier with fresh context and read-only tools

When the agent claims success, [worker/agent/verifier.py](worker/agent/verifier.py) runs a separate, short LLM conversation. It sees only the task, the success criteria, the claim, and the memory facts; it does not see the agent's reasoning. It has only the read-only subset of the tools and must re-observe the world (open the bill, compare amount and due date, look for duplicates) and submit one check per criterion with what it actually saw. A verdict with no checks, or any failed check, is downgraded to failed. This targets the classic failure of agents that report success after a form silently did not save. Failed audits feed their findings back into the loop (up to 2 correction rounds); an audit that cannot complete is reported as unverified (`needs_attention`) instead of passed.

### 3.7 Guards that escalate to a human instead of thrashing

[worker/agent/guards.py](worker/agent/guards.py): a step budget, a wall-clock timeout, a consecutive-failure counter (nudge at half the limit, escalate at the limit), and a repeat detector (same action and same observation three times in six steps). Soft problems get one corrective nudge, then the run pauses and asks the human "Keep trying / Stop the task". Hard limits end the run as `needs_attention` with a partial report. Denials, skipped calls and rejected finishes are not counted as tool failures.

### 3.8 Transient vs. semantic errors, handled at different layers

Transient failures (timeouts, connection resets, detached elements, HTTP 5xx, a closed browser) of idempotent or read-level calls are retried with exponential backoff inside the tool layer ([worker/tools/retry.py](worker/tools/retry.py)); the LLM sees only the final outcome plus a note such as "succeeded after 1 automatic retry". Semantic failures (validation errors, not found, stale ref, wrong format) are returned to the model with an actionable message ("Date must be in YYYY-MM-DD format", the list of valid select options) so it re-plans. Retries follow an idempotency rule: only calls that are safe to repeat are retried automatically. For `http_request` that means GET, HEAD and OPTIONS; a 5xx or network error on POST, PUT, PATCH or DELETE is returned as a non-transient failure with the note "the write may or may not have happened: check whether the record exists (GET) before retrying", because a 5xx after a partial write would otherwise duplicate it. The same applies to the browser: clicks and key presses classified above read (and typing with submit) are not retried after a timeout or a closed page; the agent is told to re-read the page and check whether the action took effect. Read-level browser actions keep their auto-retry. Retrying a validation error is wasted calls; re-planning a network blip wastes tokens, so the layers are split.

### 3.9 A simulated company with deliberate traps

A narrow prototype that genuinely works beats mocked breadth. Rather than pretending to integrate with a dozen SaaS products, the repo ships three small but real web apps with sessions, validation and a REST API, plus failure injection. Traps: the portal list is paginated and **not sorted by date**; amount and due date appear only on the detail page and inside the PDF; the ERP wants `YYYY-MM-DD` while invoices show other formats; the ERP rejects duplicates; "Company X Ltd" vs "Company X Holdings" is ambiguous; credentials are only in a workspace file; and chaos flags add one-time 500s, session expiry, missing portal invoices and latency. Everything is seeded deterministically, relative to "today", and is resettable, so runs are comparable.

### 3.10 Deterministic ground-truth evals, not LLM-judged

Each scenario ends with a Python checker ([evals/checkers.py](evals/checkers.py)) that compares what is in the simulator afterwards (bills and sent mail from `GET /admin/state`, expected invoices derived from the deterministic seed) with what should be there: exactly one bill, right vendor, exact amount, currency and due date, no duplicates or stray records, nothing written after a denial, a reply actually in the Sent folder that states the due date. The agent's own claims are never used to decide success; if the agent says `succeeded` while the ground truth disagrees, the run is flagged as a **false success**. LLM-as-judge would grade the agent with the same kind of fallibility it is being tested for, whereas the checkers can see what the agent cannot.

### 3.11 No agent framework

No LangChain, LlamaIndex or similar. The runner is one file of about 400 lines and is the interesting part of the task: context policy, approval gating, guard behaviour and verification all need precise control over when and how the model is called, and a framework would hide exactly those decisions. It also keeps the behaviour inspectable and the dependency list short.

### 3.12 Gemini free tier behind a provider adapter

Zero cost was a requirement, so the default is the Gemini free tier with native function calling via the `google-genai` SDK. [worker/llm/base.py](worker/llm/base.py) defines a neutral message and tool-spec format, so Gemini and Ollama (and a scripted fake for tests) are interchangeable. A `ResilientLLM` wrapper adds the pieces free tiers need: a minimum interval between calls, retry with backoff that honours the server's retry hint on 429/5xx, JSON-schema validation of tool arguments with one corrective re-prompt, and token accounting. Ollama is the offline fallback, but a 4 GB laptop GPU (RTX 3050) only fits roughly 3B-parameter models, which were not reliable enough at multi-step tool calling to be the primary path.

### 3.13 Design rules that came from failures I expected

- Calls after a failure in the same turn are skipped (the plan changed), and `finish` must be called alone, so the model cannot claim success in the same breath as an unseen result.
- The system prompt requires checking whether a record exists before creating it, and trusting the authoritative source (PDF over list view).
- File access is sandboxed to `workspace/` plus the run's `downloads/`; `http_request` is restricted to an allowlist of hosts.

---

## 4. Demo

> Demo video: <add link>

The recording shows, in order (shot-by-shot script in **[docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md)**):

1. **Headline task, headed browser.** The agent reads the shared workspace, finds credentials, logs in to the vendor portal, works out which invoice is the latest despite the unsorted list, reads the amount and due date from the invoice, then logs in to the ERP.
2. **Approval gate.** Before the "Save bill" click, an approval card shows the exact values; approving lets it proceed. The ERP bill is shown in the real ERP afterwards.
3. **Chaos run.** Same task with a one-time server 500 and session expiry: re-login, and a clean result (the API 500 on a write is not retried blindly: the agent checks whether the bill exists, then retries).
4. **Ambiguity.** With "Company X Holdings" present, the agent asks which vendor instead of guessing.
5. **Verification and report.** The final card shows the independent audit's checks, what it actually saw, evidence screenshots, and run stats.
6. **A second, unrelated task** (reply to a vendor's payment-date question, then the month-end backfill) on the same code, showing generalisation.
7. **Eval table** from `python -m evals.run`.

---

## 5. Requirement-to-implementation map

| Capability | How it is implemented | Where |
|---|---|---|
| Understands the end goal | First turn restates the goal, defines checkable success criteria, lists unknowns, calls `update_plan` | [prompts.py](worker/agent/prompts.py), [control.py](worker/tools/control.py) |
| Breaks it into actions | Plan checklist kept current by `update_plan`; status per item; shown in the dashboard | [control.py](worker/tools/control.py), [state.py](worker/agent/state.py) |
| Acts on a real computer | Playwright Chromium (10 browser tools), files, HTTP | [browser.py](worker/tools/browser.py), [files.py](worker/tools/files.py), [http.py](worker/tools/http.py) |
| Observes | Compact DOM snapshot with element refs, alerts, validation errors; PDF text extraction | [snapshot.js](worker/tools/snapshot.js), [snapshot.py](worker/tools/snapshot.py) |
| Remembers | Working memory with provenance, re-injected every turn; state-based context | [memory.py](worker/agent/memory.py), [context.py](worker/agent/context.py) |
| Detects failure | `ToolResult.ok/error`, page alerts, HTTP status, guards (streak, repetition, budget, timeout) | [retry.py](worker/tools/retry.py), [guards.py](worker/agent/guards.py) |
| Retries / finds alternatives | Auto-retry for transient errors; semantic errors go to the LLM; prompt directs it to alternatives such as the ERP REST API | [retry.py](worker/tools/retry.py), [prompts.py](worker/agent/prompts.py) |
| Verifies its work | Independent fresh-context auditor, read-only tools, per-criterion checks, feedback loop | [verifier.py](worker/agent/verifier.py), [runner.py](worker/agent/runner.py) |
| Asks for clarification | `ask_user` tool routed to the dashboard; prompt says look first, ask only when ambiguous | [interaction.py](worker/tools/interaction.py), [broker.py](worker/agent/broker.py) |
| Asks for approval | Runtime risk classification + approval policy; the model cannot skip it | [policy.py](worker/agent/policy.py), [browser.py](worker/tools/browser.py) |
| Summary with evidence | `finish` carries summary, outputs, claim-to-source evidence; report adds audit checks, screenshots, stats, `report.md` | [report.py](worker/agent/report.py), [app.js](worker/ui/app.js) |
| Safe by default | Approval modes, file sandbox, HTTP host allowlist, step budget, timeout, no-retry on denial | [config.py](worker/config.py), [files.py](worker/tools/files.py) |
| Observable | Live timeline, plan, memory, usage, event log `events.jsonl`, `run.json` after every step | [server.py](worker/server.py), [ui/](worker/ui) |
| Generalises | Task-agnostic runtime; 8 eval scenarios of different shapes | [evals/scenarios/](evals/scenarios) |

---

## 6. Evaluation results

Scenarios live in [evals/scenarios/](evals/scenarios). Each one defines the task text, the chaos flags to set, how approvals and questions are answered automatically (`approve_all`, or `deny_writes`), and a deterministic checker that inspects the simulator database afterwards.

| Scenario | What it tests |
|---|---|
| `invoice_happy` | Baseline: find latest Company X invoice, enter in ERP |
| `invoice_server_error` | One-time ERP 500 and session expiry: retry and re-login |
| `invoice_duplicate` | Bill already exists: must not double-enter |
| `invoice_ambiguous_vendor` | Ltd vs Holdings: must ask, then enter under the right vendor |
| `invoice_portal_missing` | Invoice absent from the portal: must re-plan and use the email PDF |
| `reply_payment_date` | Non-invoice task: reply to a vendor email with the correct due date |
| `month_backfill` | Multi-item: record every open invoice due this month that is not in the ERP |
| `deny_write` | Human denies the write: nothing may change and the agent must report honestly |

Run them with `python -m evals.run`. Results are written to `evals/results/latest.md` (the timestamped JSON is git-ignored; `latest.md` is kept so the table can be read without running anything). Columns: `Agent said` is the final status the agent reported, `Ground truth` is the checker's verdict, `Score` is 0 to 1 (partial credit only for `month_backfill`, as covered / total invoices). Copy the table from that file into this README after a run:

| Scenario | Chaos | Agent said | Ground truth | Score | Steps | LLM calls | Tokens | Retries | Approvals | Questions | Time |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `invoice_happy` | - | (run to fill) | | | | | | | | | |
| `invoice_server_error` | erp_submit_500_once, erp_session_expiry | | | | | | | | | | |
| `invoice_duplicate` | duplicate_bill | | | | | | | | | | |
| `invoice_ambiguous_vendor` | ambiguous_vendor | | | | | | | | | | |
| `invoice_portal_missing` | portal_missing_invoice | | | | | | | | | | |
| `reply_payment_date` | - | | | | | | | | | | |
| `month_backfill` | - | | | | | | | | | | |
| `deny_write` | - | | | | | | | | | | |

LLM behaviour varies between runs (`--repeat N` helps); treat a single table as a sample, not a guarantee.

---

## 7. Known limitations

- **Scope is web, files and HTTP.** There is no desktop or pixel-level computer use, no OS automation, no vision. The browser tool does not handle iframes or file uploads, and it cannot see canvas-only content.
- **One run at a time.** The server returns 409 for a second run and refuses an environment reset during a run. The simulator holds state in one SQLite file and in-memory sessions.
- **Simulated environment.** The company is small and clean compared with real systems; real-world robustness (CAPTCHAs, SSO/MFA, flaky SPAs) is untested.
- **Free-tier rate limits slow runs.** The client throttles to one call every 4 seconds by default and backs off on 429, so a 30-step task takes minutes. Quotas and model names can change on Google's side.
- **The LLM can still misjudge.** The verifier is the same model family as the worker, so correlated errors are possible. It catches missing or wrong records, but not misreading ambiguous requirements it also misreads.
- **The verifier shares the worker's browser session.** That is deliberate (it needs the logged-in state), but it means page state can be affected between the two. The verifier cannot type, so if the session has expired it relies on GET requests, such as the ERP API key from `workspace/credentials.json`, or reports inconclusive.
- **Browser risk classification is heuristic.** It reads element roles and labels. A destructive action behind an unlabeled or oddly named control could be classified as a read. API calls and file writes are classified exactly.
- **No idempotency keys.** Automatic retry is restricted to idempotent calls (see 3.8), so a failed write is never re-sent blindly. The agent is told to check whether the record exists before retrying, but that check is prompt-driven, not enforced by the runtime.
- **No authentication on the worker UI or the `/admin` endpoints.** Both bind to localhost and are meant for a single local user.
- **Credentials sit in a plaintext workspace file** (`workspace/credentials.json`). This is by design: the task is for the agent to discover how to log in, as it would on a shared drive. There is no secret store; typed passwords are masked in approval cards and page snapshots, but the file itself is readable by the agent and its contents appear in the step log.
- **No unconditional protection from prompt injection.** Page text and PDF contents are untrusted input that reaches the model. The approval gate and read-only verifier limit the damage, but this is not a security boundary.
- **Approvals wait indefinitely** in the dashboard (no timeout), and a run is not resumable after the server restarts, although its history remains viewable.
- **Dates are anchored to the real current date**, so seeded data shifts day to day (`SIM_TODAY` can pin it). The agent's own "today" comes from the host clock.

---

## 8. What I would build next

In priority order:

1. **Resumable runs.** Persist enough state (it is already saved after every step) to resume after a crash or restart, and add approval timeouts and reminders.
2. **Idempotency and exactly-once writes.** Idempotency keys on writes and a pre-write "does it exist" check enforced by the runtime rather than the prompt (retry is already restricted to idempotent calls).
3. **Stronger verification.** A second verifier model (different family) to cut correlated errors; verifier-written checks that run as deterministic assertions where the system exposes an API.
4. **Real integrations.** Connectors for real mail and ERP APIs where they exist (cheaper and more reliable than UI driving), with the browser as the fallback, plus a secret manager instead of a plaintext file.
5. **Vision fallback.** Screenshot-based observation only when the DOM snapshot is empty or ambiguous (canvas, iframes), to widen coverage without paying for vision on every step.
6. **Concurrency and queueing.** Multiple runs with isolated browser contexts, a task queue, and per-run budgets.
7. **Auth and audit trail.** Login and roles on the dashboard, signed approval records, and exportable audit logs.
8. **Learning from runs.** Reusable "skills" distilled from successful runs (for example, how this portal paginates) and an eval dashboard that tracks pass rate over time.

---

## 9. Assumptions

- The reviewer has Python 3.11+, can install Chromium through Playwright, and can create a free Google AI Studio key.
- "Using a computer" means a real browser plus files and HTTP against web applications. A simulated company is acceptable because real accounts cannot be shared, and it lets failure modes be injected and checked deterministically.
- "Latest invoice" means the most recent issue date, not the highest internal id or the first row.
- The company name "Company X" in the task is ambiguous only when the environment contains an alias; otherwise it maps to "Company X Ltd".
- A human is available to approve writes and answer questions during interactive runs; evals use scripted responders instead.
- "Payment date" for the vendor-reply task is taken to mean the invoice due date, as stated in the task text.
- The amount to record is the invoice's total as printed on it (including tax), in the invoice's own currency; this is what the evals check against.
- A single local user; no multi-tenancy.

---

## 10. Models, APIs, frameworks and components used

| Component | Use |
|---|---|
| **Google Gemini API**, models `gemini-3.5-flash`, `gemini-3.6-flash`, `gemini-2.5-flash`, ... as a failover chain (the default in [worker/config.py](worker/config.py), overridable via the `GEMINI_MODELS` fallback chain) via the **`google-genai`** SDK | Primary LLM, native function calling, free tier |
| **Ollama** (optional, default model `qwen2.5:3b`) | Local fallback behind the same adapter |
| **Playwright** (Chromium) | Real browser automation |
| **FastAPI**, **uvicorn**, **Jinja2**, `python-multipart`, `itsdangerous` | Worker API, WebSocket events, the simulated company's web apps and sessions |
| **SQLite** (stdlib `sqlite3`) | Simulator data store |
| **reportlab** | Generates the invoice PDFs |
| **pypdf** | Extracts text from PDFs for the `read_file` tool |
| **httpx** | HTTP tool, server-to-simulator calls, health checks |
| **pydantic**, `pydantic-settings`, `python-dotenv` | Settings and request models |
| **jsonschema** | Validates tool-call arguments before execution |
| **PyYAML** | Eval scenario files and dashboard presets |
| **pytest**, `pytest-asyncio` | Test suite |
| Vanilla JavaScript, HTML, CSS | Dashboard (no build step, no CDN) |

No agent framework (LangChain, LlamaIndex, AutoGen and similar) is used, deliberately; see [decision 3.11](#311-no-agent-framework). All dependencies are free and open source; the only external service is the Gemini API.

---

## 11. Project structure

```
CentrAlignAI/
├── run.py                    start simulator (:8001) + worker/dashboard (:8000) together
├── requirements.txt  pytest.ini  .env.example  .gitignore
├── company_sim/              simulated company (FastAPI + Jinja2 + SQLite)
│   ├── app.py  __main__.py   app wiring, `python -m company_sim`
│   ├── apps/                 mail.py  portal.py  erp.py (UI + REST API)  admin.py
│   ├── seed.py  db.py        deterministic seed data, schema
│   ├── chaos.py              failure-injection flags
│   ├── auth.py  state.py     cookie sessions; ground-truth queries for evals
│   ├── pdfgen.py             invoice PDFs
│   └── templates/ static/    HTML for the three apps
├── worker/
│   ├── config.py             settings (env / .env)
│   ├── server.py             REST + WebSocket API, run sessions, event log
│   ├── agent/                runner.py  context.py  state.py  memory.py  policy.py
│   │                         guards.py  verifier.py  report.py  broker.py  prompts.py
│   │                         toolset.py  cli.py (python -m worker.agent)
│   ├── llm/                  base.py  gemini.py  ollama.py  resilient.py  factory.py  fake.py
│   ├── tools/                base.py  registry.py  retry.py  browser.py  snapshot.py/.js
│   │                         files.py  http.py  memory_tools.py  interaction.py  control.py
│   └── ui/                   index.html  app.js  styles.css (dashboard)
├── evals/                    scenarios/*.yaml, run.py (runner), checkers.py, scenarios.py, simstate.py, results/
├── tests/                    245 tests (scripted LLM, local HTML fixtures)
├── workspace/                the agent's "shared drive": credentials.json, README.txt
├── docs/                     ARCHITECTURE.md, DEMO_SCRIPT.md
└── runs/                     (generated) per-run logs, screenshots, downloads, report.md
```

---

## 12. Reference: simulated company

Credentials (also in [workspace/credentials.json](workspace/credentials.json), which the agent is expected to find):

| App | URL | Login |
|---|---|---|
| Mail (shared AP inbox) | <http://localhost:8001/mail> | `ap@acme.example` / `mail-pass-123` |
| Vendor portal | <http://localhost:8001/portal> | `acme-ap` / `portal-pass-456` |
| ERP "Ledger" | <http://localhost:8001/erp> | `ap.clerk` / `ledger-pass-789` |
| ERP REST API | <http://localhost:8001/erp/api> (docs at `/erp/api/docs-summary`) | header `X-API-Key: ledger-api-key-001` |

Seeded data: six vendors (including both **Company X Ltd** and **Company X Holdings**), 14 portal invoices in non-chronological order (5 per page), inbox emails (three with PDF invoices, a vendor asking about the payment date of INI-20502, and noise), and five historical ERP bills.

Chaos flags (toggle in the dashboard, with `POST /admin/chaos`, or `SIM_CHAOS=a,b` when starting the sim; data flags trigger a reseed):

| Flag | Effect |
|---|---|
| `erp_submit_500_once` | First ERP form submit (and first ERP API POST) returns HTTP 500, then works |
| `erp_session_expiry` | ERP session expires after the 3rd page view, once per reset |
| `duplicate_bill` | Latest Company X Ltd invoice already exists as an ERP bill |
| `ambiguous_vendor` | Company X Holdings has an invoice with the same issue date as Company X Ltd's latest |
| `portal_missing_invoice` | Latest Company X Ltd invoice is hidden from the portal (email attachment only) |
| `slow_pages` | 1 to 3 seconds random latency on page loads |

Admin endpoints (no auth, localhost only): `GET/POST /admin/chaos`, `POST /admin/reset` (optional body `{"chaos": [...]}`), `GET /admin/state` (bills and sent mail, the ground truth the evals read).

---

## 13. Troubleshooting

| Symptom | Fix |
|---|---|
| Run stops with "language model could not be reached" or log shows 429 / `RESOURCE_EXHAUSTED` | Free-tier quota or rate limit. The client already backs off and honours retry hints; wait a minute, raise `LLM_MIN_INTERVAL_S` (for example `8`), or edit the `GEMINI_MODELS` chain. A model whose free-tier daily quota (about 20 requests per model per day) is used up is skipped automatically and the run continues on the next model; the error "All configured Gemini models are out of free-tier quota" means every model in the chain is used up for the day. |
| `GEMINI_API_KEY is not set` / dashboard badge says no key | Create `.env` from `.env.example` and set the key; restart `python run.py`. |
| `Chromium is not installed. Run: python -m playwright install chromium` | Run exactly that in the activated venv. On Linux also `python -m playwright install-deps chromium`. |
| `port(s) 8000/8001 already in use` | Another instance is still running. On Windows: `netstat -ano \| findstr :8000`, then `taskkill /PID <pid> /F`. On macOS/Linux: `lsof -i :8000`. |
| Browser tools fail with `NotImplementedError` on Windows | Playwright needs subprocess support in the event loop. Start via `python run.py` or `python -m worker.server`; **do not use `uvicorn --reload`**, which can switch to a loop that cannot spawn Chromium. |
| Dashboard says the environment is not reachable | The simulator is not running. Use `python run.py`, or start `python -m company_sim` yourself. |
| Data looks wrong or a previous run changed things | Press **Reset environment now** in the dashboard (disabled during a run) or `POST http://localhost:8001/admin/reset`. Deleting `company_sim/data/` also forces a clean reseed on next start. |
| `409 Run ... is still active` | Only one run at a time; cancel the active run or wait. |
| Browser window does not appear | Switch on **Headed browser** in the dashboard or pass `--headed` to the CLI; `HEADLESS=true` is the default. |
| Eval or test failures because the date moved | Seed dates are relative to today. Pin them with `SIM_TODAY=YYYY-MM-DD` in the shell when starting the simulator. |
