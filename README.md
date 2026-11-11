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
- **Rules**: `atl/policy` (R1 to R10, deterministic, no model calls). Rails: `atl/rails` (simulated rail, and
  the Asaas Pix sandbox).

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

## Used by

- Nanda Town integration (`payments: atl.v1`): `integrations/atl-payments` in `projnanda/nandatown`, which
  runs this service as the town's payments guard. Tag `hw8` of this repository is the version it was tested
  with.
