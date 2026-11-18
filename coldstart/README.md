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

## Independent test and the waiting fix (2026-11-17 and 18)

**Independent run.** Another builder (Shahaleel Al Yousef, TrueMenu) ran her own Claude Code agent (Opus 5.5,
interactive, auto mode) against tag `hw8` with only `docs/AGENT_GUIDE.md` and the task in
`bakery/task.txt`; Marlon wrote the scenario and `bakery/PRE_RUN_CHECK.md` before her run. All three
outcomes matched the check (FF-1042 paid and verified; ESP-559 escalated by R6, approved by the owner, paid
and verified; FF-1043 refused by R3, R4 and R8 and not resubmitted). It took three interventions: the owner's
approval (the owner's normal role), a nudge after the agent **ended its turn about 2 minutes into a
15-minute escalation**, and allowing `execute_payment` after Claude Code's auto mode blocked it. Her raw
transcript and log stay with the team (not published here).

**Fix.** Every decision now carries `next_step` (one plain instruction) and an escalation carries
`expires_at`; the guide says not to end the turn while waiting, that an approved decision does not expire,
that only the agent executes, and how to allow `execute_payment` in the host. Also: a missing `ATL_RAIL`
now means the simulated rail and an unknown value is refused (`CONFIG_INVALID`). Regression tests:
`tests/service/test_wait_and_rail_default.py`.

**Rerun** (`run_bakery.sh`, checks in `bakery/RERUN_CHECK.md` written before): same scenario, fresh
`claude -p` session, Opus 5.5, `execute_payment` pre-allowed, a scripted owner applying the tester's owner
rule after a delay, no messages to the agent. Evidence in `evidence/bakery/`.

| Owner answers after | Before (tag `hw8` + tester's guide) | After (`hw9-usability` + new guide) |
|---|---|---|
| 3 min | waited (5 polls), paid ESP-559, verified | waited (5 polls), paid ESP-559, verified |
| 8 min, run 1 | **stopped after 4 polls (~2 min), ESP-559 left "waiting for you"** | waited (14 polls), paid ESP-559, verified |
| 8 min, run 2 | **stopped after 5 polls (~2.5 min), ESP-559 left unpaid** | waited (14 polls), paid ESP-559, verified |

```bash
coldstart/run_bakery.sh <checkout of tag hw8> <hw8 AGENT_GUIDE.md> claude-opus-5-5 480   # before
coldstart/run_bakery.sh . docs/AGENT_GUIDE.md claude-opus-5-5 480                          # after
```
