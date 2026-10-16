# Demo video script (target: 4 to 5 minutes)

A shot-by-shot plan for recording the walkthrough (the single-user script first, then an optional multi-user segment at the end). Runs on the Gemini free tier are slow (a call every few seconds), so the plan below assumes you speed up or cut the waiting in editing, and keep narration over the interesting moments. Replace the link placeholder in the [README](../README.md) (`> Demo video: <add link>`) when the video is uploaded.

## Before recording

1. Fresh start: stop anything on ports 8000/8001, then `python run.py --open`.
2. Confirm the header badge shows the provider and model, and the environment indicator is green.
3. Delete old runs if you want a clean history: remove the `runs/` folder while the server is stopped.
4. Rehearse once with each of the example tasks you plan to show; LLM behaviour varies, so keep a clean take.
5. Layout: dashboard on the left half of the screen, the Chromium window on the right (headed mode), so both are visible. Browser zoom at 100 percent; record only the two windows, not the terminal.
6. Optional: set `LLM_MIN_INTERVAL_S=2` in `.env` for the recording session if your quota allows. Restore it afterwards.
7. Pre-run `python -m evals.run` and have `evals/results/latest.md` open for the last shot. It makes real LLM calls and takes several minutes.
8. Have the simulated ERP bills page open in a second tab (`http://localhost:8001/erp/bills`, login `ap.clerk` / `ledger-pass-789`).

## Shot list

| # | Time | On screen | Narration (talk track) |
|---|---|---|---|
| 1 | 0:00 to 0:25 | Dashboard landing page, idle. Briefly the three simulated apps in tabs (mail, vendor portal, ERP). | "This is an autonomous task worker. You give it a business goal in plain language; it plans, uses a real browser and files, recovers from problems, asks before changing anything, and checks its own work. It runs against a simulated company with mail, a vendor portal and an ERP, built with deliberate traps so everything you see is real behaviour, not a mock." |
| 2 | 0:25 to 1:55 | Select **Latest invoice to ERP (happy path)**, switch on **Headed browser**, press **Run task**. Show the plan appearing, then the Chromium window working. | "The headline task: find the latest invoice from Company X, enter it in our internal system, and tell me when done. Nothing in the agent knows what an invoice is. On its first turn it restates the goal, defines success criteria, and writes a plan. It looks in the shared workspace, finds the logins, signs in to the vendor portal. Note the invoice list is paginated and not sorted by date, so it has to work out which one is latest. The amount and due date are not on the list: it opens the detail page or downloads the PDF and reads it. Facts it will need later go into the memory panel with where they came from." |
| 3 | 1:55 to 2:30 | The approval card appears before the ERP save. Zoom on the exact values. Click **Approve**. Then switch to the ERP tab and show the new bill. | "Before it writes to the ERP, the runtime stops it. This gate is enforced by the code, not by the model's good manners: the click is classified as a write, so a human sees the exact values and the memory behind them. I approve. Date format conversion is the agent's job: the invoice shows one format and the ERP requires year-month-day. Here is the bill in the real ERP." |
| 4 | 2:30 to 3:05 | Final report card: summary, outputs, **Verification** checks with observed values, evidence thumbnails, stats (steps, LLM calls, retries). | "When it says it is finished, that claim is not trusted. A separate verifier with a fresh context and read-only tools re-opens the system and checks each criterion: the bill exists, the amount, the due date, no duplicate. These checks show what it actually saw. Every screenshot is evidence a person can click through." |
| 5 | 3:05 to 3:40 | Select **Invoice to ERP (server error + session expiry)** (it sets the chaos flags; leave **Reset environment before each run** on). Run it, headed or headless. Scroll the timeline to the failing step, the retry note, and the re-login. | "Same task, now with injected failures: the first ERP submit returns a server error, and the session expires partway through. Transient errors are retried by the tool layer with backoff; the model only sees the final outcome. A session expiry is a semantic problem, so the model sees the login page, re-authenticates with the credentials it remembered, and continues. The result is still one correct bill." |
| 6 | 3:40 to 4:05 | Select **Ambiguous vendor name (Ltd vs Holdings)**. Run it; when the question card appears, answer "Company X Ltd". | "Now there are two Company X entities with invoices issued the same day. The agent does not guess: it asks one precise question with options, then continues. Asking is reserved for real ambiguity; it is told to look things up first." |
| 7 | 4:05 to 4:30 | Select **Reply to a vendor payment-date question** (or **Record every open invoice due this month**). Run it; show the Sent folder or the new ERP bills when done. | "A completely different task, same code: reply to a vendor who asked when an invoice will be paid. No code changed. The agent finds the email, looks up the due date, and sends a reply, which the mail system now shows in Sent. The monthly backfill task works the same way across several invoices." |
| 8 | 4:30 to 5:00 | `evals/results/latest.md` or the terminal table from `python -m evals.run`. | "Behaviour is measured, not asserted. Each scenario resets the environment with a chaos configuration, runs the unchanged agent, then a deterministic checker reads the simulator's database: right vendor, exact amount and due date, no duplicates, nothing written after a denial. The agent's own claim never counts, and a success claim that the data contradicts is flagged as a false success. Thanks for watching." |

## About showing the verifier catching a problem

A genuine verifier rejection is not something you can reliably trigger on demand with a live model, and faking one would be dishonest. Options, in order of preference:

1. If a take happens to include a failed audit during a rehearsal, keep it: it shows the red check, the feedback going back to the agent, and the fixed second attempt.
2. Otherwise, in shot 4, point at the evidence the verifier produced (the `observed` text per criterion) and say that a failed check feeds back into the loop, with at most two correction rounds before the run is reported as failed.
3. For a concrete demonstration, run `pytest tests/test_agent_runner.py -k verif -v` on screen. `test_verifier_failure_feeds_back_and_worker_fixes` and `test_verification_failing_repeatedly_ends_failed_honestly` replay a scripted false success being caught and corrected.

## Optional extras (if time allows)

- **Approval denied:** run **Approval denied (the gate must hold)** and deny the write. Show the agent adapting and the report ending honestly instead of claiming success.
- **Duplicate:** run **Invoice that is already in the ERP**; the agent should report the existing bill rather than enter a second one.
- **Invoice missing from the portal:** shows re-planning to the email attachment.
- **Run history:** open a finished run from the history list to show the persisted timeline and `report.md`.

## Optional segment: multi-user mode (about 3 to 4 minutes)

Shows what v2 adds around the same agent: people with roles, a queue with worker processes, approvals answered by a second person, crash recovery, governed memory and the operations cards. Record it as a separate take after the single-user script, or cut it in after shot 5.

### Before recording this segment

1. PostgreSQL installed, `DATABASE_URL` set in `.env` (see the README, section 1.4). Choose the demo password yourself so you know it: `python scripts/bootstrap.py --demo-password "your-password"`. The demo logins come from bootstrap: `operator@acme.test`, `approver@acme.test`, `admin@acme.test` and so on. If you let bootstrap generate the password it is printed only once, when the users are first created, and never again.
2. Start everything with two workers: `python run.py --workers 2` (the workers are named `local-1` and `local-2`). In a second terminal give the demo orgs their simulated companies: `python scripts/bootstrap.py --provision-sim` (safe to repeat).
3. Two browser windows side by side: a normal window signed in as `operator@acme.test`, and a **private window** signed in as `approver@acme.test` (separate cookies, so two sessions). Optionally a third tab as `admin@acme.test` for the Usage and System cards.
4. Optional, to keep the crash-recovery wait short: `JOB_LEASE_S=10` in `.env` before starting (the default lease is 30 seconds).
5. Rehearse once. For a predictable memory note and no model quota you can start the stack with `LLM_PROVIDER=policy`; that is a rule-based stand-in for the model (see docs/SCALING.md) and you should say so on camera. With real Gemini a note appears in the Memory page only if the agent marks a reusable how-to fact as durable and the run passes the audit.

### Shot list

| # | On screen | Narration (talk track) |
|---|---|---|
| M1 | The sign-in screen, then the operator's dashboard. Point at the role badge and the organization name in the header. | "This is the multi-user version. People sign in with an account, belong to an organization and have a role. Everything you see is scoped to this organization: another organization's runs would answer not found." |
| M2 | Operator starts the headline task. Switch to the private window: the **Approvals** tab shows a live badge with the count. | "The API only queues the run. One of two worker processes claims it and does the work. The approver's inbox updates by push the moment the agent asks for approval, across all runs of the organization." |
| M3 | Back in the operator window, open the pending card for the ERP save: the buttons are disabled with the reason. Then in the approver window open the inbox card and approve. | "The operator started this run, so the organization's separation-of-duties rule stops them approving its irreversible actions; they could still deny. The card says why. The approver sees the exact values and the memory behind the action, approves, and the timeline records who approved." |
| M4 | Start two or three more tasks as the operator so they run on both workers (the Admin page's System card or the run list shows the worker per run, for an admin). Find the worker process of a running run (Windows PowerShell: `Get-CimInstance Win32_Process \| Where-Object CommandLine -match 'worker.jobs'`) and kill its whole tree: `taskkill /F /T /PID <pid>` (macOS/Linux: `kill -9 <pid>`). | "Now I kill a worker in the middle of runs, the hard way. Its runs hold a lease that it can no longer renew." |
| M5 | Watch the timeline of an affected run: `run_requeued`, then `run_resumed` and the agent re-checking the page. The run finishes `succeeded` on the other worker. | "When the lease expires another worker's reaper puts the run back in the queue and the next claim resumes it from its last checkpoint, in a fresh browser. The agent logs in again and checks whether its write already happened before repeating it. Even the independent audit can sign in again with read-only access, so it is not blind after a restart." |
| M6 | Optional: restart the API (stop and start `python -m worker.server`, or restart `python run.py`) while an approval card is open, then approve it. | "Approvals are database rows. The API restart lost nothing, and the answer still reaches the waiting run." |
| M7 | Open the **Memory** page as the operator, then as admin show edit and delete. | "After a run ends successfully, which means the independent audit passed, reusable how-to notes about the organization's systems are saved. Failed or unverified runs write nothing. Each note shows which run taught it, and admins can correct or remove it. Recall is keyword search, by design." |
| M8 | Admin tab: the **Usage** card (calls and tokens per day, per user, against the daily budget) and the **System** card (queue depth, running and waiting runs, the workers holding runs, finished in 24 hours, p50 and p95). Optionally `/metrics`. | "Every LLM call is accounted to an organization and a user, with a daily budget that stops a runaway. The System card shows the queue and the workers. The same numbers are exposed to Prometheus." |
| M9 | Optional: `docs/SCALING.md` results table. | "I load-tested this on one machine with a deterministic stand-in for the model: 280 of 280 runs succeeded and matched ground truth with a worker killed and the API restarted, and no response ever leaked another organization's data. The page also lists the caveats." |

Notes for the take: the demo passwords come from bootstrap (the value you passed to `--demo-password`, or the one printed once); do not show `.env`. If a run sits in the queue, its organization is at its concurrency limit (default 2), which is also worth showing.

## Recording tips

- Narrate what the system decides, not what you click. The interesting moments are the plan, the approval card, the retry, the question, and the verification checks.
- Use the timeline's step list as the source of truth when explaining; each step shows the thought, tool, arguments, result and screenshot.
- Keep each segment under the limits in the table. Speed up waiting time rather than cutting content.
- Free-tier 429 errors are normal under load; the client waits and retries. If the run stalls for more than a minute, stop and retake with a larger `LLM_MIN_INTERVAL_S`.
