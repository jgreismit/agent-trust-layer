# Asaas sandbox notes

## Wave 0 spike (L0, 2026-10-28, script `scripts/spike/spike_pix.py`, log `runs/spike-*.jsonl`)

Result: **sandbox transfers work for the debit side. No fallback to the simulated rail.**

| Step | Call | Observed |
|---|---|---|
| Account status | `GET /v3/myAccount/status` | 200; commercialInfo, bankAccountInfo, documentation, general all `APPROVED` |
| Balance | `GET /v3/finance/balance` | 200 `{"balance": 0}` (number, BRL with decimals) |
| Customer | `POST /v3/customers` `{name, cpfCnpj, externalReference}` | 200, id `cus_...`. A checksum-valid fictitious CPF is accepted. Lookup by `GET /v3/customers?externalReference=atl-funding` |
| PIX charge | `POST /v3/payments` `{customer, billingType: "PIX", value: 210.0, dueDate: today, externalReference}` | 200, status `PENDING`, `netValue` 209.01 (**charge fee R$ 0.99**) |
| Sandbox confirm | `POST /v3/sandbox/payment/{id}/confirm` with `{}` | 200, status `RECEIVED`; balance became 209.01 **immediately** (first poll) |
| Pix transfer | `POST /v3/transfers` `{value: 1.00, operationType: "PIX", pixAddressKey: "cliente-a00001@pix.bcb.gov.br", pixAddressKeyType: "EMAIL", description, externalReference}` | 200, id is a UUID, `status: PENDING`, **`authorized: false`**, `transferFee: 0`, `canBeCancelled: true`, `bankAccount.bank.name: "Banco Virtual - BACEN"`, `bankAccount.pixAddressKey` echoes the key |
| Debit | `GET /v3/finance/balance` | 208.01: **debited exactly R$ 1.00 at creation**, no fee |
| Transfer status | `GET /v3/transfers/{id}` polled 60 s, rechecked minutes later | stays `PENDING`, `authorized: false` |
| List | `GET /v3/transfers?limit=5` | returns the transfer, `totalCount` |

Units: Asaas uses decimal BRL (`value: 1.0`). ATL converts at the rail boundary only: `cents = round(value * 100)`, `value = cents / 100`.

### Fictitious destination keys (docs: "Testando transferências")

`cliente-a00001@pix.bcb.gov.br` to `cliente-a00005@pix.bcb.gov.br`, type `EMAIL`. The transfer is simulated as completed at the BCB side, the origin is debited, nobody is credited. Demo payees use:
- `concreto-forte`: `cliente-a00001@pix.bcb.gov.br`
- `ferragens-silva`: `cliente-a00002@pix.bcb.gov.br`
- D2 injected "new key": `cliente-a00003@pix.bcb.gov.br` (never paid, refused by R3)

### Critical-action token

- Docs ("Como testar ações críticas"): sandbox token is `000000`, but the doc does not name a request field for transfers and says the flow "depends on the endpoint". The transfer reference (`POST /v3/transfers`) has **no token field**. `authorized: false` means "awaiting authorization by token" (reference page).
- The two documented ways to avoid the pending state are (a) asking Asaas support to disable the token in sandbox and (b) enabling the withdrawal validation webhook in *Integrações > Mecanismos de segurança*. **Both are account settings changes, forbidden by hard rule 4.** Not done.
- Consequence: API-created transfers stay `PENDING` / `authorized: false` until the owner authorizes them in the sandbox web UI (token `000000`). The debit and the transfer record are real and immediate.
- What the rail does: it records `authorized` and raw `status` in `RailTransfer.status` (for example `PENDING`), logs `sandbox_critical_action_pending` with the transfer id, and does not fail. In production it must never use `000000`.
- Verifier rule: a transfer is accepted when it exists, amount and key match, and status is one of `PENDING`, `BANK_PROCESSING`, `DONE`. `FAILED` and `CANCELLED` fail the check.
- Limitation for the demo text: "Asaas holds sandbox transfers created by API for owner token authorization, so transfers show as PENDING in the sandbox; the debit is immediate and verified."

### Minimums and fees (S-03)

- Pix transfer of R$ 1.00 accepted; `transferFee: 0` (sandbox PF account). The demo values R$ 12.00, R$ 9.00 and R$ 48.00 are valid; no scaling needed.
- PIX charge fee: R$ 0.99 per charge (210.00 gross, 209.01 net). Funding R$ 200.00 needs a charge of about R$ 201.00 or more.
- No minimum observed for charges or transfers at these values; the docs state none.

### Still to verify (L1)

- `externalReference` uniqueness: Asaas does not document deduplication on it. ATL idempotency must be enforced in ATL (one transfer per decision, stored before returning), and the rail can check `GET /v3/transfers?externalReference=...` before creating if supported.
- Error body shape for 400s (`{"errors": [{"code", "description"}]}` per docs).

Both answered in "L1 findings" below.

## L1 findings (2026-10-28, logs `runs/contract-tests.jsonl`, `runs/setup-*.jsonl` in wt/L1)

Docs pages checked:
- Create Pix transfer `POST /v3/transfers`: https://docs.asaas.com/reference/transferir-para-conta-de-outra-instituicao-ou-chave-pix (body: value, operationType, pixAddressKey, pixAddressKeyType, description, scheduleDate, externalReference, recurring; **no token field**)
- List transfers `GET /v3/transfers`: https://docs.asaas.com/reference/listar-transferencias (documented filters only `dateCreated[ge|le]`, `transferDate[ge|le]`, `type`)
- Get one transfer `GET /v3/transfers/{id}`: https://docs.asaas.com/reference/recuperar-uma-unica-transferencia
- Balance, account status, customers, payments and sandbox confirm: paths as in the spike table above, all confirmed live by L1 (200).

externalReference lookup (contract test `test_lookup_by_external_reference`):
- `GET /v3/transfers?externalReference=atl-no-such-ref` returns the spike transfer anyway (`totalCount: 1`). **The server ignores the externalReference filter.** The response items do carry `externalReference`.
- So `AsaasRail.find_transfer_by_external_reference` sends `dateCreated[ge]=<yesterday UTC>` (covers the Brazil local date), pages with `limit=100`/`offset` (max 5 pages) and matches `externalReference` client side. Verified live: finds the spike transfer by its ref, returns None for an unknown ref. No real transfer was needed to confirm this (0 of 2 budget used).
- `POST /transfers` is never blindly retried: on 5xx or timeout the rail runs this lookup and re-POSTs only if nothing matches (at most 2 POSTs). Asaas does not dedupe on externalReference, so ATL must still enforce one transfer per decision in the store.

Error bodies:
- 400 validation: `{"errors": [{"code": "invalid_name", "description": "O parâmetro name deve ser informado"}]}` (live, `POST /customers {}`; nothing is created). RailError(RAIL_ERROR) message carries `code: description` pairs.
- 404 on `GET /v3/transfers/{unknown uuid}`: **empty body**. Mapped to RailError(TRANSFER_NOT_FOUND) on status alone.

Other:
- `GET /transfers/{id}` returns `dateCreated` as a date only (`2026-10-28`, Brazil local). `get_transfer` maps it to midnight UTC of that date; `transfer_pix` uses the request time in UTC.
- Spike transfer `4e8f9314-...` still `PENDING`, `authorized: false` hours later; balance R$ 208.01.
- `sandbox_setup.py` ran once in default mode: balance R$ 208.01 already above R$ 200.00, so no funding charge was created; it wrote `var/payees.json`. The funding path (customer reuse, charge of `(20000 - balance) + 200` cents, min 500, sandbox confirm, balance poll 2 min) is covered offline by `tests/rails/test_sandbox_setup.py`.
- Spec vs brief: payment-execution spec says fund when balance is below 10000 cents; the L1 brief says below 20000. The script funds below 20000, which also satisfies the spec scenario (target is at least 20000 either way).
- 409 `checkout.already.requested` on `POST /transfers` when an identical transfer (same amount and key) is still PENDING. `sandbox_setup.py` (default mode, sandbox only) now cancels PENDING transfers whose externalReference starts with `dec_` via `DELETE /v3/transfers/{id}/cancel` (https://docs.asaas.com/reference/cancelar-uma-transferência). Live run 2026-10-28: 2 cancelled (R$ 12.00 and R$ 48.00), status CANCELLED, refund visible on the first balance read (R$ 201.01 to R$ 261.01). The spike transfer is never touched.
- Spec says "the rail sends 000000" when a token is requested. The transfer API has no token field (see Critical-action token above), so the rail logs `sandbox_critical_action_pending` instead and never sends a token. In production it logs `critical_action_pending` and never uses 000000.

## Owner authorization observed live (L0, 2026-10-28, run_01M61BA0DMJ4Z4ZX6DXQPFXC7K)

- After creation: `status: PENDING`, `authorized: false`.
- After Marlon typed `000000` in the sandbox web UI: `status: PENDING`, `authorized: true`, `effectiveDate: null` (13:36 UTC).
- About a minute later: `status: DONE`. The verifier passed with both transfers DONE.
- So "authorized" cannot be read from the status alone: `AsaasRail.transfer_authorized` reads the `authorized` flag, and `wait_for_authorization` reports `PENDING (authorized)` for that state.
- DONE transfers are never touched by `sandbox_setup.py` (it cancels only PENDING `dec_` transfers), so an authorized run keeps its Asaas evidence.
