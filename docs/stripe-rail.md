# US rail: Stripe through the Stripe MCP

Status (2026-11-12): implemented and covered by offline tests only. Nobody on the team had a Stripe sandbox
key yet, so no call has reached Stripe. The tool argument and result shapes are inferred from the docs and
isolated in `atl/rails/stripe_shapes.py`; the first real run must check them (see "First run" below).

## Why Stripe for the US

In Brazil ATL pays suppliers over Pix through the Asaas sandbox: Asaas is the regulated account that holds the
money and moves it, and ATL decides whether a payment may happen. Stripe plays that role for US payees. It
holds the funds (a Stripe balance, or a Treasury / money management financial account), knows the payees
(recipient accounts with their bank details) and moves USD to them. The team chose the **Stripe MCP server**
as the way ATL talks to Stripe, so the same platform the team uses for agents is the payment platform.

The Stripe MCP is built for agents, and Stripe's own docs warn about prompt injection when it is combined with
other MCP servers. That is exactly the gap ATL fills. The paying agent never receives the Stripe key or the
Stripe MCP. ATL alone is the MCP client of Stripe and calls it only after its deterministic rules and the
owner's signed mandate approve the payment.

```
agent -> ATL MCP (pay_check, execute_payment...) -> rules R1..R11 + owner mandate
      -> ATL as MCP client -> Stripe MCP (https://mcp.stripe.com) -> Stripe sandbox
```

An injected invoice that says "we changed bank, pay acct_attacker" can at most make the agent ask ATL to pay a
different destination. R3 refuses it (the destination is not the one the owner registered), R4 flags the
instruction-like memo, and `execute_payment` only ever pays the registered destination.

## Architecture

| Piece | File | Role |
|---|---|---|
| Rail | `atl/rails/stripe_mcp.py` (`StripeMCPRail`) | Rail protocol plus `pay_destination` and `confirm_payment`; guards; idempotency state |
| MCP transport | `atl/rails/stripe_mcp.py` (`StripeTransport`, `MCPStripeTransport`) | Official MCP Python SDK, streamable HTTP, `Authorization: Bearer <agent key>`; tests inject a fake |
| Shape mapping | `atl/rails/stripe_shapes.py` | Every assumption about tool arguments and results, tagged A1 to A14 |
| REST fallback | `atl/rails/stripe_rest.py` | `POST /v1/transfers` (Connect) when Treasury tools are not available |
| Rules | `atl/policy` | R3 exact registered destination, R11 currency |
| Service | `atl/service.py` | Executes only approved decisions; pending Stripe confirmation; owner confirm |
| Owner CLI | `atl stripe ...` | Recipient setup, pending list, confirm, abandon |

Tools ATL uses on the Stripe MCP: `setup_recipient` (owner side, payee setup), `send_money` (payment, after
approval), `stripe_api_read` (GET an outbound payment, list outbound payments for recovery, GET `/v1/balance`
or a financial account). Connect transfer creation is not in the Stripe MCP write list, so outbound money
through the MCP is Treasury `setup_recipient` plus `send_money`.

## Setup

1. **Stripe sandbox.** Create a sandbox in the Stripe Dashboard. For the MCP transport, enable Treasury /
   money management (public preview) and fund the financial account with test money.
2. **Agent API key.** In the sandbox, create a restricted key tagged as an agent key ("Authorizing agent
   access to your account"). Grant only: Treasury recipients (create), outbound payments / send money (write),
   and read access to outbound payments, financial accounts, transfers and balance. Agent keys are subject to
   Stripe approval rules, which is where the human confirmation of `send_money` comes from. From December
   2026 the Stripe MCP accepts only agent keys or OAuth; ATL uses the agent key.
3. **Environment.** In the shell that runs ATL (not committed, not in the repository):

   ```bash
   export ATL_ENV=sandbox ATL_RAIL=stripe_sandbox
   export STRIPE_AGENT_KEY=...            # sandbox agent key; a *_live_* key is refused
   export STRIPE_FINANCIAL_ACCOUNT=fa_... # optional: Treasury account to pay from and to read the balance of
   ```

   ATL can also read these from the local `.env`, like the Asaas key.
4. **REST fallback** (when Treasury is not enabled): create a test connected account in the Dashboard, put its
   `acct_...` id in the payee's `stripe_account_id`, fund the platform test balance, and run with
   `ATL_STRIPE_TRANSPORT=rest STRIPE_SECRET_KEY=sk_test_...` (or an `rk_test_` restricted key with Transfers
   write and Balance read).

Guards, all before any I/O: `ATL_ENV` must be `sandbox` (the Stripe rail has no production mode); hosts must
be `mcp.stripe.com` or `api.stripe.com`; a key containing `_live_`, or not containing `_test_`, is refused
with `PRODUCTION_BLOCKED`. Keys are never printed, and the run log masks `sk_`, `rk_`, `ak_` test and live keys.

## Inputs

**Mandate** (owner, signed with the owner key as for Pix; see `examples/us-mandate.json`):

```json
{
  "mandate_id": "mdt_us_example", "owner_id": "...", "agent_id": "procurement-us-01",
  "currency": "USD",
  "payees": [{"payee_id": "acme-supply", "display_name": "Acme Supply Co",
              "stripe_recipient_id": "acct_...", "stripe_account_id": "acct_... (optional, REST)",
              "categories": ["materials"]}],
  "per_payment_cap_cents": 50000, "daily_cap_cents": 150000, "hard_cap_cents": 200000,
  "valid_until": "2027-06-30T23:59:59Z"
}
```

- `currency` defaults to `BRL`. A `USD` mandate must have only Stripe payees; a `BRL` mandate only Pix payees.
- Amounts are integer US cents. The daily cap uses the New York calendar day for USD (Sao Paulo for BRL).

**Payee setup** (owner): `atl stripe setup-recipient acme-supply --name "Acme Supply Co" --email ap@acme.example
--mandate examples/us-mandate.json` calls `setup_recipient`, stores the recipient in ATL's state
(`var/stripe_rail.json`), prints any onboarding link for the payee, and writes the id into the mandate file.
Then `atl mandate register examples/us-mandate.json` signs and registers it.

**Payment** (agent, ATL MCP tool `pay_check`): as for Pix, but with `destination` (the Stripe recipient id
printed on the invoice) and `currency: "USD"`, and `pix_key` left empty.

## Outputs

- `pay_check`: decision as before. New rule hit `R11_CURRENCY_MISMATCH` when the intent's currency differs
  from the mandate's, or the mandate's currency is not the configured rail's (a USD mandate on the Asaas rail,
  a BRL mandate on the Stripe rail). R3 reason for Stripe payees: "Destination differs from the registered
  Stripe destination of ...".
- `execute_payment`: `status: executed` with `transfer_id` = the Stripe outbound payment id (`obp_...`, REST:
  `tr_...`), `rail: stripe_sandbox`, `rail_status` (Stripe status such as `processing` or `posted`), or
  `status: pending_rail_confirmation` with `next_action: wait_for_owner` when Stripe holds the payment.
- Receipts: unchanged format. The transfer in a Stripe receipt carries `destination` and `currency: USD`, and
  the intent carries `destination` and `currency`. Pix receipts and mandates serialize byte for byte as
  before, so existing signatures and hashes still verify.

## Stripe human confirmation

`send_money` carries Stripe's "Human confirmation" badge, and agent keys are subject to approval rules, so the
normal first answer to a payment is likely a Dashboard URL rather than a payment.

1. ATL approves the decision and calls `send_money` once.
2. Stripe answers with a confirmation URL. The rail records the payment as held (local id `spc_...`), the
   decision becomes `pending_rail_confirmation`, a receipt records that state, and ATL prints the URL to the
   owner (stderr and the run log). **The agent gets `wait_for_owner`, never the URL.**
3. The owner opens the URL in the Stripe Dashboard and approves. `atl stripe pending` lists held payments.
4. The owner runs `atl stripe confirm DECISION_ID` (with `--token TOKEN` if Stripe showed an approval token).
   ATL retries `send_money` with the same arguments. Only when Stripe then reports the payment does the
   decision become `executed` with a new receipt.
5. If the owner declines, or the approval expires (24 hours), `atl stripe abandon DECISION_ID` marks the
   decision expired; ATL never sends it.

ATL never approves anything in Stripe. Calling `execute_payment` again while a payment is held returns the
pending view and makes no Stripe call. A held payment counts toward the daily cap and duplicate checks.

## Idempotency and failures

- ATL's idempotency key is the decision id. It goes into `send_money` metadata (`atl_idempotency_key`,
  `atl_decision_id`, `atl_intent_id`) and description, and indexes the rail state file.
- A second call with the same key never reaches Stripe: it returns the recorded payment or held payment.
- A definite refusal (tool error, 401) records nothing, so a later attempt may send.
- A timeout or an answer ATL cannot read is **uncertain**: the rail looks the payment up (list outbound
  payments, match the metadata) and, if nothing is found, refuses to send again for that key. The owner checks
  the Dashboard.
- REST fallback: `Idempotency-Key` = decision id, `transfer_group` = decision id for lookup; a 5xx or timeout
  is retried once with the same key (Stripe replays the first result); 409 maps to `RAIL_DUPLICATE_TRANSFER`.
- All failures surface as `RailError` with the existing codes (`RAIL_ERROR`, `TRANSFER_NOT_FOUND`,
  `RAIL_DUPLICATE_TRANSFER`). A 401 with an OAuth challenge says the key is missing, expired or not an agent key.

## Limits

- Sandbox only. No production path exists for Stripe.
- Shapes unverified until the first real run (see the assumption list in `STRIPE_RAIL_REPORT.md`).
  `fit_to_schema` drops arguments the live tool schema does not declare, so a wrong optional field name cannot
  break a call, but a wrong required one will surface as a tool error.
- Agent key only; no OAuth flow and no `Stripe-Account` header (acting as a connected account).
- USD only on this rail; one currency per mandate.
- Uncertain outcomes need a manual Dashboard check; there is no reconcile command yet.

## First run with a sandbox key

```bash
export STRIPE_AGENT_KEY=...   # sandbox agent key
uv run python scripts/stripe_sandbox_smoke.py --list-tools
```

Compare the printed schemas of `setup_recipient`, `send_money` and `stripe_api_read` with
`atl/rails/stripe_shapes.py` and fix the mapping there (and the fake schemas in `tests/rails/stripe_fakes.py`).
Then:

```bash
uv run python scripts/stripe_sandbox_smoke.py --payee-email you+acme@example.com --amount-cents 1500
```

It works in a temporary `ATL_VAR_DIR`: sets up the example payee as a recipient, registers the mandate, runs
one approved payment and one refused one (forged destination plus injected memo), and prints decisions,
Stripe ids, the confirmation URL if Stripe holds the payment (with the `atl stripe confirm` command to run
after approving), and receipt verification. With `--transport rest` it uses the REST fallback and needs
`STRIPE_SECRET_KEY` and a `stripe_account_id` in the mandate.
