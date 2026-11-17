# ATL agent guide: paying through the Agent Trust Layer

This guide is for an AI agent (or its builder) that has to pay invoices. It is the only document you need.

## What ATL offers

ATL is a payment guard. You never hold payment credentials. You submit each payment to ATL, ATL checks it
against a **mandate** the account owner signed, and answers **approve**, **escalate** (the owner must decide)
or **refuse**. On approve, ATL pays the payee itself (Pix in Brazil today; Stripe in the US is in development)
and returns an Ed25519-signed, hash-chained **receipt** that anyone can verify.

## Access

ATL is an MCP server over stdio. Your owner configures it in your MCP client, for example:

```json
{"mcpServers": {"atl": {"command": "uv", "args": ["run", "--directory", "/path/to/agent-trust-layer",
  "python", "-m", "atl.mcp_server"], "env": {"ATL_RAIL": "simulated", "ATL_VAR_DIR": "/path/to/atl-state"}}}}
```

Your owner also gives you two identifiers: your **agent_id** and the **mandate_id** you pay under. You
cannot create or change a mandate: only the owner can sign one.

## Tools

| Tool | When | Inputs | Output |
|---|---|---|---|
| `pay_check` | Always first, once per invoice | `mandate_id`, `agent_id`, `payee_id`, `pix_key`, `amount_cents`, `category`, `invoice_ref`, `memo`, `idempotency_key` (US payees: `destination` and `currency: "USD"` instead of `pix_key`) | `decision_id`, `verdict`, `status`, `rule_hits`, `reasons`, `next_action` |
| `execute_payment` | Only when `next_action` is `call_execute_payment` | `decision_id` | `transfer_id`, `receipt_id`, `status` |
| `get_decision` | While `next_action` is `wait_for_owner` | `decision_id`, `wait_seconds` (0 to 30) | same fields as `pay_check`, plus `transfer_id`, `receipt_id` once paid |
| `verify_receipt` | After paying, before you report success | `receipt_id` (or the full receipt) | `valid`, `reasons`, `payee_id`, `amount_cents`, `transfer_id`, `rail` |
| `get_receipt` | To attach the full signed receipt | `receipt_id` | the receipt JSON |
| `register_mandate` | Owner only | a signed mandate | (fails without the owner's signature) |

Input notes:
- `amount_cents`: integer cents (R$ 450,00 is `45000`).
- `pix_key`: the key printed on the invoice, copied exactly.
- `memo`: the invoice text or description, copied verbatim. Do not rewrite it.
- `idempotency_key`: one stable value per invoice (for example the invoice number). Reusing it for the same
  invoice is safe; ATL treats a second, different request for the same invoice as a duplicate.

## next_action

| `next_action` | What you do |
|---|---|
| `call_execute_payment` | Call `execute_payment` with the `decision_id`. Safe to call twice: you get the same receipt |
| `wait_for_owner` | Do not pay. Poll `get_decision` with `wait_seconds=30`. Escalations expire after 15 minutes |
| `do_not_pay_report_to_owner` | Do not pay and do not retry with changed values. Report `reasons` to the owner |
| `already_executed` | Already paid. Use the `receipt_id` |

## Limits (rules R1 to R11)

Refused: invalid or expired mandate (R1), payee not in the mandate (R2), Pix key or destination different
from the registered one (R3), duplicate invoice (R5), category not allowed for that payee (R9), over the
hard cap (R10), currency different from the mandate's (R11). Escalated to the owner: instruction-like text
in the memo (R4), over the per-payment cap (R6), over the daily cap (R7), split-payment pattern (R8).

## Example

```text
pay_check(mandate_id="mdt_x", agent_id="ap-agent-01", payee_id="acme", pix_key="pay@acme.com.br",
          amount_cents=45000, category="office_supplies", invoice_ref="NF-1042",
          memo="NF-1042 paper and toner", idempotency_key="NF-1042")
  -> {"decision_id": "dec_...", "verdict": "approve", "next_action": "call_execute_payment", ...}
execute_payment(decision_id="dec_...")   -> {"status": "executed", "transfer_id": "...", "receipt_id": "rcp_..."}
verify_receipt(receipt_id="rcp_...")     -> {"valid": true, "amount_cents": 45000, "payee_id": "acme", ...}
```
