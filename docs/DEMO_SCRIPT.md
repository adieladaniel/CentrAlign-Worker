# Demo video script (target: 4 to 5 minutes)

A shot-by-shot plan for recording the walkthrough. Runs on the Gemini free tier are slow (a call every few seconds), so the plan below assumes you speed up or cut the waiting in editing, and keep narration over the interesting moments. Replace the link placeholder in the [README](../README.md) (`> Demo video: <add link>`) when the video is uploaded.

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
| 2 | 0:25 to 1:55 | Select **Latest invoice to ERP (happy path)**, switch on **Headed browser**, press **Run task**. Show the plan appearing, then the Chromium window working. | "The task is the one from the brief: find the latest invoice from Company X, enter it in our internal system, and tell me when done. Nothing in the agent knows what an invoice is. On its first turn it restates the goal, defines success criteria, and writes a plan. It looks in the shared workspace, finds the logins, signs in to the vendor portal. Note the invoice list is paginated and not sorted by date, so it has to work out which one is latest. The amount and due date are not on the list: it opens the detail page or downloads the PDF and reads it. Facts it will need later go into the memory panel with where they came from." |
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

## Recording tips

- Narrate what the system decides, not what you click. The interesting moments are the plan, the approval card, the retry, the question, and the verification checks.
- Use the timeline's step list as the source of truth when explaining; each step shows the thought, tool, arguments, result and screenshot.
- Keep each segment under the limits in the table. Speed up waiting time rather than cutting content.
- Free-tier 429 errors are normal under load; the client waits and retries. If the run stalls for more than a minute, stop and retake with a larger `LLM_MIN_INTERVAL_S`.
