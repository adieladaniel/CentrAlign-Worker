# Architecture

This document describes how the worker is built, in enough detail to review or extend it. Everything here reflects the code in this repository; file links are relative. For setup and the rationale behind the major decisions, see the [README](../README.md).

Contents: [1 Components](#1-components) | [2 Run lifecycle](#2-run-lifecycle) | [3 Context construction](#3-context-construction) | [4 State model](#4-state-model) | [5 Event model](#5-event-model) | [6 Browser observation](#6-browser-observation) | [7 Risk classification and approval](#7-risk-classification-and-approval) | [8 Verifier](#8-verifier) | [9 Guards](#9-guards) | [10 Reliability matrix](#10-reliability-matrix) | [11 LLM layer](#11-llm-layer) | [12 Simulated company](#12-simulated-company) | [13 Extension points](#13-extension-points)

---

## 1. Components

```
            +-------------------+            +---------------------------+
            | Dashboard (JS)    |            | CLI: python -m worker.agent|
            | worker/ui/        |            | worker/agent/cli.py       |
            +---------+---------+            +-------------+-------------+
                      | REST + WebSocket                   | AutoBroker + terminal prompts
                      v                                    v
            +---------------------------------------------------------------+
            | worker/server.py  (FastAPI :8000)                             |
            |  RunSession: runner + InteractionBroker + event list + jsonl  |
            +-------------------------------+-------------------------------+
                                            |
                                            v
  +-----------------------------------------------------------------------------+
  | AgentRunner  worker/agent/runner.py          (one per task, task-agnostic)  |
  |                                                                             |
  |  state.py     RunState {plan, facts, steps, usage, status, final_report}    |
  |  context.py   RunState -> messages for the next LLM call                    |
  |  llm/         ResilientLLM -> GeminiClient | OllamaClient | ScriptedLLM      |
  |  policy.py    (tool, args, risk, mode) -> approval needed?                  |
  |  broker.py    InteractionBroker: approval cards + questions <-> a human     |
  |  tools/retry  execute_with_retry: transient failures retried with backoff   |
  |  guards.py    nudge / escalate / stop                                       |
  |  verifier.py  fresh-context auditor with the read-only tool subset          |
  |  report.py    final report dict + report.md                                 |
  +---------------------------------+-------------------------------------------+
                                    |  ToolRegistry (worker/tools/registry.py)
        +--------------+------------+-------------+----------------+-----------+
        | browser_*    | list/read/ | http_request | remember       | update_   |
        | (Playwright) | write_file |  (allowlist) | ask_user       | plan,     |
        |              | (sandbox)  |              |                | finish    |
        +------+-------+------+-----+------+-------+----------------+-----------+
               |              |            |
          Chromium        workspace/   httpx
               |         runs/<id>/downloads
               v                           v
        +-------------------------------------------------------+
        | company_sim (FastAPI :8001): /mail /portal /erp /admin |
        | SQLite + PDFs, deterministic seed, chaos flags         |
        +-------------------------------------------------------+
```

Module responsibilities:

| Module | Responsibility |
|---|---|
| [worker/agent/runner.py](../worker/agent/runner.py) | The loop: context, LLM call, per-call classify/approve/execute/record, guards, finish and verification, final report. Contains no task knowledge. |
| [worker/agent/state.py](../worker/agent/state.py) | Serializable `RunState`, `Step`, `Fact`, `PlanItem`; atomic save to `run.json`. |
| [worker/agent/context.py](../worker/agent/context.py) | Builds the LLM message list from state each turn. |
| [worker/agent/prompts.py](../worker/agent/prompts.py) | Task-agnostic system prompt, verifier prompt, and canned feedback messages. |
| [worker/agent/policy.py](../worker/agent/policy.py) | Approval policy (`auto`, `confirm_writes`, `confirm_all`) and the "approve similar" cache. |
| [worker/agent/guards.py](../worker/agent/guards.py) | Budget, timeout, failure streak and repetition detection. |
| [worker/agent/verifier.py](../worker/agent/verifier.py) | Independent audit and `Verdict`. |
| [worker/agent/broker.py](../worker/agent/broker.py) | Async request/response channel between runner and human; `AutoBroker` for tests, evals and the CLI. |
| [worker/agent/report.py](../worker/agent/report.py) | Report dict (status, summary, outputs, evidence, verification, stats, artifacts) and markdown. |
| [worker/tools/](../worker/tools) | Tool framework (`Tool`, `ToolResult`, `Risk`, registry, retry) and the concrete tools. |
| [worker/llm/](../worker/llm) | Neutral message types, provider adapters, resilience wrapper. |
| [worker/server.py](../worker/server.py) | REST and WebSocket API, run sessions, event log, scenario presets, simulator proxy. |

## 2. Run lifecycle

```
User        Dashboard/CLI     Runner                 LLM            Policy/Broker        Tools            Verifier
 | task          |               |                     |                 |                 |                 |
 |-------------->| POST /api/runs|                     |                 |                 |                 |
 |               |-------------->| run_started         |                 |                 |                 |
 |               |               |                                                                           |
 |               |   LOOP        | build_messages(state)                                                     |
 |               |               |-------------------->| tool calls      |                 |                 |
 |               |               | (turn 1 normally: update_plan with goal, success criteria, checklist)     |
 |               |               | for each call:                                                            |
 |               |               |   classify(risk) ---------------------->| needs approval? |                 |
 |               |<-------- approval_requested (exact action + memory) ----| yes             |                 |
 |  approve/deny |-------------->| POST /respond ------------------------->| resolves        |                 |
 |               |               |   denied -> step(error=denied) -> observation to the LLM                  |
 |               |               |   approved -> tool_started ---------------------------->| run + retry     |
 |               |               |   record Step, emit tool_finished, feed guards                            |
 |               |               | guards.check_progress -> nudge | ask the human | stop                     |
 |               |               |                                                                           |
 |               |               | finish(status=success, summary, outputs, evidence)                        |
 |               |               | status = verifying ------------------------------------------------------>|
 |               |               |                                      re-observes real system, read-only   |
 |               |               |<------------------------------------------------ submit_verdict ---------|
 |               |               | passed        -> succeeded                                                |
 |               |               | failed        -> feedback into next turn (max 2 correction rounds), else failed
 |               |               | inconclusive  -> needs_attention                                          |
 |               |<-- run_finished(status, report) + report.md                                               |
```

Step by step, as implemented in `AgentRunner._loop`:

1. **Start.** `run()` creates `runs/<id>/`, sets status `running`, saves `run.json`, emits `run_started`.
2. **Context.** `build_messages` assembles the prompt from `RunState` (section 3).
3. **LLM call** through `ResilientLLM` (`tool_choice="auto"`; `"any"` after a text-only reply). `LLMError` after retries ends the run as `needs_attention`. Usage is added to the run and a `usage` event is emitted. Any text is emitted as `thought`.
4. **No tool call.** The model answered in prose: the step is recorded as `no_tool_call`, a nudge is added, and the next call forces a tool. A second consecutive text-only reply ends the run as `needs_attention` using the text as the summary.
5. **Per call** (`_handle_calls`):
   - `finish` mixed with other calls in one turn is rejected (`finish_not_alone`).
   - After a failure earlier in the same turn, remaining calls are recorded as `skipped` (the situation has changed).
   - Otherwise `_execute`: look up the tool (unknown tool is an error observation listing valid names), classify the risk from the tool and its arguments, ask `ApprovalPolicy.needs_approval`, and if needed block on a human via the broker (status `waiting_approval`). A denial produces a `denied` step and a `DENIED` observation; the tool is never run.
   - Approved or ungated calls emit `tool_started`, run through `execute_with_retry`, and become a `Step` with duration, evidence paths and the retry count parsed from the observation. `update_plan` results are applied to `RunState` and emitted as `plan_updated`.
6. **Guards.** After each turn `check_progress` may return a nudge (appended to the next prompt) or an escalation (the runner asks the human "Keep trying / Stop the task"). Before each loop iteration `check_limits` enforces the step budget and timeout.
7. **Finish.** `finish` with status `failed` or `blocked` ends the run immediately (`failed` / `needs_attention`) without an audit. `success` starts verification (status `verifying`, `verification_started`).
8. **Verification outcome.** Passed: step marked OK, run `succeeded`. Failed: the auditor's findings become `VERIFIER FEEDBACK (unresolved)` in the state block and the loop continues; after more than 2 failed audits the run ends `failed`. Inconclusive (auditor ran out of steps or its LLM failed): `needs_attention`.
9. **Wrap-up.** The browser is closed, `build_report` assembles the report, `run.json` and `report.md` are written, and `run_finished` is emitted. An unexpected exception inside the loop still produces a report (status `failed`, reason recorded).

Final statuses: `succeeded` (verified), `failed`, `needs_attention` (blocked, guard stop, unreachable model, inconclusive audit, no tool calls), `cancelled`. Intermediate: `pending`, `running`, `waiting_approval`, `waiting_user`, `verifying`.

Run artifacts in `runs/<run_id>/`: `run.json` (full state, saved after every step), `events.jsonl` (dashboard sessions), `report.md`, `screenshots/NNN_label.png`, `downloads/`.

## 3. Context construction

The prompt is rebuilt from state on every turn instead of appending to a chat transcript ([context.py](../worker/agent/context.py)).

```
[system]  SYSTEM_PROMPT (task-agnostic behaviour rules)
[user]    Task from the user
          Today's date / Environment (tool names, workspace folder, intranet start page)
          History so far (older steps, one line each, max 60 lines, 150 chars per line)
[assistant tool calls] \  the most recent whole turns, covering at least 6 steps,
[tool results]         /  properly paired by tool_call_id; older turns' results cut at 2000 chars,
...                       the latest turn's results shown in full
          + appended to the newest message:
          === CURRENT STATE (action N of at most MAX_STEPS) ===
          Goal / Success criteria / Plan (checklist with marks) / Working memory (with provenance)
          VERIFIER FEEDBACK (unresolved)   (only after a failed audit)
          NOTICE: ...                      (guard nudges, text-only nudge)
```

Properties this buys:

- **Bounded cost.** Prompt size depends on the window and caps, not on run length.
- **Valid transcripts.** The window is cut on turn boundaries, so a tool result is never separated from its call (a provider error otherwise). Steps that are not tied to a tool call (approvals, system notes, verifier steps) never enter the transcript. `tests/test_agent_context.py` checks the pairing invariant over many histories.
- **Durable knowledge.** Anything the agent needs after its observation scrolls away must be in working memory or the plan; the system prompt instructs it to `remember` facts with sources as soon as it learns them.
- **Persisted observations** are capped at 8,000 characters per step, and event payloads at 2,000.

## 4. State model

All in [state.py](../worker/agent/state.py); `RunState.to_dict()` is what `run.json` and the API return.

```
RunState
  run_id, task, status, goal_summary
  success_criteria: list[str]
  plan:   list[PlanItem {id, text, status: pending|in_progress|done|blocked|skipped, note}]
  facts:  dict[key -> Fact {key, value, source, step}]
  steps:  list[Step]
  usage:  {llm_calls, input_tokens, output_tokens}
  final_report: dict | None
  started_at, finished_at

Step
  index, kind: llm | tool | approval | ask_user | verify | system
  thought, tool, args, risk (read|write|irreversible)
  ok, observation, error, evidence: list[path], duration_ms, retries
  timestamp, turn (which LLM reply produced it), tool_call_id (links to the model's call)
```

Step `error` values that are outcomes of policy or flow rather than tool malfunctions (`denied`, `skipped`, `verification_failed`) are excluded from failure counting by the guards and the report statistics.

## 5. Event model

Runner events are `(type, payload)`; every payload carries `run_id`. The server wraps each in an envelope `{seq, type, ts, payload}`, keeps them in memory, appends them to `runs/<id>/events.jsonl`, and streams them over `WS /api/runs/<id>/events` (new subscribers first receive a replay, then a `replay_done` marker, so the dashboard can reconnect mid-run). For runs that have no event log (for example started from the CLI) the server synthesizes events from `run.json`.

| Event | Payload (main fields) | Meaning |
|---|---|---|
| `run_started` | `task`, `approval_mode` | Run began |
| `thought` | `turn`, `text` | Model's reasoning text for a turn |
| `plan_updated` | `plan`, `goal_summary`, `success_criteria` | `update_plan` applied |
| `tool_started` | `turn`, `tool`, `args`, `risk`, `action` | About to execute (after any approval) |
| `tool_finished` | all `Step` fields plus `phase` (`work` or `verify`) | Step recorded, including denied, skipped, failed and verifier steps |
| `memory_updated` | `key`, `value`, `source`, `step` | `remember` stored a fact |
| `approval_requested` | `request_id`, `tool`, `action`, `args`, `risk`, `context {reason, facts}` | Human must approve or deny |
| `approval_resolved` | `response`, `answered` | Decision arrived (or none, on cancel) |
| `question_asked` | `request_id`, `question`, `options` | `ask_user` or guard escalation |
| `question_answered` | `response`, `answered` | Answer arrived |
| `guard_triggered` | `guard` (`failures`, `repetition`, `budget`, `timeout`), `level` (`nudge`, `escalate`, `stop`), `message` | A guard fired |
| `verification_started` | `criteria` | Auditor started |
| `verification_finished` | `passed`, `inconclusive`, `checks`, `summary` | Audit result |
| `usage` | `llm_calls`, `input_tokens`, `output_tokens`, `llm_retries` | Cumulative accounting |
| `run_finished` | `status`, `report` | Terminal event |

Server-only: `replay_done` (`live` flag) and `error` (unknown run on the WebSocket).

HTTP API ([server.py](../worker/server.py)): `POST /api/runs {task, approval_mode?, headed?}`, `GET /api/runs`, `GET /api/runs/{id}`, `POST /api/runs/{id}/respond {request_id, approved|answer, note?, remember?}`, `POST /api/runs/{id}/cancel`, `GET /runs/{id}/files/...` (screenshots, report.md), `GET /api/scenarios`, `POST /api/sim/reset`, `GET /api/sim/chaos`, `GET /api/health`.

## 6. Browser observation

The agent never receives pixels. [snapshot.js](../worker/tools/snapshot.js) runs in the page, tags each visible interactive element with `data-agent-ref="N"`, and returns text (headings marked, tables as `|`-separated rows, links and buttons annotated with their ref) plus one record per element. [snapshot.py](../worker/tools/snapshot.py) formats it. Limits per view: 3,500 characters of text and 80 elements; longer pages are split into parts reachable with `browser_scroll`, or searched with `browser_read_page(find=...)`.

Real output from the simulated vendor portal, page 1 of the invoice list:

```
URL: http://localhost:8001/portal/invoices
Title: Invoices - Supplier Billing Portal
--- Page text ---
# Invoices
Vendor
Filter[4]
Invoice # | Vendor | Issue date | Status
GLX-88213[5] | Globex Corporation | 2026-09-19 | Open
INI-20417[6] | Initech Supplies | 2026-08-02 | Paid
CX-2026-0388[7] | Company X Ltd | 2026-07-20 | Paid
UMB-5531[8] | Umbrella Logistics | 2026-09-26 | Open
STK-1207[9] | Stark Components | 2026-08-31 | Paid
Page 1 of 3
Next[10]
(14 invoices)
--- Interactive elements ---
[1] link "Invoices" -> /portal/invoices
[2] button "Sign out"
[3] combobox "Vendor" selected="All vendors" options=[All vendors, Company X Ltd, Globex Corporation, Initech Supplies, Stark Components, Umbrella Logistics]
[4] button "Filter"
[5] link "GLX-88213" -> /portal/invoices/GLX-88213
[6] link "INI-20417" -> /portal/invoices/INI-20417
[7] link "CX-2026-0388" -> /portal/invoices/CX-2026-0388
[8] link "UMB-5531" -> /portal/invoices/UMB-5531
[9] link "STK-1207" -> /portal/invoices/STK-1207
[10] link "Next" -> /portal/invoices?page=2
```

And the ERP bill form (note the select options and format hints):

```
--- Interactive elements ---
[4] combobox "Vendor" selected="-- Select vendor --" options=[-- Select vendor --, Company X Holdings, Company X Ltd, Globex Corporation, Initech Supplies, Stark Components, Umbrella Logistics]
[5] textbox "Invoice number"
[6] textbox "Amount"
[7] combobox "Currency" selected="USD" options=[USD, EUR, GBP]
[8] textbox "Issue date" placeholder="YYYY-MM-DD"
[9] textbox "Due date" placeholder="YYYY-MM-DD"
[10] textbox "Notes"
[11] button "Save bill"
[12] link "Cancel" -> /erp/bills
```

Notes that matter for correctness:

- The list shows only issue date and status. Amount and due date require opening the detail page or the PDF, which the agent must download (`browser_download` then `read_file`, which extracts PDF text with pypdf).
- Refs are re-assigned on every snapshot. A ref that is not on the current page raises a `StaleRefError` whose message tells the agent to re-read the page.
- Invalid fields appear as `INVALID: <message>`; page alerts appear in an `Alerts:` header line; a 4xx/5xx navigation status is shown in the header.
- Side effects are reported as notes in the next observation: dialogs are auto-accepted and noted, new tabs are followed, downloads are saved to `runs/<id>/downloads/` and reported as `Downloaded: downloads/<file>`.
- Screenshots are written to `runs/<id>/screenshots/` after clicks, selects, submits and `browser_screenshot`, and attached to the step as evidence. They are for humans, not for the model.
- Not supported: iframes (skipped by the snapshot), file upload inputs, canvas-only content. The browser is Chromium at 1280x800; headed mode adds a 150 ms slow-motion delay for demos.

Browser tools: `browser_navigate`, `browser_click`, `browser_type`, `browser_select`, `browser_press`, `browser_scroll`, `browser_back`, `browser_read_page`, `browser_screenshot`, `browser_download`. A `BrowserSession` (one Chromium, one context) is stored in `ctx.extras["browser"]` and shared by the verifier.

## 7. Risk classification and approval

`Risk` has three levels: `read`, `write`, `irreversible`. A tool declares a default; `Tool.classify(args)` can refine it per call.

| Tool / action | Classification |
|---|---|
| `list_files`, `read_file`, `browser_navigate`, `browser_scroll`, `browser_back`, `browser_read_page`, `browser_screenshot`, `browser_download` | read |
| `write_file` | write |
| `http_request` GET, HEAD | read |
| `http_request` POST, PUT, PATCH | write |
| `http_request` DELETE | irreversible |
| `browser_click` on a link | read, unless the label is an action word: write for submit/confirm, irreversible for delete/remove/pay/approve/transfer |
| `browser_click` on a text box, checkbox, radio, select, tab, slider | read |
| `browser_click` on a button whose name matches delete/remove/pay/payment/approve/transfer | irreversible |
| `browser_click` on a button named like sign in/log in/log out/search/filter/find | read |
| `browser_click` on a button named submit/save/create/confirm/send/post/sign/reply/place order/cancel order | write |
| `browser_click` on a form-submitting button with any other name | write, unless named next/previous/back/apply/reset/clear/show/view/go/load more |
| `browser_press` Enter or Space, `browser_type` with `submit=true` | the highest risk among the submit buttons of the focused form (read if a textarea or no submit button) |
| `browser_type`, `browser_select` without submit | read |
| `remember`, `update_plan`, `finish`, `ask_user` | internal; never gated |

Classification of a click uses the element's role and accessible name from the latest snapshot (`classify_element` in [browser.py](../worker/tools/browser.py)); an unknown ref falls back to the tool's declared risk.

Approval policy ([policy.py](../worker/agent/policy.py)):

| Mode | Asks for |
|---|---|
| `auto` | irreversible only |
| `confirm_writes` (default) | write and irreversible |
| `confirm_all` | everything except internal tools and reads that are `read_only_safe` |

- The runner applies the policy before executing; the model has no way to bypass it, and it does not need to request approval (there is deliberately no `request_approval` tool).
- The approval card carries `action` (a one-line description with exact values, with password input masked), `args`, `risk`, and context: the model's stated reason and the current working memory.
- The human can approve, deny with a note, or approve with "remember" so similar calls (same tool and risk) skip the prompt for the rest of the run. Irreversible calls are always asked individually.
- Denial returns `User DENIED this action. Note: ...` as the observation, and the prompt instructs the agent to adapt rather than retry unchanged.
- In the dashboard there is no approval timeout. In the CLI the `AutoBroker` asks at the terminal. In evals a scripted responder approves or denies.

## 8. Verifier

Goal: catch "the agent says it is done but the system disagrees".

- **Trigger:** `finish(status="success")`. Other statuses skip the audit and are reported as `failed` or `needs_attention`.
- **Fresh context:** the auditor gets a new two-message conversation: its own system prompt and a briefing with the original task, the success criteria, the worker's claim (summary, outputs, evidence) and the worker's memory facts, all labelled unverified. It does not see the worker's step history or reasoning.
- **Read-only tools:** `registry.subset(read_only=True)` keeps only tools with `read_only_safe=True`, which excludes `write_file`, `browser_type`, `browser_select`, `browser_press`, `remember`, `update_plan`, `finish`, `ask_user`. Two further layers apply: any call whose per-call risk is not `read` is refused, and `ctx.extras["read_only"]` makes the browser tools refuse state-changing clicks and `http_request` refuse everything except GET.
- **Output:** it must call `submit_verdict(passed, checks[{criterion, passed, observed}], summary)`. The runtime recomputes `passed`: a verdict with no checks, or with any failed check, is downgraded to failed.
- **Budget:** at most 12 turns; on the last turn only `submit_verdict` is offered. Running out of turns, or an LLM failure, yields an *inconclusive* verdict, which the runner treats as `needs_attention` (never as success).
- **Feedback loop:** a failed verdict becomes `VERIFIER FEEDBACK (unresolved)` in the worker's state block along with an instruction to fix, re-check and `finish` again. After more than two failed audits the run ends `failed`.
- **Recording:** verifier tool calls are stored as steps of kind `verify` (emitted with `phase: "verify"`), do not count against the worker's budget or failure counters, and its token usage is added to the run.
- **Known weakness:** the auditor shares the worker's browser session (it needs the login state), cannot type, and is the same model family as the worker. See the README's limitations.

## 9. Guards

[guards.py](../worker/agent/guards.py), configured by `MAX_STEPS` (40), `MAX_CONSECUTIVE_FAILURES` (4) and `RUN_TIMEOUT_S` (900).

| Guard | Trigger | Response |
|---|---|---|
| Step budget | `MAX_STEPS` real actions (tool calls except `remember`, `update_plan`, `finish`, `ask_user`), or 3x that many steps in total | Stop: `needs_attention` with a partial report |
| Timeout | wall clock exceeds `RUN_TIMEOUT_S` | Stop: `needs_attention` |
| Failure streak | consecutive failed tool calls reach half the limit (2 by default) | One nudge: re-read state, check the error, change approach, consider the API path |
| Failure streak | reaches `MAX_CONSECUTIVE_FAILURES` | Escalate: ask the human "Keep trying / Stop the task"; guidance is injected into the next prompt and counters reset |
| Repetition | same tool, same arguments and same observation 3 times within the last 6 steps | First time a nudge; second time escalate |
| Cancel | `POST /api/runs/{id}/cancel` | Stop at the next safe point; also releases a pending approval or question |

Denials, skipped calls, rejected finishes and `ask_user` are not counted as failures.

## 10. Reliability matrix

| Failure | Detected by | Response | Where |
|---|---|---|---|
| Timeout, connection reset, detached element, `net::ERR_*` on an idempotent or read-level call | Exception type and message match, Playwright timeout | Tool layer retries with backoff (0.5 s, 1 s; 1 or 2 retries per tool); observation notes "after N automatic retries" | [retry.py](../worker/tools/retry.py), [browser.py](../worker/tools/browser.py) |
| HTTP 5xx from `http_request` (GET, HEAD, OPTIONS) | Status code | Marked transient and retried; if still failing the 5xx body is shown to the model | [http.py](../worker/tools/http.py) |
| HTTP 5xx, timeout or dropped connection on POST, PUT, PATCH, DELETE | Method plus status or exception | Never auto-retried (the write may have partly happened); non-transient failure with the note to GET and check whether the record exists before retrying | [http.py](../worker/tools/http.py), [retry.py](../worker/tools/retry.py) |
| Timeout or closed page on a state-changing browser action (click, press or typed submit classified above read) | Risk classification at call time | Never auto-retried; non-transient failure telling the agent to re-read the page and check whether the action took effect. Read-level actions keep the retry | [browser.py](../worker/tools/browser.py), [retry.py](../worker/tools/retry.py) |
| Browser or page closed (read-level action) | Error text match | Session reset, retried; next observation says the browser restarted | [browser.py](../worker/tools/browser.py) |
| Server error on a UI form submit (injected 500) | Page shows an error alert, status 500 in the header | Model reads the alert and retries the submit or switches to the ERP REST API | prompt guidance |
| Validation error (wrong date format, bad amount) | `INVALID` marker and alert in the snapshot | Semantic error: the model fixes the value (the prompt says to convert formats) and resubmits | snapshot, prompt |
| Stale element ref | `StaleRefError` | Message says refs were renumbered; model calls `browser_read_page` | [browser.py](../worker/tools/browser.py) |
| Wrong control for the action (typing into a select, clicking a text field) | Pre-checks in `browser_type`, `browser_select` | Error names the right tool and lists valid options | [browser.py](../worker/tools/browser.py) |
| Session expiry | Redirect to login page with an "expired" notice | Model logs in again with credentials kept in working memory | prompt, memory |
| Data missing where expected (invoice not in portal) | Search finds nothing | Model re-plans (`update_plan`) and looks in email attachments | prompt |
| Duplicate record | Prompt requires search-before-create; ERP rejects duplicates (409 / form error) | Model reports it or asks, instead of forcing a second entry | prompt, simulator |
| Ambiguity (two plausible vendors) | Model judgement guided by the prompt | `ask_user` with options | [interaction.py](../worker/tools/interaction.py) |
| Action denied by the human | `denied` step | Observation tells the model not to retry unchanged | [runner.py](../worker/agent/runner.py) |
| Model returns prose instead of a tool call | No tool calls in response | Nudge and force `tool_choice="any"`; second time `needs_attention` | [runner.py](../worker/agent/runner.py) |
| Model names an unknown tool or sends invalid arguments | JSON Schema validation | One corrective re-prompt inside `ResilientLLM`; the runner also reports unknown tools | [resilient.py](../worker/llm/resilient.py) |
| Gemini 429 or 5xx, malformed function call | Mapped to `RateLimitError` / `TransientLLMError` | Exponential backoff with jitter, honouring the server's retry hint, up to 5 retries; then `needs_attention` | [gemini.py](../worker/llm/gemini.py), [resilient.py](../worker/llm/resilient.py) |
| Repeated failures or identical actions | Guards | Nudge, then ask the human | [guards.py](../worker/agent/guards.py) |
| Step or time budget exhausted | Guards | Stop with a partial, honest report | [runner.py](../worker/agent/runner.py) |
| False success (claim not true in the system) | Verifier | Feedback loop; `failed` after repeated failed audits | [verifier.py](../worker/agent/verifier.py) |
| Verifier cannot decide | Out of turns or LLM error | `needs_attention`, reported as unverified | [verifier.py](../worker/agent/verifier.py) |
| Bug inside the runner | Exception handler in `run()` | Run ends `failed` with the partial report intact | [runner.py](../worker/agent/runner.py) |
| UI or event consumer broken | Exception in the event sink | Logged and ignored; the run continues | [runner.py](../worker/agent/runner.py) |

## 11. LLM layer

- [base.py](../worker/llm/base.py): neutral `Message`, `ToolSpec`, `ToolCall`, `LLMResponse`, `Usage`, and the error hierarchy (`LLMError`, `RateLimitError`, `TransientLLMError`). One method: `complete(messages, tools, temperature, tool_choice)`.
- [gemini.py](../worker/llm/gemini.py): `google-genai` with native function calling and automatic function calling disabled (the runner owns the loop). Converts schemas to the subset Gemini accepts (drops unsupported keys, turns free-form objects into JSON-encoded strings and decodes them on return), echoes the provider's `thought_signature` on later turns, merges consecutive tool results, maps SDK errors and extracts retry hints.
- [ollama.py](../worker/llm/ollama.py): `/api/chat` with tools; `tool_choice="any"` is advisory only.
- [resilient.py](../worker/llm/resilient.py): wraps any client with a minimum interval between calls (`LLM_MIN_INTERVAL_S`), retry with backoff, tool-argument validation with one corrective re-prompt, and counters.
- [fake.py](../worker/llm/fake.py): `ScriptedLLM` for tests; `LLM_PROVIDER=fake` selects an empty one.
- [factory.py](../worker/llm/factory.py): `make_llm(settings)`.

## 12. Simulated company

`python -m company_sim` serves, on port 8001:

- `/mail`: shared AP inbox (login), message view, PDF attachment download, compose and reply (writes to the Sent folder).
- `/portal`: vendor billing portal (login). Five invoices per page in internal-id order (not date order); the list shows number, vendor, issue date, status. Detail page and PDF show amount, due date and line items.
- `/erp`: "Ledger" accounts payable (login). Bills list and search, "New AP bill" form (vendor dropdown, invoice number, amount with at most 2 decimals, currency, issue and due dates strictly `YYYY-MM-DD`, due not before issue), duplicate rejection on (vendor, invoice number), approve action. REST API under `/erp/api` (`X-API-Key`; summary at `/erp/api/docs-summary`).
- `/admin`: chaos flags, reset, ground-truth state. Unauthenticated, for harnesses.

Data is seeded deterministically relative to "today" ([seed.py](../company_sim/seed.py)); chaos flags are in [chaos.py](../company_sim/chaos.py). See the README's reference section for credentials and flag descriptions. The simulator is a replaceable environment: the worker only knows a start URL, a workspace folder and an HTTP allowlist.

## 13. Extension points

### Add a tool

Subclass `Tool` ([base.py](../worker/tools/base.py)), set `name`, `description`, a JSON Schema `parameters`, and optionally `risk`, `read_only_safe`, `max_retries`, `classify(args)`, `retry_safe(args)`, `no_retry_note(args)` and `describe_action(args)`. Return a `ToolResult`; raise for failures (exceptions are converted to results, and timeouts, connection errors and 5xx are retried unless `retry_safe(args)` is False, which is the right answer for state-changing calls). Register it in `build_registry` in [toolset.py](../worker/agent/toolset.py).

```python
class LookupTool(Tool):
    name = "lookup_order"
    description = "Fetch an order by id from the order system."
    parameters = {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}
    # risk defaults to READ and read_only_safe to True, so the verifier may use it too

    async def run(self, args, ctx):
        ...
        return ToolResult(ok=True, observation="...")
```

Set `read_only_safe = False` for anything that changes state so the verifier never receives it, and set `risk` (or override `classify`) so the approval gate covers it. The Gemini schema sanitizer test (`test_all_registered_tool_specs_are_gemini_compatible`) will flag schemas the provider cannot accept.

### Add an LLM provider

Implement `LLMClient.complete` in `worker/llm/<name>.py` returning an `LLMResponse` with parsed `ToolCall`s and `Usage`, raising `RateLimitError` or `TransientLLMError` for retryable failures. Add a branch in [factory.py](../worker/llm/factory.py) and the name to the `LLM_PROVIDER` literal in [config.py](../worker/config.py). `ResilientLLM` wraps it automatically.

### Point it at a different web application

Nothing in the agent is specific to the simulator:

1. Set `SIM_BASE_URL` to the new start page (it is given to the model as the intranet start page and used to resolve relative `browser_navigate` paths).
2. Put the logins and any notes the agent needs in `WORKSPACE_DIR` (the agent is told to look there first).
3. Add the new API hosts to `HTTP_ALLOWED_HOSTS` if the agent should call them.
4. Leave prompts and tools unchanged. If the app needs login-heavy flows, nothing special is required: credentials are discovered from the workspace and kept in working memory.
5. For evals, add a scenario YAML and a checker in [evals/checkers.py](../evals/checkers.py) (register it in `CHECKERS`).

### Change policy

Set `APPROVAL_MODE`, or adjust the label patterns in [browser.py](../worker/tools/browser.py) (`_IRREVERSIBLE_RE`, `_WRITE_RE`, `_BENIGN_RE`) for an application whose buttons are named differently. Tests for the matrix are in `tests/test_agent_policy.py` and `tests/test_browser_tool.py`.
