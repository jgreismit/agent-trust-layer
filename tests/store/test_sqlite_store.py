from datetime import UTC, datetime, timedelta, timezone

import pytest

from atl.contracts import (
    ApprovalRecord,
    Decision,
    Mandate,
    PayeeRule,
    PaymentIntent,
    RailTransfer,
)
from atl.store import open_store, reset_store

NOW = datetime(2026, 1, 10, 12, 0, 0, tzinfo=UTC)


def mandate(mid="mdt_1"):
    return Mandate(
        mandate_id=mid,
        owner_id="own_1",
        agent_id="agt_1",
        payees=[
            PayeeRule(
                payee_id="p1",
                display_name="P One",
                pix_key="k1",
                pix_key_type="EVP",
                categories=["materials"],
            )
        ],
        per_payment_cap_cents=10_000,
        daily_cap_cents=50_000,
        hard_cap_cents=100_000,
        valid_until=NOW + timedelta(days=1),
        signature="sig",
    )


def intent(iid="int_1", mid="mdt_1", idem="idem1", invoice="INV-1", amount=5000):
    return PaymentIntent(
        intent_id=iid,
        mandate_id=mid,
        agent_id="agt_1",
        payee_id="p1",
        pix_key="k1",
        amount_cents=amount,
        category="materials",
        invoice_ref=invoice,
        memo="m",
        idempotency_key=idem,
    )


def decision(did="dec_1", iid="int_1", at=NOW, status="approved"):
    return Decision(
        decision_id=did,
        intent_id=iid,
        verdict="approve",
        rule_hits=[],
        reasons=[],
        status=status,
        created_at=at,
    )


def transfer():
    return RailTransfer(
        rail="simulated", transfer_id="tr_1", status="DONE", amount_cents=5000, pix_key="k1", created_at=NOW
    )


@pytest.fixture
def store(tmp_path):
    s = open_store(tmp_path / "sub" / "atl.db")
    yield s
    s.close()


def test_round_trip_all_models(store):
    m, i, d, t = mandate(), intent(), decision(), transfer()
    store.save_mandate(m)
    store.save_intent(i)
    store.save_decision(d)
    store.save_transfer("dec_1", t)
    a = ApprovalRecord(decision_id="dec_1", action="approve", owner_id="own_1", signature="s", created_at=NOW)
    store.save_approval(a)
    assert store.get_mandate("mdt_1") == m
    assert store.get_intent("int_1") == i
    assert store.get_decision("dec_1") == d
    assert store.get_transfer_for_decision("dec_1") == t
    assert store.get_approval("dec_1") == a
    assert store.get_mandate("nope") is None
    assert store.get_intent("nope") is None
    assert store.get_decision("nope") is None
    assert store.get_transfer_for_decision("nope") is None
    assert store.get_approval("nope") is None


def test_update_decision_status_and_replace(store):
    store.save_intent(intent())
    store.save_decision(decision(status="pending_owner"))
    store.update_decision_status("dec_1", "approved")
    assert store.get_decision("dec_1").status == "approved"
    store.save_decision(decision(status="executed"))
    assert store.get_decision("dec_1").status == "executed"


def test_idempotency_returns_original(store):
    store.save_mandate(mandate())
    store.save_intent(intent("int_1", idem="same"))
    store.save_decision(decision("dec_1", "int_1"))
    store.save_intent(intent("int_2", idem="same"))
    store.save_decision(decision("dec_2", "int_2", at=NOW + timedelta(minutes=1)))
    found = store.find_by_idempotency_key("same")
    assert found is not None
    assert found[0].intent_id == "int_1"
    assert found[1].decision_id == "dec_1"
    assert store.find_by_idempotency_key("missing") is None


def test_find_by_invoice(store):
    store.save_intent(intent("int_1", invoice="A"))
    store.save_decision(decision("dec_1", "int_1"))
    store.save_intent(intent("int_2", idem="x", invoice="A"))
    store.save_decision(decision("dec_2", "int_2"))
    store.save_intent(intent("int_3", idem="y", invoice="B"))
    store.save_decision(decision("dec_3", "int_3"))
    assert [i.intent_id for i, _ in store.find_by_invoice("p1", "A")] == ["int_1", "int_2"]
    assert store.find_by_invoice("p1", "C") == []


def test_decisions_since_filters(store):
    store.save_intent(intent("int_1", idem="a"))
    store.save_decision(decision("dec_1", "int_1", at=NOW - timedelta(hours=5)))
    store.save_intent(intent("int_2", idem="b"))
    store.save_decision(decision("dec_2", "int_2", at=NOW))
    store.save_intent(intent("int_3", mid="other", idem="c"))
    store.save_decision(decision("dec_3", "int_3", at=NOW))
    # non UTC aware input is compared as an instant
    brt = timezone(timedelta(hours=-3))
    since = (NOW - timedelta(hours=1)).astimezone(brt)
    got = store.decisions_since("mdt_1", since)
    assert [d.decision_id for _, d in got] == ["dec_2"]
    assert [d.decision_id for _, d in store.decisions_since("mdt_1", NOW)] == ["dec_2"]
    assert len(store.decisions_since("mdt_1", NOW - timedelta(days=1))) == 2
    assert store.decisions_since("mdt_1", NOW + timedelta(seconds=1)) == []


def test_revocation(store):
    store.save_mandate(mandate())
    assert store.get_mandate("mdt_1").revoked is False
    store.revoke_mandate("mdt_1")
    assert store.get_mandate("mdt_1").revoked is True


def test_cross_connection_visibility(tmp_path):
    path = tmp_path / "atl.db"
    a = open_store(path)
    b = open_store(path)
    a.save_mandate(mandate())
    assert b.get_mandate("mdt_1") is not None
    b.revoke_mandate("mdt_1")
    assert a.get_mandate("mdt_1").revoked is True
    a.close()
    b.close()


def test_reset_store(tmp_path):
    path = tmp_path / "atl.db"
    s = open_store(path)
    s.save_mandate(mandate())
    s.close()
    reset_store(path)
    assert not path.exists()
    s2 = open_store(path)
    assert s2.get_mandate("mdt_1") is None
    s2.close()
    reset_store(tmp_path / "never.db")
