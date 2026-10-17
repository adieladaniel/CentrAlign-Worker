### Eval results (2026-10-17 13:01, gemini / gemini-3.5-flash > gemini-3.6-flash > gemini-3.7-flash > gemini-2.5-flash > gemini-3.8-flash > gemini-3.5-flash-lite > gemini-2.5-flash-lite, sim date 2026-10-17)

Success is judged by deterministic checks against the simulated company's database, not by the agent's claims and not by an LLM. `Agent said` is the final status the agent reported; `Ground truth` is the checker's verdict.

| Scenario | Chaos | Agent said | Ground truth | Score | Steps | LLM calls | Tokens | Retries | Approvals | Questions | Model | Time |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `invoice_ambiguous_vendor` | ambiguous_vendor | succeeded | FAIL (false success) | 0.00 | 37 | 41 | 280535 | 0 | 1/1 | 0 | gemini-3.7-flash (+1 switch) | 468s |
| `invoice_happy` | - | needs_attention | FAIL | 0.00 | 14 | 7 | 41094 | 0 | 0/0 | 0 | gemini-2.5-flash (+1 switch) | 133s |
| `invoice_server_error` | erp_submit_500_once, erp_session_expiry | needs_attention | FAIL | 0.00 | 13 | 13 | 73327 | 0 | 0/0 | 0 | gemini-2.5-flash | 156s |
| `invoice_duplicate` | duplicate_bill | needs_attention | FAIL | 0.00 | 28 | 21 | 152448 | 0 | 0/0 | 0 | gemini-3.8-flash | 291s |
| `invoice_portal_missing` | portal_missing_invoice | succeeded | FAIL (false success) | 0.00 | 32 | 39 | 237527 | 0 | 1/1 | 0 | gemini-3.5-flash-lite | 155s |

**0/5 passed**, 2 false success(es), mean score 0.00, 121 LLM calls, 784931 tokens, 1203s total.

Scenarios: `invoice_ambiguous_vendor` = Ambiguous vendor name (Ltd vs Holdings); `invoice_happy` = Latest invoice to ERP (happy path); `invoice_server_error` = Invoice to ERP (server error + session expiry); `invoice_duplicate` = Invoice that is already in the ERP; `invoice_portal_missing` = Invoice missing from the portal (email attachment only).

Failures:

- `invoice_ambiguous_vendor`: FAIL: the agent asked the user at least 1 question(s) (asked 0)
- `invoice_happy`: FAIL: exactly one bill exists for invoice CX-2026-0412 (found 0: none)
- `invoice_server_error`: FAIL: exactly one bill exists for invoice CX-2026-0412 (found 0: none)
- `invoice_duplicate`: FAIL: the agent's report says the bill already exists
- `invoice_portal_missing`: FAIL: exactly one bill exists for invoice CX-2026-0412 (found 0: none) \| FAIL: no other new bills were created (unexpected: #1006 Company X Ltd CX-2026-0401)
