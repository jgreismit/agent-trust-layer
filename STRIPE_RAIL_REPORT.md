# Stripe rail report (branch `stripe-rail`, 2026-11-12)

Goal: do for the US with Stripe what Asaas does for Brazil, with the **Stripe MCP server as the way ATL talks
to Stripe**, and keep ATL's core promise: the paying agent never gets Stripe credentials or the Stripe MCP.
ATL alone is the MCP client of Stripe and calls it only after its rules approve the payment.

```
agent -> ATL MCP (pay_check, execute_payment...) -> rules R1..R11 + owner mandate
      -> ATL as MCP client -> Stripe MCP (https://mcp.stripe.com) -> Stripe sandbox
```

**Bottom line:** implemented and tested offline (231 tests pass, up from 155). No call has reached Stripe,
because no sandbox key exists. The Stripe MCP tool shapes are therefore assumptions, kept in one module
(`atl/rails/stripe_shapes.py`). The first real run of `scripts/stripe_sandbox_smoke.py --list-tools` shows
the real shapes.

## What changed and why

| Commit | Change | Why |
|---|---|---|
| `98a5760` | Contracts: `Mandate.currency` (BRL default, USD), payees with a Pix key **or** a Stripe destination (`stripe_recipient_id`, optional `stripe_account_id`), `PaymentIntent.destination` and `.currency`, `RailTransfer.destination`, `.currency`, `.confirmation_url`, status `pending_rail_confirmation`, `DestinationRail` protocol, rule `R11_CURRENCY_MISMATCH`. Policy: R3 now checks the exact registered destination, R11 refuses a currency that differs from the mandate, money and the daily window follow the currency, held payments count toward caps. R4 also spots Stripe ids and routing numbers next to a payment verb | Payees and currency; "exact Pix key" becomes "exact registered destination" |
| `df03685` | `StripeMCPRail`, `MCPStripeTransport` (official MCP SDK, streamable HTTP), `StripeTransport` interface, `stripe_shapes.py`, REST fallback `stripe_rest.py`, config (`ATL_RAIL=stripe_sandbox`, `ATL_STRIPE_TRANSPORT`, `STRIPE_AGENT_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_FINANCIAL_ACCOUNT`), sandbox and key guards, Stripe key redaction in run logs | The rail itself |
| `b67f295` | Service: optional `destination` / `currency` in `pay_check`, R11 when the mandate currency is not the rail's, `execute_payment` pays Stripe payees through `pay_destination`, the pending confirmation state, `confirm_rail_payment`, `abandon_rail_payment`, `setup_stripe_recipient`. MCP server `pay_check` takes `destination` and `currency` (`pix_key` now optional). CLI `atl stripe setup-recipient / pending / confirm / abandon`; `atl rail balance` prints USD | Wiring and the owner side of human confirmation |
| `08a593f` | 76 offline tests (fake Stripe MCP transport, `httpx.MockTransport`). They found two bugs, fixed here: `ATL_RAIL=stripe_sandbox` was not selected by `load_settings`, and JSON returned as a text block lost its status and amount | Tests |
| `3b77bc9` | `examples/us-mandate.json`, `scripts/stripe_sandbox_smoke.py`, tests that the script prints usage without a key and refuses a live key | First run with a key |
| `ed03c03` | README section, `docs/stripe-rail.md`, `atl/rails/STRIPE_NOTES.md` | Docs |

Pix behavior is unchanged: every existing test passes untouched, and a Pix mandate, intent or transfer
serializes byte for byte as before (new optional fields are left out while unset, and `currency` while it is
`BRL`), so existing owner signatures and receipt hashes still verify. A test covers this. The Asaas
production guard and rules R1 to R10 were not weakened. The only rule changes add checks: R11, R4 patterns,
and held payments counting toward caps.

## Asaas to Stripe mapping

| Concern | Asaas (Brazil, Pix) | Stripe (US, USD) |
|---|---|---|
| Rail | `atl/rails/asaas.py` `AsaasRail` | `atl/rails/stripe_mcp.py` `StripeMCPRail` (name `stripe_sandbox`) |
| Selection | default rail | `ATL_RAIL=stripe_sandbox`; `ATL_STRIPE_TRANSPORT=mcp` (default) or `rest` |
| Protocol | REST over httpx | MCP streamable HTTP with the official Python SDK; REST fallback over httpx |
| Credential | `ASAAS_API_KEY`, `access_token` header | `STRIPE_AGENT_KEY` (agent API key) as Bearer; `STRIPE_SECRET_KEY` (test) for REST |
| Sandbox guard | host must be `api-sandbox.asaas.com` | `ATL_ENV=sandbox`; host `mcp.stripe.com` / `api.stripe.com`; key must contain `_test_` and never `_live_`; `PRODUCTION_BLOCKED` before any I/O |
| Production | guarded human promotion | none (refused) |
| Destination | Pix key and key type | Treasury recipient `acct_...` (MCP) or Connect `acct_...` (REST) |
| Payee setup | owner writes the Pix key in the mandate | `atl stripe setup-recipient` calls `setup_recipient`, stores the id, writes it into the mandate file |
| Currency, units | BRL, integer cents (decimal at the boundary) | USD, integer cents (minor units at the boundary) |
| Pay | `POST /v3/transfers` | `send_money` tool (REST: `POST /v1/transfers`) |
| Idempotency | `externalReference`, lookup before any retry, never a blind re-POST | local state by decision id, metadata `atl_idempotency_key`, uncertain attempts never resent; REST `Idempotency-Key` plus `transfer_group` |
| Owner second factor | critical-action token; transfer `PENDING`, `authorized: false` | Stripe human confirmation URL; decision `pending_rail_confirmation`; `atl stripe confirm` |
| Status | `GET /v3/transfers/{id}` | `stripe_api_read` GET `/v2/money_management/outbound_payments/{id}` (REST `GET /v1/transfers/{id}`) |
| Balance | `GET /v3/finance/balance` | `stripe_api_read` GET `/v1/balance`, or the financial account when `STRIPE_FINANCIAL_ACCOUNT` is set |
| Duplicate at the rail | 409 maps to `RAIL_DUPLICATE_TRANSFER` | REST 409 (idempotency) maps to `RAIL_DUPLICATE_TRANSFER` |
| Errors | `RailError` `RAIL_ERROR` / `TRANSFER_NOT_FOUND` | same codes; timeouts are `RailOutcomeUnknown` (code `RAIL_ERROR`) |
| Notes | `ASAAS_NOTES.md` | `STRIPE_NOTES.md` |
| Live contract tests | `tests/contract` (marked sandbox) | none yet |

## Design decisions

1. **One currency per mandate**, on the mandate (`currency`), not per payee. A USD mandate may hold only
   Stripe payees and a BRL mandate only Pix payees (validated). The intent may state a currency; a mismatch is
   refused by R11 with a reason, not rejected as invalid input, so it gets a receipt.
2. **Rail currency check in the service, not the policy.** The policy stays pure and rail-agnostic;
   `pay_check` adds R11 when the mandate's currency is not the configured rail's (from settings, so no rail is
   built during `pay_check`). `execute_payment` checks again against the actual rail.
3. **Backward-compatible serialization.** New fields are dropped from the canonical form while unset, so
   nothing signed or hashed before this branch changes.
4. **Registered destinations.** A Stripe payee's registered destinations are its recipient id and its
   connected account id (both owner-signed). R3 accepts either, exact string; ATL pays the one the active
   transport uses.
5. **The agent never sees the Stripe confirmation URL.** `execute_payment` returns `pending_rail_confirmation`
   and `wait_for_owner`; the URL goes to the owner on stderr, in the run log and in `atl stripe pending`.
   The agent cannot approve in Stripe anyway, but the link is an owner action and stays on the owner side.
6. **No auto-approval, ever.** `atl stripe confirm` only retries the identical `send_money` call (plus a token
   the owner pastes). `abandon` gives up a held payment and blocks any resend.
7. **Idempotency owned by ATL.** The state file is written with status `uncertain` before `send_money`, so
   even a crash mid-call is never resent. Definite refusals clear the record (nothing moved); timeouts and
   unreadable answers trigger a lookup and otherwise block resending.
8. **Schema fitting.** The rail reads the live tool schemas once and drops arguments they do not declare,
   logging the dropped names. A wrong guess for an optional field (description, metadata, idempotency key)
   cannot fail a payment.
9. **One MCP session per call in a worker thread.** ATLService is synchronous and can run inside the ATL MCP
   server's event loop; a fresh loop in a thread avoids nested-loop errors. Simple over fast; fine for
   per-payment calls.
10. **Strict key check.** Besides refusing `_live_`, a key without `_test_` is refused too, since an unknown
    format could be live. If Stripe's agent keys turn out to use another test marker, relax
    `assert_stripe_test_key` in `atl/config.py` (one place).
11. **Day boundary for USD** is the New York day (Sao Paulo stays for BRL).
12. **Keys from the environment** (or the local `.env`, like Asaas); the smoke script accepts the
    environment only.
13. **No new dependency.** `mcp>=1.2` was already in `pyproject.toml` (2.3.0 installed), so `uv add mcp`
    changed nothing. The transport supports both the 2.x and the 1.x client API.

## Every assumption about Stripe MCP tool shapes

All in `atl/rails/stripe_shapes.py` (tags A1 to A14) unless noted. None has been observed live.

| # | Assumption | If wrong |
|---|---|---|
| A1 | Tool names `setup_recipient`, `send_money`, `stripe_api_read` (from the docs table) | `--list-tools` reports them missing |
| A2 | `setup_recipient` takes `name`, `email`, `country`, `entity_type` | tool error at setup; fix `setup_recipient_args` |
| A3 | `setup_recipient` returns a recipient id `acct_...` (structured field `recipient_id` / `recipient` / `account_id` / `account` / `id`, or the first `acct_` in text), and maybe an onboarding URL | "no recipient id" error; fix `parse_recipient` |
| A4 | `send_money` takes `recipient`, `amount`, `currency` (lowercase), `description`, `metadata`, `idempotency_key`, optional `financial_account` | required ones: tool error; optional ones: dropped by schema fitting |
| A5 | `send_money.amount` is integer cents | **money risk**: if it is decimal dollars, 1500 would mean $1500. Check first; the per-payment cap still bounds it, and Stripe human confirmation shows the amount before anything moves |
| A6 | `send_money` accepts `description` and `metadata` | dropped; recovery lookup then matches only by description, or not at all |
| A7 | `send_money` accepts `idempotency_key` | dropped; ATL's own state still prevents a second send |
| A8 | Result amounts are `{value, currency}` (v2) or integers (v1) | amount not parsed; ATL keeps its own amount |
| A9 | Outbound payment statuses `processing`, `posted`, `failed`, `returned`, `canceled` | status shown raw anyway |
| A10 | A held payment is a Dashboard URL in a structured field (`confirmation_url`, `approval_url`, `confirm_url`, `review_url`, `url`) or a `stripe.com` URL in text that mentions confirming or approving, or `approval_required` in the answer | a held answer could read as "unknown outcome": ATL then refuses to resend (safe, but the owner must check the Dashboard) |
| A11 | The retry after approval takes `approval_token` (and any `approval_request` / `approval_request_id` / `approval_id` Stripe returned) | dropped by schema fitting; the retry may be held again |
| A12 | `stripe_api_read` takes `{"method": "GET", "path": ..., "params": {...}}` | status, balance and recovery reads fail with `RAIL_ERROR`; payments still work |
| A13 | REST: v1 transfers have no status; ATL reports `paid` or `reversed` | cosmetic |
| A14 | Balance: v1 `available[]` with `amount` / `currency`; financial account `balance.available.usd.value` | `rail balance` fails |
| A15 | A successful `send_money` names an `obp_...` id (structured `id` / `outbound_payment` / `outbound_payment_id`, or in text) | treated as unknown outcome: no resend, owner checks |
| A16 | The SDK surfaces a 401 as an HTTP status error with `response.status_code == 401`, or a message containing 401 / Unauthorized (`map_transport_error` in `stripe_mcp.py`) | reported as a generic MCP connection error |
| A17 | Sandbox agent keys contain `_test_` (agent keys are restricted keys, `rk_test_`) (`atl/config.py`) | ATL refuses the key; relax the check |
| A18 | `send_money` pays from the account's default Treasury source when no financial account is given | tool error asking for one; set `STRIPE_FINANCIAL_ACCOUNT` |

## Tests

Baseline on `main`: `155 passed, 6 deselected`.

Final on `stripe-rail` (`uv run pytest -q tests`): **`231 passed, 6 deselected`** (6 deselected are the
existing Asaas sandbox contract tests, skipped by default). `uv run ruff check atl tests scripts`: clean.

New test files: `tests/rails/test_stripe_mcp_rail.py` (31), `tests/rails/test_stripe_rest.py` (9),
`tests/rails/test_stripe_shapes.py` (8), `tests/rails/test_stripe_smoke_script.py` (5),
`tests/service/test_stripe_service.py` (23), fakes in `tests/rails/stripe_fakes.py`. Covered:

- approved USD payment calls `send_money` exactly once with the right recipient, amount (cents), currency,
  idempotency key, decision id and intent id; the receipt verifies and the chain is intact;
- forged destination (with injected memo) and unregistered payee refused before any Stripe call (the rail is
  not even built);
- live agent and secret keys, keys without `_test_`, `ATL_ENV=production` and non-Stripe hosts refused with
  `PRODUCTION_BLOCKED` before any I/O, the key never in messages or logs;
- Stripe human confirmation: pending, no URL to the agent, URL to the owner, `execute_payment` again makes
  no call, owner confirm with a token completes it, receipts go approved, pending, executed; still-pending
  confirm; abandon blocks resends;
- tool error, timeout, 401 OAuth challenge and MCP protocol errors become `RailError` with existing codes;
  exception groups unwrapped;
- same idempotency key never pays twice (same rail object, fresh rail object, and R5 at the policy level);
  timeout never resent unless a lookup finds the payment;
- REST fallback: form body, `Idempotency-Key`, Bearer key, 5xx retried with the same key, 400, 409, 401,
  timeout then lookup by `transfer_group`, status and balance;
- currency: EUR or BRL intent on a USD mandate, USD mandate on the Pix rail, BRL mandate on the Stripe rail;
- Pix canonical bytes unchanged; payee destination validation; config and CLI guards; the smoke script.

## What was not done

- **No call to Stripe or mcp.stripe.com** (no key; forbidden by the brief). Every shape above is unverified.
- `MCPStripeTransport` (the real SDK client) is covered only for its guards and error mapping, not against a
  running MCP server; its session code runs for the first time on the first real run.
- No live contract tests for Stripe (the equivalent of `tests/contract/test_asaas_sandbox.py`).
- No OAuth flow and no `Stripe-Account` header (acting as a connected account).
- No reconcile command for an uncertain payment; the owner checks the Dashboard. `atl stripe abandon` covers
  held payments only.
- No automatic expiry of held payments after Stripe's 24 hours.
- `atl demo` and the Nanda Town integration were not extended or retested with the Stripe rail.
- No production mode for Stripe, deliberately.
- I did not read `.env.example` (the brief forbids `.env.*`), so it does not list the new variables; they are
  documented in `docs/stripe-rail.md`.

## First steps once a sandbox key exists

1. Create a Stripe sandbox, enable Treasury / money management (preview) and fund a financial account. Create
   an agent API key scoped to Treasury recipients, send money and reads (see `docs/stripe-rail.md`).
2. `export STRIPE_AGENT_KEY=...` and run `uv run python scripts/stripe_sandbox_smoke.py --list-tools`.
3. Compare the schemas of `setup_recipient`, `send_money` and `stripe_api_read` with A1 to A18. **Check A5
   (amount units) first.** Fix `atl/rails/stripe_shapes.py`, update `SCHEMAS` and the canned results in
   `tests/rails/stripe_fakes.py`, and run `uv run pytest -q tests`.
4. Run the smoke script without flags and with a small amount. Expect Stripe to hold `send_money` for human
   confirmation: approve it in the Dashboard, then run the printed `atl stripe confirm` command.
5. Record what you saw (tool list, first `send_money` answer, the confirmation flow, the approval token) in
   the "Findings" section of `atl/rails/STRIPE_NOTES.md`, and turn the run into tests marked `sandbox`.
6. If Treasury is not available on the sandbox, use the REST fallback: create a test connected account, add
   its id as the payee's `stripe_account_id`, and run the smoke script with `--transport rest` and
   `STRIPE_SECRET_KEY`.
