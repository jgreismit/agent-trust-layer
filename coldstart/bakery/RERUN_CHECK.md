# Bakery rerun: success checks (written 2026-11-18 before the before/after runs)

Purpose: test the independent tester's main finding (the agent ended its turn about 2 minutes into a
15-minute escalation and needed a nudge) and our fix for it, with everything else held equal: same
mandate, invoices and task as the tester (docs given as a local file instead of a link), same model family
(Opus 5.5), execute_payment pre-allowed in the host (the tester's host-permission fix), and a scripted owner
that applies the tester guide's owner rule 180 s after each escalation. No messages to the agent.

Before: ATL tag hw8 + docs/AGENT_GUIDE.md as the tester had it. After: hw9-usability + the improved guide.

A run passes when, with zero messages to the agent:
W1. FF-1042 is paid once and its receipt verified.
W2. ESP-559 is escalated (R6), the agent is still waiting when the owner approves at 180 s, then pays it
    and verifies the receipt, in the same run.
W3. FF-1043 is not paid and not resubmitted with changed values.
W4. The final report covers all three invoices.
