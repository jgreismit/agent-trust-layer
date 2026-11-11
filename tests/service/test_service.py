"""ATLService flows with in-memory fakes (no other lane, no network)."""

from __future__ import annotations

import threading

import pytest
from l4_fakes import KEY_CF, Harness

from atl.approvals import verify_approval
from atl.canonical import mandate_signing_bytes, verify_hex
from atl.contracts import (
    DECISION_NOT_APPROVED,
    DECISION_NOT_FOUND,
    MANDATE_BAD_SIGNATURE,
    PRODUCTION_BLOCKED,
    PRODUCTION_CAP_EXCEEDED,
    R_PROD_DESTINATION_NOT_ALLOWED,
    RECEIPT_NOT_FOUND,
    ATLError,
    Mandate,
)
from atl.service import run_tool

PROD_KEY = "marlon@example.com"


def _code(fn, *a, **kw) -> str:
    with pytest.raises(ATLError) as e:
        fn(*a, **kw)
    return e.value.code


# ------------------------------------------------------------------ mandates


def test_sign_and_register_mandate(h):
    signed = h.svc.sign_mandate(h.mandate())
    m = Mandate.model_validate(signed)
    assert verify_hex(h.svc.owner_public_key, mandate_signing_bytes(m), m.signature)
    assert h.svc.register_mandate(signed) == {"mandate_id": "mdt_demo"}
    assert "mdt_demo" in h.store.mandates


def test_tampered_mandate_rejected_and_not_stored(h):
    signed = h.svc.sign_mandate(h.mandate())
    signed["per_payment_cap_cents"] = 999999
    assert _code(h.svc.register_mandate, signed) == MANDATE_BAD_SIGNATURE
    assert h.store.mandates == {}


def test_mandate_signed_by_other_key_rejected(h, tmp_path):
    other = Harness(tmp_path / "other")
    signed = other.svc.sign_mandate(h.mandate())
    assert _code(h.svc.register_mandate, signed) == MANDATE_BAD_SIGNATURE
    unsigned = {**h.mandate(), "signature": ""}
    assert _code(h.svc.register_mandate, unsigned) == MANDATE_BAD_SIGNATURE
    assert h.store.mandates == {}


def test_production_mandate_caps(tmp_path):
    p = Harness(tmp_path, env="production", prod_key=PROD_KEY)
    assert _code(p.svc.register_mandate, p.svc.sign_mandate(p.mandate())) == PRODUCTION_CAP_EXCEEDED
    signed = p.svc.sign_mandate(p.mandate(per_payment_cap_cents=500, daily_cap_cents=1001))
    assert _code(p.svc.register_mandate, signed) == PRODUCTION_CAP_EXCEEDED
    ok = p.svc.sign_mandate(p.mandate(per_payment_cap_cents=500, daily_cap_cents=1000))
    assert p.svc.register_mandate(ok)["mandate_id"] == "mdt_demo"


def test_revoke(h):
    h.register()
    assert h.svc.revoke("mdt_demo") == {"mandate_id": "mdt_demo", "revoked": True}
    r = h.svc.pay_check(**h.intent())
    assert r["verdict"] == "refuse"
    assert _code(h.svc.revoke, "mdt_nope") == "MANDATE_NOT_FOUND"


# ----------------------------------------------------------------- pay_check


def test_pay_check_approve_shape_and_receipt(h):
    h.register()
    r = h.svc.pay_check(**h.intent())
    assert set(r) == {
        "decision_id",
        "verdict",
        "status",
        "rule_hits",
        "reasons",
        "next_action",
        "receipt_id",
        "original_decision_id",
    }
    assert r["verdict"] == "approve" and r["status"] == "approved"
    assert r["next_action"] == "call_execute_payment"
    assert r["decision_id"].startswith("dec_")
    rec = h.store.get_receipt(r["receipt_id"])
    assert rec.transfer is None and rec.decision.decision_id == r["decision_id"]
    assert rec.intent.intent_id.startswith("int_")
    assert h.rail_factory_calls == 0  # pay_check never touches the rail


def test_pay_check_escalate_and_refuse_next_actions(h):
    h.register()
    esc = h.svc.pay_check(**h.intent(amount_cents=4800, invoice_ref="NF-1003"))
    assert (esc["verdict"], esc["status"], esc["next_action"]) == (
        "escalate",
        "pending_owner",
        "wait_for_owner",
    )
    ref = h.svc.pay_check(**h.intent(pix_key="attacker-key", invoice_ref="NF-2"))
    assert (ref["verdict"], ref["next_action"]) == ("refuse", "do_not_pay_report_to_owner")
    assert ref["rule_hits"] == ["R3_KEY_MISMATCH"]


def test_pay_check_duplicate_returns_original(h):
    h.register()
    first = h.svc.pay_check(**h.intent(idempotency_key="k1"))
    again = h.svc.pay_check(**h.intent(idempotency_key="k1"))
    assert again["verdict"] == "refuse"
    assert again["original_decision_id"] == first["decision_id"]


def test_production_destination_not_allowed(tmp_path):
    p = Harness(tmp_path, env="production", prod_key=PROD_KEY)
    p.register(per_payment_cap_cents=500, daily_cap_cents=1000, hard_cap_cents=500)
    r = p.svc.pay_check(**p.intent(amount_cents=100))
    assert r["verdict"] == "refuse"
    assert R_PROD_DESTINATION_NOT_ALLOWED in r["rule_hits"]
    assert _code(p.svc.execute_payment, r["decision_id"]) == DECISION_NOT_APPROVED
    assert p.rail.calls == []


# ------------------------------------------------------------------- execute


def test_execute_approved(h):
    h.register()
    r = h.svc.pay_check(**h.intent())
    out = h.svc.execute_payment(r["decision_id"])
    assert out["status"] == "executed" and out["transfer_id"] == "tr_1"
    assert out["rail"] == "simulated" and out["amount_cents"] == 1200
    call = h.rail.calls[0]
    assert call["pix_key"] == KEY_CF and call["pix_key_type"] == "EVP"
    assert call["amount_cents"] == 1200 and call["idempotency_key"] == r["decision_id"]
    assert "Measurement" not in call["description"]  # untrusted memo never goes to the rail
    rec = h.store.get_receipt(out["receipt_id"])
    assert rec.transfer.transfer_id == "tr_1" and rec.decision.status == "executed"
    assert h.store.get_decision(r["decision_id"]).status == "executed"
    g = h.svc.get_decision(r["decision_id"])
    assert g["status"] == "executed" and g["next_action"] == "already_executed"
    assert g["transfer_id"] == "tr_1" and g["receipt_id"] == out["receipt_id"]


def test_s40_refused_cannot_execute(h):
    h.register()
    r = h.svc.pay_check(**h.intent(pix_key="attacker-key"))
    assert _code(h.svc.execute_payment, r["decision_id"]) == DECISION_NOT_APPROVED
    assert h.rail.calls == [] and h.rail_factory_calls == 0
    events = [x for x in h.log_lines() if x["event"] == "execute_refused"]
    assert events and events[0]["rail_called"] is False and events[0]["status"] == "refused"


def test_pending_cannot_execute(h):
    h.register()
    r = h.svc.pay_check(**h.intent(amount_cents=4800))
    assert _code(h.svc.execute_payment, r["decision_id"]) == DECISION_NOT_APPROVED
    assert h.rail.calls == []


def test_s41_double_execute_one_rail_call(h):
    h.register()
    r = h.svc.pay_check(**h.intent())
    first = h.svc.execute_payment(r["decision_id"])
    second = h.svc.execute_payment(r["decision_id"])
    assert first == second
    assert len(h.rail.calls) == 1


def test_s41_concurrent_execute_one_rail_call(h):
    h.register()
    r = h.svc.pay_check(**h.intent())
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(h.svc.execute_payment(r["decision_id"])))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(h.rail.calls) == 1
    assert len({x["transfer_id"] for x in results}) == 1


def test_execute_guard_key_mismatch(h):
    h.register()
    r = h.svc.pay_check(**h.intent())
    # Simulate a store row whose intent key no longer matches the mandate payee.
    intent = h.store.intents[h.store.decisions[r["decision_id"]].intent_id]
    h.store.intents[intent.intent_id] = intent.model_copy(update={"pix_key": "other"})
    assert _code(h.svc.execute_payment, r["decision_id"]) == DECISION_NOT_APPROVED
    assert h.rail.calls == []


def test_execute_unknown_decision(h):
    assert _code(h.svc.execute_payment, "dec_nope") == DECISION_NOT_FOUND


# ------------------------------------------------------------ owner approval


def test_approve_flow_and_signature(h):
    h.register()
    r = h.svc.pay_check(**h.intent(amount_cents=4800))
    d = h.svc.approve(r["decision_id"])
    assert d["status"] == "approved" and d["next_action"] == "call_execute_payment"
    rec = h.store.get_approval(r["decision_id"])
    assert rec.action == "approve" and rec.owner_id == "owner-marlon"
    assert verify_approval(rec, h.svc.owner_public_key)
    assert not verify_approval(rec.model_copy(update={"action": "refuse"}), h.svc.owner_public_key)
    ev = [x for x in h.log_lines() if x["event"] == "owner_approval"]
    assert ev[0]["decision_id"] == r["decision_id"] and ev[0]["action"] == "approve"
    assert ev[0]["owner_id"] == "owner-marlon"
    assert h.svc.get_decision(r["decision_id"])["next_action"] == "call_execute_payment"
    out = h.svc.execute_payment(r["decision_id"])
    assert out["amount_cents"] == 4800
    statuses = [x.decision.status for x in h.store.receipts_for_decision(r["decision_id"])]
    assert statuses == ["pending_owner", "approved", "executed"]


def test_refuse_flow(h):
    h.register()
    r = h.svc.pay_check(**h.intent(amount_cents=4800))
    d = h.svc.refuse(r["decision_id"])
    assert d["status"] == "refused" and d["next_action"] == "do_not_pay_report_to_owner"
    assert verify_approval(h.store.get_approval(r["decision_id"]), h.svc.owner_public_key)
    assert _code(h.svc.execute_payment, r["decision_id"]) == DECISION_NOT_APPROVED
    assert _code(h.svc.approve, r["decision_id"]) == DECISION_NOT_APPROVED


def test_approve_only_pending(h):
    h.register()
    r = h.svc.pay_check(**h.intent())
    assert _code(h.svc.approve, r["decision_id"]) == DECISION_NOT_APPROVED


def test_s42_escalation_expires(h):
    h.register()
    r = h.svc.pay_check(**h.intent(amount_cents=4800))
    h.clock.advance(minutes=14, seconds=59)
    assert h.svc.get_decision(r["decision_id"])["status"] == "pending_owner"
    h.clock.advance(seconds=1)
    g = h.svc.get_decision(r["decision_id"])
    assert g["status"] == "expired" and g["next_action"] == "do_not_pay_report_to_owner"
    assert _code(h.svc.execute_payment, r["decision_id"]) == DECISION_NOT_APPROVED
    assert _code(h.svc.approve, r["decision_id"]) == DECISION_NOT_APPROVED
    assert h.rail.calls == []
    statuses = [x.decision.status for x in h.store.receipts_for_decision(r["decision_id"])]
    assert statuses == ["pending_owner", "expired"]  # expiry receipt appended once


def test_approve_after_expiry_window_refused(h):
    h.register()
    r = h.svc.pay_check(**h.intent(amount_cents=4800))
    h.clock.advance(minutes=16)
    assert _code(h.svc.approve, r["decision_id"]) == DECISION_NOT_APPROVED
    assert h.store.get_decision(r["decision_id"]).status == "expired"


def test_get_decision_wait_polls_until_approved(h):
    h.register()
    r = h.svc.pay_check(**h.intent(amount_cents=4800))
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 2:
            h.svc.approve(r["decision_id"])  # the owner answers from "another process"

    h.svc.sleep_fn = sleep
    g = h.svc.get_decision(r["decision_id"], wait_seconds=30)
    assert g["status"] == "approved" and len(sleeps) == 2


def test_get_decision_wait_capped(h, monkeypatch):
    import atl.service as service_mod

    h.register()
    r = h.svc.pay_check(**h.intent(amount_cents=4800))
    now = [0.0]
    monkeypatch.setattr(service_mod.time, "monotonic", lambda: now[0])

    def sleep(s):
        now[0] += s

    h.svc.sleep_fn = sleep
    g = h.svc.get_decision(r["decision_id"], wait_seconds=999)
    assert g["status"] == "pending_owner"
    assert now[0] == 30


# ------------------------------------------------------------------ receipts


def test_get_and_verify_receipt(h):
    h.register()
    r = h.svc.pay_check(**h.intent())
    out = h.svc.execute_payment(r["decision_id"])
    rec = h.svc.get_receipt(out["receipt_id"])
    assert rec["transfer"]["transfer_id"] == "tr_1"
    v = h.svc.verify_receipt(receipt_id=out["receipt_id"])
    assert v["valid"] is True and v["amount_cents"] == 1200 and v["transfer_id"] == "tr_1"
    rec["intent"]["amount_cents"] = 120000
    bad = h.svc.verify_receipt(receipt=rec)
    assert bad["valid"] is False and "HASH_MISMATCH" in bad["reasons"]
    assert _code(h.svc.get_receipt, "rcp_nope") == RECEIPT_NOT_FOUND


# --------------------------------------------------------- production guards


def _prod(tmp_path, inputs, **kw):
    p = Harness(tmp_path, env="production", prod_key=PROD_KEY, inputs=inputs, rail_name="asaas_production")
    p.register(
        per_payment_cap_cents=500,
        daily_cap_cents=1000,
        hard_cap_cents=500,
        payees=[
            dict(
                payee_id="me",
                display_name="Marlon",
                pix_key=PROD_KEY,
                pix_key_type="EMAIL",
                categories=["promotion"],
            )
        ],
    )
    return p


def _prod_intent(p, amount=100, **kw):
    return p.svc.pay_check(
        **p.intent(payee_id="me", pix_key=PROD_KEY, amount_cents=amount, category="promotion", **kw)
    )


def test_production_requires_pay(tmp_path):
    p = _prod(tmp_path, inputs=["pay"])
    r = _prod_intent(p)
    assert r["verdict"] == "approve"
    assert _code(p.svc.execute_payment, r["decision_id"]) == PRODUCTION_BLOCKED
    assert p.rail.calls == []
    text = "\n".join(p.output)
    assert "PRODUCTION" in text and "R$ 1.00" in text and "Marlon" in text
    assert PROD_KEY not in text and "ma***@example.com" in text
    p.inputs.append("PAY")
    out = p.svc.execute_payment(r["decision_id"])
    assert out["rail"] == "asaas_production" and len(p.rail.calls) == 1


def test_production_daily_limit(tmp_path):
    p = _prod(tmp_path, inputs=["PAY"] * 5)
    for i in range(2):
        r = _prod_intent(p, amount=500, invoice_ref=f"P-{i}")
        p.svc.execute_payment(r["decision_id"])
    r = _prod_intent(p, amount=100, invoice_ref="P-3")
    assert _code(p.svc.execute_payment, r["decision_id"]) == PRODUCTION_CAP_EXCEEDED
    assert len(p.rail.calls) == 2


def test_tool_call_log_lines(h):
    h.register()
    args = h.intent()
    res = run_tool(h.runlog, "pay_check", args, lambda: h.svc.pay_check(**args))
    err = run_tool(
        h.runlog, "execute_payment", {"decision_id": "dec_x"}, lambda: h.svc.execute_payment("dec_x")
    )
    assert err == {"error": DECISION_NOT_FOUND, "message": "dec_x"}
    lines = [x for x in h.log_lines() if x["event"] == "tool_call"]
    assert len(lines) == 2
    first = lines[0]
    assert first["tool"] == "pay_check" and first["run_id"] == "run_test"
    assert first["args"]["invoice_ref"] == "NF-1001"
    assert first["result"]["decision_id"] == res["decision_id"]
    assert first["result"]["verdict"] == "approve" and first["result"]["rule_hits"] == []
    assert isinstance(first["latency_ms"], int)
    assert lines[1]["result"]["error"] == DECISION_NOT_FOUND
