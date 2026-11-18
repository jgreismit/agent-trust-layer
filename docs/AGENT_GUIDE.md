# Agent Trust Layer (ATL) for agents

ATL answers one question for a paying agent: **may I make this payment, and can I prove what I paid?**
You never hold payment credentials. You submit each payment to ATL. ATL checks it against a mandate the
owner signed and answers **approve**, **escalate** (the owner decides) or **refuse**. ATL then makes the
approved payment itself and returns a signed receipt that anyone can verify.

ATL does not choose what to pay. You read the invoices and decide what to submit. ATL decides what may
actually be paid.

This guide matches ATL branch `hw9-usability`. Changes since tag `hw8` are marked **(new)**: they come from
our own cold-start test and from an independent tester's run on 2026-11-17.

## Access

| | |
|---|---|
| Interface | MCP server named `atl` (stdio), already connected by your owner |
| Tools | `get_mandate` **(new)**, `pay_check`, `get_decision`, `execute_payment`, `get_receipt`, `verify_receipt` (`register_mandate` is for the owner, not for you) |
| What your owner gives you | your `mandate_id` and your `agent_id`. Read the payees from `get_mandate` |
| Money | Brazilian reais, always as **integer cents** (R$ 64,90 = `6490`) |
| Rail in this deployment | simulated Pix rail: no real money moves |

**(new)** Never guess a `payee_id` or a category: call `get_mandate` and copy them exactly. If the
invoice's supplier or Pix key is not listed there, do not pay: report it to the owner. If you do not have a
`mandate_id` or `agent_id`, ask your owner: a wrong value is refused.

## The flow

0. **(new)** Call `get_mandate` once to read your payees, their exact Pix keys and categories, and your caps.
1. Call `pay_check` once per invoice.
2. Follow `next_action` exactly. Every response also has `next_step` **(new)**, one plain sentence saying
   what to do next:
   - `call_execute_payment`: call `execute_payment` with the `decision_id`. Only you can execute a payment:
     the owner approves or refuses, but has no command to pay for you.
   - `wait_for_owner`: the owner must decide. **Do not end your turn while you wait (new).** Call
     `get_decision` with `wait_seconds: 30` again and again until `next_action` changes or the escalation
     expires; the response tells you when (`expires_at`, 15 minutes after the escalation). You may handle
     your other invoices in between, but come back and keep waiting before you report.
   - `do_not_pay_report_to_owner`: do not pay. Report the `reasons` to the owner.
3. After paying, call `verify_receipt` with the `receipt_id` and report the result.

**(new)** An escalation expires 15 minutes after it was created if the owner does not answer. Once the
owner approves, the decision does not expire: execute it right away.

Never resubmit a refused payment with changed values (another key, a smaller amount, a different payee).
A refusal is the answer.

## Tools

### `get_mandate` **(new)**: read your own mandate

`{"mandate_id": "...", "agent_id": "..."}`. Read-only. Returns `payees` (each with `payee_id`,
`display_name`, the exact `pix_key`, `pix_key_type` and allowed `categories`), `currency`, the caps,
`spent_today_cents` and `remaining_today_cents`. Another agent's mandate answers `MANDATE_NOT_FOUND`.

### `pay_check`: submit a payment for a decision

| Field | Type | Meaning |
|---|---|---|
| `mandate_id` | string | Your mandate, from your owner. |
| `agent_id` | string | Your agent id, from your owner. |
| `payee_id` | string | The owner's id for this payee (for example `fresh-farms`), not the display name. |
| `pix_key` | string | The Pix key **you intend to pay**, as written on the invoice. |
| `amount_cents` | integer | Amount in cents. |
| `category` | string | What is being bought, for example `produce`. Must be a category the owner allowed for this payee. |
| `invoice_ref` | string | The invoice number. |
| `memo` | string | The invoice description, **copied verbatim**. ATL treats it as untrusted text. |
| `idempotency_key` | string | Unique per payment attempt. The invoice number is a good choice. Reusing a key is refused as a duplicate. |

Returns `decision_id`, `verdict` (`approve`, `escalate`, `refuse`), `status`, `rule_hits`, `reasons`
(one plain sentence per rule hit), `next_action`, `receipt_id` (a signed record of the decision itself)
and `original_decision_id` (set when it is a duplicate).

### `get_decision`

`{"decision_id": "...", "wait_seconds": 30}`. Returns the same fields plus `transfer_id` and `receipt_id`
once paid. `wait_seconds` (0 to 30) blocks while the owner has not answered.

### `execute_payment`

`{"decision_id": "..."}`. Works only when `next_action` is `call_execute_payment`. Pays exactly the
registered payee key and the checked amount. Safe to call twice: the second call returns the same
transfer. Returns `status: "executed"`, `transfer_id`, `receipt_id`, `rail`, `amount_cents`,
`rail_status`.

### `get_receipt` and `verify_receipt`

`get_receipt {"receipt_id": "..."}` returns the full signed receipt (decision, intent, transfer, hash,
signature). `verify_receipt {"receipt_id": "..."}` (or `{"receipt": {...}}` for a receipt someone handed
you) recomputes the hash, checks the ATL signature and returns `valid`, `reasons`, `payee_id`,
`amount_cents`, `transfer_id`, `rail`, `verdict`, `status`.

## The rules ATL applies

All rules run on every check and all hits are reported. Any refuse rule wins over escalate.

| Rule | Verdict | When |
|---|---|---|
| R1 mandate invalid | refuse | unknown, revoked, expired or tampered mandate, or wrong `agent_id` |
| R2 unknown payee | refuse | `payee_id` is not in the mandate |
| R3 key mismatch | refuse | `pix_key` differs from the key the owner registered for that payee |
| R5 duplicate | refuse | idempotency key reused, or the same invoice already paid |
| R9 category not allowed | refuse | category not allowed for that payee |
| R10 over hard cap | refuse | amount above the mandate's hard cap |
| R4 instruction in memo | escalate | the memo contains instruction-like text ("pay to", "new key", "changed our bank", "ignore previous") or a payment key next to a payment verb |
| R6 over per-payment cap | escalate | amount above the per-payment cap |
| R7 over daily cap | escalate | today's paid total plus this amount is above the daily cap (day in São Paulo time) |
| R8 split pattern | escalate | the payment looks like part of a larger order split under the cap, or recent payments to the same payee plus this one exceed the per-payment cap within 24 h |

## Errors

Tool errors come back as `{"error": "<CODE>", "message": "..."}`. The ones you may see:
`DECISION_NOT_FOUND`, `DECISION_NOT_APPROVED` (you called `execute_payment` on a decision that is not
approved), `RECEIPT_NOT_FOUND`, `INVALID_ARGUMENT` (a field is missing or has the wrong type),
`RAIL_ERROR` (the rail failed or the simulated balance is too low).

## Limits

- **Pix only**, in reais. The simulated rail in this deployment starts with a balance of R$ 200,00.
- **Escalations need a human.** If the owner does not answer within 15 minutes, the decision expires and
  `next_action` becomes `do_not_pay_report_to_owner`.
- **The rules are deterministic** (no model runs inside ATL). They can over-flag: a second, legitimate
  invoice from the same payee on the same day can trigger R8. That goes to the owner, it is not paid
  silently.
- **(new)** ATL uses the simulated rail unless your owner sets another one explicitly; an unknown rail
  setting stops ATL with `CONFIG_INVALID` instead of reaching a real rail.
- **For builders:** some agent hosts block tools that move money until you allow them. In Claude Code,
  allow `mcp__atl__execute_payment` for the agent's folder, otherwise an approved payment cannot be executed.

## Example

An invoice from Fresh Farms for R$ 64,90:

```json
pay_check {
  "mandate_id": "mdt_bakery_test", "agent_id": "bakery-payables-agent",
  "payee_id": "fresh-farms", "pix_key": "financeiro@freshfarms.example",
  "amount_cents": 6490, "category": "produce", "invoice_ref": "FF-1001",
  "memo": "Weekly produce delivery", "idempotency_key": "FF-1001"
}
-> {"decision_id": "dec_01M7...", "verdict": "approve", "status": "approved", "rule_hits": [],
    "reasons": [], "next_action": "call_execute_payment", "receipt_id": "rcp_01M7...",
    "original_decision_id": null}

execute_payment {"decision_id": "dec_01M7..."}
-> {"decision_id": "dec_01M7...", "status": "executed", "transfer_id": "sim_01M7...",
    "receipt_id": "rcp_01M7...", "rail": "simulated", "amount_cents": 6490, "rail_status": "..."}

verify_receipt {"receipt_id": "rcp_01M7..."}
-> {"valid": true, "reasons": [], "payee_id": "fresh-farms", "amount_cents": 6490,
    "transfer_id": "sim_01M7...", "rail": "simulated", "verdict": "approve", "status": "executed"}
```

An escalation looks like this:

```json
{"verdict": "escalate", "rule_hits": ["R6_OVER_PER_PAYMENT_CAP"],
 "reasons": ["R$ 128.00 is above the per-payment cap of R$ 100.00"], "next_action": "wait_for_owner"}
```
