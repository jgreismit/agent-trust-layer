import sqlite3
import stat
from datetime import UTC, datetime

import pytest

from atl.canonical import ZERO_HASH, public_key_hex, sign_hex
from atl.contracts import BAD_SIGNATURE, HASH_MISMATCH, Decision, PaymentIntent, RailTransfer
from atl.receipts import ReceiptChain, load_or_create_key, verify_chain, verify_receipt
from atl.store import open_store

NOW = datetime(2026, 1, 10, 12, 0, 0, tzinfo=UTC)


def make(n):
    intent = PaymentIntent(
        intent_id=f"int_{n}",
        mandate_id="mdt_1",
        agent_id="agt",
        payee_id="p1",
        pix_key="k1",
        amount_cents=1000 * n,
        category="c",
        invoice_ref=f"INV-{n}",
        memo="",
        idempotency_key=f"i{n}",
    )
    decision = Decision(
        decision_id=f"dec_{n}",
        intent_id=intent.intent_id,
        verdict="approve",
        rule_hits=[],
        reasons=[],
        status="executed",
        created_at=NOW,
    )
    transfer = RailTransfer(
        rail="simulated",
        transfer_id=f"tr_{n}",
        status="DONE",
        amount_cents=1000 * n,
        pix_key="k1",
        created_at=NOW,
    )
    return decision, intent, transfer


@pytest.fixture
def setup(tmp_path):
    store = open_store(tmp_path / "atl.db")
    key = load_or_create_key(tmp_path / "keys", "atl")
    chain = ReceiptChain(store, key)
    receipts = [chain.append(*make(n)) for n in range(1, 5)]
    yield store, key, chain, receipts
    store.close()


def test_chain_verifies(setup):
    store, key, _, receipts = setup
    assert [r.seq for r in receipts] == [1, 2, 3, 4]
    assert receipts[0].prev_hash == ZERO_HASH
    assert receipts[1].prev_hash == receipts[0].hash
    assert receipts[0].public_key == public_key_hex(key)
    assert verify_chain(store.list_receipts()) == []
    res = verify_receipt(receipts[0], public_key_hex(key))
    assert res.valid and res.reasons == []
    assert (res.payee_id, res.amount_cents, res.transfer_id, res.rail) == ("p1", 1000, "tr_1", "simulated")
    assert store.receipts_for_decision("dec_2")[0].receipt_id == receipts[1].receipt_id
    assert store.get_receipt(receipts[2].receipt_id).seq == 3


def test_no_transfer(setup):
    _, _, chain, _ = setup
    d, i, _ = make(9)
    r = chain.append(d, i, None)
    res = verify_receipt(r)
    assert res.valid and res.transfer_id is None and res.rail is None


def tamper(receipt, field):
    r = receipt.model_copy(deep=True)
    if field == "verdict":
        r.decision.verdict = "refuse"
    elif field == "amount":
        r.intent.amount_cents += 1
    elif field == "transfer":
        r.transfer.transfer_id = "tr_evil"
    elif field == "prev_hash":
        r.prev_hash = "1" * 64
    elif field == "seq":
        r.seq += 10
    elif field == "public_key":
        r.public_key = public_key_hex(load_other_key())
    return r


_other = []


def load_other_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if not _other:
        _other.append(Ed25519PrivateKey.generate())
    return _other[0]


@pytest.mark.parametrize("field", ["verdict", "amount", "transfer", "prev_hash", "seq", "public_key"])
def test_tamper_detected(setup, field):
    receipt = setup[3][1]
    res = verify_receipt(tamper(receipt, field))
    assert not res.valid
    assert set(res.reasons) & {HASH_MISMATCH, BAD_SIGNATURE}


def test_tamper_rehashed_still_bad_signature(setup):
    receipt = setup[3][1].model_copy(deep=True)
    receipt.intent.amount_cents += 1
    from atl.canonical import receipt_hash

    receipt.hash = receipt_hash(receipt)
    res = verify_receipt(receipt)
    assert res.reasons == [BAD_SIGNATURE]


def test_wrong_signature_and_expected_key(setup):
    key, receipt = setup[1], setup[3][0]
    bad = receipt.model_copy(update={"signature": sign_hex(load_other_key(), receipt.hash.encode("ascii"))})
    assert verify_receipt(bad).reasons == [BAD_SIGNATURE]
    assert verify_receipt(receipt, public_key_hex(load_other_key())).reasons == [BAD_SIGNATURE]
    assert verify_receipt(receipt, public_key_hex(key)).valid


def test_chain_reorder_and_missing(setup):
    receipts = setup[3]
    assert verify_chain([receipts[1], receipts[0], receipts[2], receipts[3]])
    problems = verify_chain([receipts[0], receipts[2], receipts[3]])
    assert problems and problems[0].startswith("CHAIN_BROKEN " + receipts[2].receipt_id)
    assert verify_chain(receipts[1:])


def test_duplicate_seq_rejected_and_retry(setup):
    store, key, chain, receipts = setup
    with pytest.raises(sqlite3.IntegrityError):
        store.append_receipt(receipts[0].model_copy(update={"receipt_id": "rcp_dup"}))

    # simulate another process appending between last_receipt and append_receipt
    other = ReceiptChain(store, key)
    real_last = store.last_receipt
    calls = {"n": 0}

    class Racy:
        def __getattr__(self, name):
            return getattr(store, name)

        def last_receipt(self):
            calls["n"] += 1
            result = real_last()
            if calls["n"] == 1:
                other.append(*make(20))
            return result

    r = ReceiptChain(Racy(), key).append(*make(21))
    assert r.seq == 6
    assert verify_chain(store.list_receipts()) == []


def test_key_file_mode_and_reload(tmp_path):
    k1 = load_or_create_key(tmp_path / "k", "owner")
    path = tmp_path / "k" / "owner.pem"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    k2 = load_or_create_key(tmp_path / "k", "owner")
    assert public_key_hex(k1) == public_key_hex(k2)
