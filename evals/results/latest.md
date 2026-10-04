### Eval results (2026-10-03 23:13, gemini / gemini-3.5-flash > gemini-3.6-flash > gemini-2.5-flash > gemini-3.8-flash > gemini-3.5-flash-lite, sim date 2026-10-03)

Success is judged by deterministic checks against the simulated company's database, not by the agent's claims and not by an LLM. `Agent said` is the final status the agent reported; `Ground truth` is the checker's verdict.

| Scenario | Chaos | Agent said | Ground truth | Score | Steps | LLM calls | Tokens | Retries | Approvals | Questions | Model | Time |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `invoice_happy` | - | succeeded | PASS | 1.00 | 33 | 24 | 134557 | 0 | 1/1 | 0 | gemini-2.5-flash (+6 switch) | 303s |

**1/1 passed**, 0 false success(es), mean score 1.00, 24 LLM calls, 134557 tokens, 303s total.

Scenarios: `invoice_happy` = Latest invoice to ERP (happy path).
