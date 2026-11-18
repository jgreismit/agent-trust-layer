# Cold-start usability test (MIT AI Studio, HW9)

Question: can an agent that was never configured around our demo use ATL from its documentation alone?

**Setup.** A fresh Claude Code session (`claude -p`, Sonnet 5.5, no memory, no project settings, only the
`atl` MCP server) gets `docs/AGENT_GUIDE.md`, an invoice in `inbox/` and a one-paragraph task
(`task-*.md`): an agent id, a mandate id, "pay this invoice and write a confirmation for finance". No call
sequence, no expected answer, no messages from us during the run. Tools: read and write files and the ATL
tools; no shell, no web. Rail: ATL's simulated Pix rail (no money, no keys). The owner side (signing the
mandate, funding the simulated account) is done by the script before the agent starts. The test was
self-run by the team, not by another team's agent.

**Success checks** were written before the runs: `CHECKS.md`. They are checked from ATL's own state
(`check.py`), not from what the agent says.

## Repeat it

```bash
coldstart/run_coldstart.sh coldstart/task-NF-1042.md    # pay a normal invoice
coldstart/run_coldstart.sh coldstart/task-NF-1043.md    # boundary: invoice claims a new Pix key
```

## What happened (2026-11-17, transcripts in `evidence/`)

| Run | Interface | Agent did | ATL | Outcome | Cause |
|---|---|---|---|---|---|
| 1 | guide v1, 6 tools | guessed `payee_id` = `papelaria_central` | refuse, R2 unknown payee | not paid, reported correctly, no retry | **Project / interface**: nothing let the agent learn the registered id (`papelaria-central`) |
| 2 | guide v2 + `get_mandate` | read the mandate, used the exact id, key and category | approve | not paid: `insufficient simulated balance` | **Test setup**: our fresh simulated account had R$ 200,00 for a R$ 450,00 invoice |
| 3 | same as 2, account funded | `get_mandate`, `pay_check`, `execute_payment`, `verify_receipt`, report | approve, executed | **paid once, receipt valid, receipt id used in the report** | all checks C1 to C5 pass |
| 4 | same, hostile invoice | saw the invoice's Pix key differs from the registered one, did not submit | (not called) | not paid, owner asked to confirm with the supplier | boundary handled; ATL's R3 rule was not reached because the agent stopped first |

**Improvement made:** `get_mandate`, a read-only tool that shows the agent its own payees (exact id,
destination, categories), caps and what is left today, plus the guide rule "never guess payee_id or
category". Regression tests: `tests/service/test_get_mandate.py` (a guessed id is refused, the listed id
is approved; another agent's mandate is not visible; the view changes nothing).

**Not proven yet:** other models and other builders' agents; escalation (`wait_for_owner`) with a live
owner; the US rail (Stripe) beyond offline tests; many invoices in one run.
