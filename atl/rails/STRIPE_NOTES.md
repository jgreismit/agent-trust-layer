# Stripe sandbox notes

Companion to `ASAAS_NOTES.md` for the US rail. User-facing setup and flow: `docs/stripe-rail.md`.

## Status (2026-11-12, branch stripe-rail)

- Implemented offline. **No call has reached Stripe**: no sandbox key existed. Nothing below marked "assumed"
  has been observed live.
- Docs read: https://docs.stripe.com/mcp (server, auth, tools, supported API methods, human confirmation),
  https://docs.stripe.com/keys (agent keys, test and live prefixes),
  https://docs.stripe.com/api/v2/money-management/outbound-payments/create (request, response, error codes).

## What the docs say (confirmed by reading, not by calling)

| Topic | Docs |
|---|---|
| Server | `https://mcp.stripe.com`, streamable HTTP |
| Auth | OAuth, or an agent API key as `Authorization: Bearer ...`. From December 2026 only agent keys or OAuth; an unsupported key gets `401` with an OAuth discovery challenge |
| Keys | Sandbox keys `sk_test_`, `rk_test_`, `pk_test_`; live `*_live_`. Agent keys are restricted keys with an Agent tag, subject to approval rules |
| Treasury tools | `get_balance_summary`, `setup_recipient`, `send_money` (human confirmation), `transfer_money` (human confirmation), `convert_currency` (human confirmation); public preview |
| API tools | `stripe_api_search`, `stripe_api_details`, `stripe_api_read` (GET), `stripe_api_write` (listed methods only) |
| Not supported | Creating Connect transfers through `stripe_api_write`; reading transfers, payouts, v2 outbound payments is supported |
| Human confirmation | Stripe returns a URL; the owner approves in the Dashboard; Stripe gives the agent an approval token and the agent must retry; expires after 24 hours |
| Outbound payment (v2) | `obp_...`; `amount: {value, currency}` in minor units; recipient is a v2 account `acct_...`; status starts `processing`; error `403 approval_required` when approval is needed; `409 idempotency_error` |

## Assumptions to check on the first run (all in `stripe_shapes.py`)

- A1 tool names `setup_recipient`, `send_money`, `stripe_api_read`.
- A2 `setup_recipient` arguments `name`, `email`, `country`, `entity_type`.
- A3 `setup_recipient` returns a recipient id `acct_...` (structured or in text) and maybe an onboarding URL.
- A4 `send_money` arguments `recipient`, `amount`, `currency`, `description`, `metadata`, `idempotency_key`,
  optional `financial_account`.
- A5 `send_money.amount` is integer minor units (cents), not decimal dollars.
- A6 `send_money` accepts `description` and `metadata` (ATL's ids go there).
- A7 `send_money` accepts an `idempotency_key`. ATL does not rely on Stripe honoring it.
- A8 amounts in results are `{value, currency}` (v2) or integers (v1).
- A9 outbound payment statuses `processing`, `posted`, `failed`, `returned`, `canceled`.
- A10 a held payment is recognized by a Dashboard URL (structured `confirmation_url`, `approval_url`, `url`, or
  a `stripe.com` URL in text that speaks of confirming or approving), or by `approval_required` in the answer.
- A11 the retry after approval passes `approval_token` (and any `approval_request_id` Stripe returned).
- A12 `stripe_api_read` arguments `{"method": "GET", "path": ..., "params": {...}}`.
- A13 REST: v1 transfers have no status; ATL reports `paid` or `reversed`.
- A14 balance: v1 `available[]`, or v2 financial account `balance.available.usd.value`.

## Design notes

- Local state `var/stripe_rail.json` (thread lock plus fcntl lock, atomic rewrite, as in the simulated rail):
  `payments` keyed by ATL idempotency key (= decision id) with status `uncertain` (written before the call),
  `sent`, `pending_confirmation` or `abandoned`; `recipients` keyed by payee id.
- The rail fetches tool schemas once (`tools/list`) and drops undeclared arguments (`dropped_args` in the run
  log). If listing fails, it sends the arguments as built.
- One MCP session per call, in a worker thread with its own event loop, because ATLService is synchronous and
  may run inside the ATL MCP server's loop.
- Works with the installed `mcp` 2.3 (`streamable_http_client` plus `httpx2`) and falls back to the 1.x
  `streamablehttp_client(url, headers=...)`. `mcp>=1.2` was already a dependency; no new package.
- `RailOutcomeUnknown` (a `RailError` with code `RAIL_ERROR`) marks timeouts and unreadable success answers;
  only those make an attempt uncertain.

## Findings from the first live run

(Empty. Record the `--list-tools` output, the first `send_money` answer and the confirmation flow here.)
