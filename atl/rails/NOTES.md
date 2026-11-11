# L1 notes

## Plan

1. `atl/rails/asaas.py`: AsaasRail on httpx with the sandbox guard before every request, GET retries on 5xx, POST /transfers lookup by externalReference before any retry, one `rail_call` runlog line per request.
2. `atl/rails/simulated.py`: SimulatedRail with JSON state in var/sim_rail.json, atomic rewrite under a file lock, balance 20000 cents at start.
3. `atl/rails/__init__.py`: RailError stays, `get_rail(settings)` picks simulated or Asaas.
4. `scripts/sandbox_setup.py`: `--check` read only; default mode funds to 20000 cents via customer, PIX charge and sandbox confirm, then writes var/payees.json.
5. Tests: offline respx tests in tests/rails, real sandbox tests in tests/contract (marked sandbox), then the "L1 findings" section in ASAAS_NOTES.md.
