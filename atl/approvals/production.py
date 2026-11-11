"""`atl demo --production-single` (specs/production-promotion).

One intent of 100 cents to the single allowlisted key, through pay_check, the PAY prompt and
execute_payment, then a read-back of the transfer and a receipt verification.

ATL_ENV=production: reached only after atl.config.production_guard passed inside load_settings
(interactive terminal, ATL_PROMOTION=1, green acceptance report, .env.production present).
ATL_ENV=sandbox: dry run on the settings rail with the allowlisted key below and the same guards on.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from atl.approvals import format_brl, mask_key
from atl.config import Settings
from atl.contracts import PRODUCTION_BLOCKED, ATLError
from atl.ids import new_id
from atl.rails.authorization import AWAITING_STATUS, owner_instruction, wait_for_authorization
from atl.service import ATLService

PROD_SINGLE_AMOUNT_CENTS = 100
PROD_AUTH_WAIT_S = 300.0  # production only: time for the owner to confirm the transfer in the Asaas app
SANDBOX_DRY_RUN_PIX_KEY = "cliente-a00004@pix.bcb.gov.br"
SANDBOX_DRY_RUN_PIX_KEY_TYPE = "EMAIL"
PROD_SINGLE_AGENT_ID = "owner-production-single"
PROD_SINGLE_PAYEE_ID = "owner-own-account"


def _allowed_key(settings: Settings) -> tuple[str, str]:
    if settings.env == "production":
        if not settings.prod_allowed_pix_key or not settings.prod_allowed_pix_key_type:
            raise ATLError(PRODUCTION_BLOCKED, "ATL_PROD_ALLOWED_PIX_KEY and its type must be set")
        return settings.prod_allowed_pix_key, settings.prod_allowed_pix_key_type
    return SANDBOX_DRY_RUN_PIX_KEY, SANDBOX_DRY_RUN_PIX_KEY_TYPE


def run_production_single(
    settings: Settings,
    *,
    service: ATLService | None = None,
    input_fn: Callable[[], str] | None = None,
    output_fn: Callable[[str], None] | None = None,
) -> int:
    """Return a process exit code: 0 on a verified payment, 1 otherwise. ATLError propagates."""
    key, key_type = _allowed_key(settings)
    svc = service or ATLService.from_settings(settings)
    if input_fn is not None:
        svc.input_fn = input_fn
    if output_fn is not None:
        svc.output_fn = output_fn
    out = svc.output_fn
    svc.production_guards = True
    svc.require_confirmation = True
    svc.prod_allowed_pix_key = key

    mode = "PRODUCTION" if settings.env == "production" else "DRY RUN (sandbox, production guards on)"
    out(f"atl production-single: {mode}, {format_brl(PROD_SINGLE_AMOUNT_CENTS)} to {mask_key(key)}")

    now = svc.now_fn()
    mandate = svc.sign_mandate(
        {
            "mandate_id": new_id("mdt"),
            "owner_id": "owner-marlon",
            "agent_id": PROD_SINGLE_AGENT_ID,
            "payees": [
                {
                    "payee_id": PROD_SINGLE_PAYEE_ID,
                    "display_name": "Marlon (own account)",
                    "pix_key": key,
                    "pix_key_type": key_type,
                    "categories": ["promotion"],
                }
            ],
            "per_payment_cap_cents": 500,
            "daily_cap_cents": 1000,
            "hard_cap_cents": 500,
            "valid_until": (now + timedelta(hours=1)).isoformat(),
        }
    )
    mandate_id = svc.register_mandate(mandate)["mandate_id"]
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    check = svc.pay_check(
        mandate_id=mandate_id,
        agent_id=PROD_SINGLE_AGENT_ID,
        payee_id=PROD_SINGLE_PAYEE_ID,
        pix_key=key,
        amount_cents=PROD_SINGLE_AMOUNT_CENTS,
        category="promotion",
        invoice_ref=f"PROD-SINGLE-{stamp}",
        memo="production promotion single payment",
        idempotency_key=new_id("prod"),
    )
    out(f"pay_check: {check['verdict']} {check['status']} rules={check['rule_hits'] or 'none'}")
    if check["verdict"] != "approve":
        out("FAIL: decision is not approve; nothing was paid")
        return 1

    executed = svc.execute_payment(check["decision_id"])
    out(f"executed: transfer {executed['transfer_id']} on rail {executed['rail']}")
    if executed.get("rail_status") == AWAITING_STATUS and executed["rail"] != "simulated":
        out(f"OWNER ACTION: Asaas holds this transfer for your token. {owner_instruction(executed['rail'])}")
        if settings.env == "production":
            out(f"waiting up to {PROD_AUTH_WAIT_S:g} s for the authorization")
            wait_for_authorization(svc.rail, [executed["transfer_id"]], PROD_AUTH_WAIT_S)

    transfer = svc.rail.get_transfer(executed["transfer_id"])
    verified = svc.verify_receipt(receipt_id=executed["receipt_id"])
    ok = (
        verified["valid"]
        and transfer.amount_cents == PROD_SINGLE_AMOUNT_CENTS
        and transfer.pix_key == key
        and verified["transfer_id"] == executed["transfer_id"]
    )
    out(
        f"verify: receipt {executed['receipt_id']} valid={verified['valid']} "
        f"rail transfer status={transfer.status} amount={format_brl(transfer.amount_cents)}"
    )
    out("PASS" if ok else "FAIL")
    return 0 if ok else 1
