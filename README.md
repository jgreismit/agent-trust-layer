# Agent Trust Layer (ATL)

A payment guard for AI agents. An agent never holds payment credentials: it submits each payment to ATL, which
checks it against a mandate the owner signed (registered payees and Pix keys, categories, per-payment, daily
and hard caps, duplicates, split orders, instruction-like memo text) and answers **approve**, **escalate** or
**refuse**. ATL executes approved payments itself and returns an Ed25519-signed, hash-chained receipt.

MIT AI Studio (MAS.665), Fall 2026. Team: Geraldo Rodrigues, Marlon Pascoal. This repository holds the ATL
service only; the course homework (benchmarks, agents, demos) lives in the team's project repository.

## Interfaces

- **MCP server** (stdio): `python -m atl.mcp_server`, with tools `register_mandate`, `pay_check`, `get_decision`,
  `execute_payment`, `get_receipt`, `verify_receipt`.
- **Owner CLI**: `atl mandate register FILE` (sign with the owner key and register), `atl approve ID`,
  `atl refuse ID`, `atl revoke ID`, `atl rail-balance`, `atl reset`. (`atl demo` needs the full project.)
- **Rules**: `atl/policy` (R1 to R11, deterministic, no model calls). Rails: `atl/rails` (simulated rail, the
  Asaas Pix sandbox, and the Stripe sandbox through the Stripe MCP).

## Run

```bash
uv sync
export ATL_RAIL=simulated ATL_VAR_DIR=/tmp/atl   # simulated rail: no payment credentials needed
uv run atl mandate register my-mandate.json
uv run python -m atl.mcp_server                   # point an MCP client (an agent) at this
uv run pytest -q tests                            # offline tests
```

The Asaas sandbox rail needs `ASAAS_API_KEY` in a local `.env` (see `.env.example`); production is blocked
unless explicit promotion preconditions hold.

## US rail: Stripe via the Stripe MCP

Stripe plays for US payees the role Asaas plays for Pix in Brazil, and ATL reaches Stripe through the
**Stripe MCP server** (`https://mcp.stripe.com`). ATL is the only MCP client of Stripe: the paying agent
talks to ATL's MCP tools and never receives the Stripe key or the Stripe MCP. ATL calls Stripe's `send_money`
only after its rules (R1 to R11) and the owner's mandate approve the payment, then issues its usual signed
receipt.

```
agent -> ATL MCP (pay_check, execute_payment...) -> rules R1..R11 + owner mandate
      -> ATL as MCP client -> Stripe MCP (https://mcp.stripe.com) -> Stripe sandbox
```

- Select it with `ATL_RAIL=stripe_sandbox` (sandbox only) and an agent API key in `STRIPE_AGENT_KEY`. Live
  keys are refused before any connection. `ATL_STRIPE_TRANSPORT=rest` switches to a small REST fallback
  (Connect transfers, `STRIPE_SECRET_KEY` test key) for sandboxes without Treasury.
- US mandates carry `"currency": "USD"` and payees carry a `stripe_recipient_id` instead of a Pix key (see
  `examples/us-mandate.json`). The agent passes the invoice's `destination` and `currency` to `pay_check`; a
  forged destination is refused by R3, a wrong currency by R11.
- Owner commands: `atl stripe setup-recipient`, `atl stripe pending`, `atl stripe confirm ID`,
  `atl stripe abandon ID`. When Stripe asks for human confirmation, the payment waits as
  `pending_rail_confirmation` until the owner approves it in the Stripe Dashboard and runs `atl stripe confirm`.
- First run with a key: `uv run python scripts/stripe_sandbox_smoke.py --list-tools`, then without flags.

Details, setup and limits: `docs/stripe-rail.md`. Implementation notes: `atl/rails/STRIPE_NOTES.md`.

## Used by

- Nanda Town integration (`payments: atl.v1`): `integrations/atl-payments` in `projnanda/nandatown`, which
  runs this service as the town's payments guard. Tag `hw8` of this repository is the version it was tested
  with.
