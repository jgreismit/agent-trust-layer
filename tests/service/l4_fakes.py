"""In-memory fakes for L4 tests: store, receipt chain, policy, rail and receipt verifier.

They follow the frozen contracts in atl/contracts.py so the tests do not depend on lanes L1, L2, L3.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atl.canonical import (
    ZERO_HASH,
    mandate_signing_bytes,
    public_key_hex,
    receipt_hash,
    sign_hex,
    verify_hex,
)
from atl.config import SANDBOX_BASE_URL, Settings
from atl.contracts import (
    BAD_SIGNATURE,
    HASH_MISMATCH,
    R1_MANDATE_INVALID,
    R3_KEY_MISMATCH,
    R5_DUPLICATE,
    R6_OVER_PER_PAYMENT_CAP,
    ApprovalRecord,
    Decision,
    Mandate,
    PaymentIntent,
    RailTransfer,
    Receipt,
    VerifyResult,
)
from atl.ids import new_id
from atl.runlog import RunLog
from atl.service import ATLService

T0 = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
KEY_CF = "11111111-2222-3333-4444-555555555555"


class FakeStore:
    def __init__(self):
        self.mandates: dict[str, Mandate] = {}
        self.intents: dict[str, PaymentIntent] = {}
        self.decisions: dict[str, Decision] = {}
        self.approvals: dict[str, ApprovalRecord] = {}
        self.transfers: dict[str, RailTransfer] = {}
        self.receipts: list[Receipt] = []

    # StoreView
    def find_by_idempotency_key(self, key):
        for d in self.decisions.values():
            i = self.intents[d.intent_id]
            if i.idempotency_key == key:
                return i, d
        return None

    def find_by_invoice(self, payee_id, invoice_ref):
        return [
            (self.intents[d.intent_id], d)
            for d in self.decisions.values()
            if self.intents[d.intent_id].payee_id == payee_id
            and self.intents[d.intent_id].invoice_ref == invoice_ref
        ]

    def decisions_since(self, mandate_id, since):
        return [
            (self.intents[d.intent_id], d)
            for d in self.decisions.values()
            if self.intents[d.intent_id].mandate_id == mandate_id and d.created_at >= since
        ]

    # Store
    def save_mandate(self, m):
        self.mandates[m.mandate_id] = m

    def get_mandate(self, mandate_id):
        return self.mandates.get(mandate_id)

    def revoke_mandate(self, mandate_id):
        self.mandates[mandate_id] = self.mandates[mandate_id].model_copy(update={"revoked": True})

    def save_intent(self, intent):
        self.intents[intent.intent_id] = intent

    def get_intent(self, intent_id):
        return self.intents.get(intent_id)

    def save_decision(self, d):
        self.decisions[d.decision_id] = d

    def get_decision(self, decision_id):
        return self.decisions.get(decision_id)

    def update_decision_status(self, decision_id, status):
        self.decisions[decision_id] = self.decisions[decision_id].model_copy(update={"status": status})

    def save_approval(self, a):
        self.approvals[a.decision_id] = a

    def get_approval(self, decision_id):
        return self.approvals.get(decision_id)

    def save_transfer(self, decision_id, t):
        self.transfers[decision_id] = t

    def get_transfer_for_decision(self, decision_id):
        return self.transfers.get(decision_id)

    def append_receipt(self, r):
        self.receipts.append(r)

    def last_receipt(self):
        return self.receipts[-1] if self.receipts else None

    def get_receipt(self, receipt_id):
        return next((r for r in self.receipts if r.receipt_id == receipt_id), None)

    def list_receipts(self):
        return list(self.receipts)

    def receipts_for_decision(self, decision_id):
        return [r for r in self.receipts if r.decision.decision_id == decision_id]


class FakeChain:
    def __init__(self, store: FakeStore, atl_key: Ed25519PrivateKey):
        self.store = store
        self.atl_key = atl_key

    def append(self, decision, intent, transfer):
        last = self.store.last_receipt()
        r = Receipt(
            receipt_id=new_id("rcp"),
            seq=(last.seq + 1) if last else 1,
            prev_hash=last.hash if last else ZERO_HASH,
            decision=decision,
            intent=intent,
            transfer=transfer,
            hash="",
            signature="",
            public_key=public_key_hex(self.atl_key),
        )
        h = receipt_hash(r)
        r = r.model_copy(update={"hash": h, "signature": sign_hex(self.atl_key, h.encode("ascii"))})
        self.store.append_receipt(r)
        return r


def fake_verify(receipt: Receipt, expected_public_key: str | None = None) -> VerifyResult:
    reasons = []
    if receipt_hash(receipt) != receipt.hash:
        reasons.append(HASH_MISMATCH)
    key = expected_public_key or receipt.public_key
    if not verify_hex(key, receipt.hash.encode("ascii"), receipt.signature):
        reasons.append(BAD_SIGNATURE)
    t = receipt.transfer
    return VerifyResult(
        valid=not reasons,
        reasons=reasons,
        receipt_id=receipt.receipt_id,
        payee_id=receipt.intent.payee_id,
        amount_cents=receipt.intent.amount_cents,
        transfer_id=t.transfer_id if t else None,
        rail=t.rail if t else None,
        verdict=receipt.decision.verdict,
        status=receipt.decision.status,
    )


def fake_evaluate(intent, mandate, view, now, *, owner_public_key):
    """Tiny subset of the rules: R1, R3, R5 (idempotency), R6."""
    hits, reasons, original = [], [], None
    refuse = escalate = False
    if (
        mandate is None
        or mandate.revoked
        or mandate.agent_id != intent.agent_id
        or not verify_hex(owner_public_key, mandate_signing_bytes(mandate), mandate.signature)
    ):
        hits.append(R1_MANDATE_INVALID)
        reasons.append("mandate invalid")
        refuse = True
    else:
        payee = next((p for p in mandate.payees if p.payee_id == intent.payee_id), None)
        if payee is None or payee.pix_key != intent.pix_key:
            hits.append(R3_KEY_MISMATCH)
            reasons.append("key mismatch")
            refuse = True
        dup = view.find_by_idempotency_key(intent.idempotency_key)
        if dup:
            hits.append(R5_DUPLICATE)
            reasons.append("duplicate")
            original = dup[1].decision_id
            refuse = True
        if intent.amount_cents > mandate.per_payment_cap_cents:
            hits.append(R6_OVER_PER_PAYMENT_CAP)
            reasons.append("over per-payment cap")
            escalate = True
    verdict = "refuse" if refuse else ("escalate" if escalate else "approve")
    status = {"refuse": "refused", "escalate": "pending_owner", "approve": "approved"}[verdict]
    return Decision(
        decision_id=new_id("dec"),
        intent_id=intent.intent_id,
        verdict=verdict,
        rule_hits=hits,
        reasons=reasons,
        status=status,
        created_at=now,
        original_decision_id=original,
    )


class FakeRail:
    def __init__(self, name="simulated"):
        self.name = name
        self.calls: list[dict] = []
        self.by_id: dict[str, RailTransfer] = {}

    def balance_cents(self):
        return 20000

    def transfer_pix(self, *, pix_key, pix_key_type, amount_cents, idempotency_key, description):
        self.calls.append(
            dict(
                pix_key=pix_key,
                pix_key_type=pix_key_type,
                amount_cents=amount_cents,
                idempotency_key=idempotency_key,
                description=description,
            )
        )
        t = RailTransfer(
            rail=self.name,
            transfer_id=f"tr_{len(self.calls)}",
            status="DONE",
            amount_cents=amount_cents,
            pix_key=pix_key,
            created_at=T0,
        )
        self.by_id[t.transfer_id] = t
        return t

    def get_transfer(self, transfer_id):
        return self.by_id[transfer_id]


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t = self.t + timedelta(**kw)


def make_settings(tmp_path: Path, env="sandbox", prod_key=None, prod_key_type=None) -> Settings:
    return Settings(
        env=env,
        rail="simulated",
        base_url=SANDBOX_BASE_URL,
        api_key=None,
        root=tmp_path,
        var_dir=tmp_path / "var",
        db_path=tmp_path / "var" / "atl.db",
        keys_dir=tmp_path / "var" / "keys",
        runs_dir=tmp_path / "runs",
        run_id="run_test",
        prod_allowed_pix_key=prod_key,
        prod_allowed_pix_key_type=prod_key_type,
    )


class Harness:
    def __init__(self, tmp_path: Path, env="sandbox", prod_key=None, inputs=None, rail_name="simulated"):
        self.settings = make_settings(tmp_path, env, prod_key, "EMAIL" if prod_key else None)
        self.store = FakeStore()
        self.rail = FakeRail(rail_name)
        self.rail_factory_calls = 0
        self.owner_key = Ed25519PrivateKey.generate()
        self.atl_key = Ed25519PrivateKey.generate()
        self.clock = Clock()
        self.inputs = list(inputs or [])
        self.output: list[str] = []
        self.runlog = RunLog("run_test", tmp_path / "runs")

        def rail_factory():
            self.rail_factory_calls += 1
            return self.rail

        self.svc = ATLService(
            self.settings,
            self.store,
            rail_factory,
            FakeChain(self.store, self.atl_key),
            fake_evaluate,
            self.owner_key,
            self.atl_key,
            self.runlog,
            self.clock,
            lambda: self.inputs.pop(0) if self.inputs else "",
            output_fn=self.output.append,
            sleep_fn=lambda s: None,
            verify_fn=fake_verify,
        )

    def mandate(self, **over) -> dict:
        data = dict(
            mandate_id="mdt_demo",
            owner_id="owner-marlon",
            agent_id="procurement-01",
            payees=[
                dict(
                    payee_id="concreto-forte",
                    display_name="Concreto Forte",
                    pix_key=KEY_CF,
                    pix_key_type="EVP",
                    categories=["materials"],
                )
            ],
            per_payment_cap_cents=3000,
            daily_cap_cents=8000,
            hard_cap_cents=10000,
            valid_until=(T0 + timedelta(days=1)).isoformat(),
        )
        data.update(over)
        return data

    def register(self, **over) -> str:
        return self.svc.register_mandate(self.svc.sign_mandate(self.mandate(**over)))["mandate_id"]

    def intent(self, **over) -> dict:
        data = dict(
            mandate_id="mdt_demo",
            agent_id="procurement-01",
            payee_id="concreto-forte",
            pix_key=KEY_CF,
            amount_cents=1200,
            category="materials",
            invoice_ref="NF-1001",
            memo="Measurement #1",
            idempotency_key=new_id("idem"),
        )
        data.update(over)
        return data

    def log_lines(self) -> list[dict]:
        import json

        if not self.runlog.path.exists():
            return []
        return [json.loads(x) for x in self.runlog.path.read_text().splitlines()]
