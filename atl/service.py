"""Core service used by the MCP server and the CLI (lane L4).

Public API (frozen; every method returns JSON-safe dicts; errors raise atl.contracts.ATLError):
    ATLService.from_settings(settings) -> ATLService
    sign_mandate(mandate: dict) -> dict          # owner action: fills `signature` with the owner key
    register_mandate(mandate: dict) -> {"mandate_id"}
    pay_check(*, mandate_id, agent_id, payee_id, pix_key, amount_cents, category, invoice_ref, memo,
              idempotency_key, destination=None, currency=None)
        -> {"decision_id", "verdict", "status", "rule_hits", "reasons", "next_action", "receipt_id",
            "original_decision_id"}
        destination / currency (Stripe rail): the Stripe recipient the agent intends to pay and the
        currency it believes it pays in. A mandate currency the configured rail does not pay is refused (R11).
    get_decision(decision_id, wait_seconds: int = 0) -> {"decision_id", "verdict", "status", "rule_hits",
                                  "reasons", "next_action", "transfer_id", "receipt_id"}
        wait_seconds (capped at 30) blocks while the status is pending_owner, so an agent without a
        sleep tool can wait for the owner. Expiry (15 min) is applied on read.
    execute_payment(decision_id) -> {"decision_id", "status", "transfer_id", "receipt_id", "rail",
                                     "amount_cents"}
    get_receipt(receipt_id) -> receipt dict
    verify_receipt(receipt_id: str | None = None, receipt: dict | None = None) -> VerifyResult dict
    approve(decision_id) -> decision dict ; refuse(decision_id) -> decision dict
    revoke(mandate_id) -> {"mandate_id", "revoked": True}
    owner_public_key -> str (hex) ; atl_public_key -> str (hex)

Stripe rail (owner side, never exposed to the agent):
    execute_payment may return status "pending_rail_confirmation" when Stripe holds the payment for its
    human confirmation; the Dashboard URL goes to the owner (output_fn and run log), not to the agent.
    confirm_rail_payment(decision_id, approval_token=None) -> executed view, or the still pending view
    abandon_rail_payment(decision_id) -> decision dict (status expired; nothing was paid)
    pending_rail_payments() -> list of held payments with their confirmation URLs
    setup_stripe_recipient(payee_id, display_name, email, country="US") -> recipient record

Every status change (expiry, approval, execution) runs under a reentrant lock that combines a thread lock
with an fcntl file lock in var/locks, so two processes cannot execute the same decision twice.
"""

from __future__ import annotations

import fcntl
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from atl.approvals import (
    PROD_MAX_PER_DAY_CENTS,
    PROD_MAX_PER_PAYMENT_CENTS,
    format_brl,
    mask_key,
    sao_paulo_day_start,
    sign_approval,
)
from atl.canonical import mandate_signing_bytes, public_key_hex, sign_hex, verify_hex
from atl.config import Settings
from atl.contracts import (
    DECISION_NOT_APPROVED,
    DECISION_NOT_FOUND,
    ESCALATION_TTL_SECONDS,
    MANDATE_BAD_SIGNATURE,
    MANDATE_NOT_FOUND,
    PENDING_RAIL_CONFIRMATION,
    PRODUCTION_BLOCKED,
    PRODUCTION_CAP_EXCEEDED,
    R1_MANDATE_INVALID,
    R11_CURRENCY_MISMATCH,
    R_PROD_DESTINATION_NOT_ALLOWED,
    RAIL_CURRENCY,
    RECEIPT_NOT_FOUND,
    ATLError,
    Decision,
    Mandate,
    PaymentIntent,
    Rail,
    RailTransfer,
    Receipt,
    Store,
    VerifyResult,
)
from atl.ids import new_id
from atl.runlog import RunLog, Timer

INVALID_ARGUMENT = "INVALID_ARGUMENT"
INTERNAL_ERROR = "INTERNAL_ERROR"
MAX_WAIT_SECONDS = 30
POLL_SECONDS = 1.0

NEXT_ACTION_BY_VERDICT = {
    "approve": "call_execute_payment",
    "escalate": "wait_for_owner",
    "refuse": "do_not_pay_report_to_owner",
}
NEXT_ACTION_BY_STATUS = {
    "approved": "call_execute_payment",
    "pending_owner": "wait_for_owner",
    "refused": "do_not_pay_report_to_owner",
    "expired": "do_not_pay_report_to_owner",
    "executed": "already_executed",
    "pending_rail_confirmation": "wait_for_owner",
}
WAITING_STATUSES = ("pending_owner", "pending_rail_confirmation")
_SUMMARY_KEYS = ("decision_id", "verdict", "status", "rule_hits", "transfer_id", "receipt_id", "error")


class _ProcessLock:
    """Reentrant within a thread; exclusive across threads and across processes (fcntl.flock)."""

    def __init__(self, path: Path):
        self.path = path
        self._tlock = threading.RLock()
        self._depth = 0
        self._fh: Any = None

    @contextmanager
    def hold(self) -> Iterator[None]:
        with self._tlock:
            if self._depth == 0:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fh = self.path.open("a")
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                self._fh = fh
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                    self._fh.close()
                    self._fh = None


def _stderr(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def summarize(result: Any) -> dict:
    """Small result summary for the tool call log."""
    if not isinstance(result, dict):
        return {}
    out = {k: result[k] for k in _SUMMARY_KEYS if k in result}
    decision = result.get("decision")
    if isinstance(decision, dict):
        for k in ("decision_id", "verdict", "status", "rule_hits"):
            if k in decision and k not in out:
                out[k] = decision[k]
    transfer = result.get("transfer")
    if isinstance(transfer, dict) and "transfer_id" not in out:
        out["transfer_id"] = transfer.get("transfer_id")
    if "valid" in result:
        out["valid"] = result["valid"]
    return out


def error_result(exc: BaseException) -> dict:
    if isinstance(exc, ATLError):
        return {"error": exc.code, "message": exc.message or exc.code}
    if isinstance(exc, ValidationError):
        return {"error": INVALID_ARGUMENT, "message": str(exc)}
    return {"error": INTERNAL_ERROR, "message": f"{type(exc).__name__}: {exc}"}


def log_tool_call(runlog: RunLog | None, tool: str, args: dict, result: dict, latency_ms: int) -> None:
    if runlog is None:
        return
    runlog.write("tool_call", tool=tool, args=args, result=summarize(result), latency_ms=latency_ms)


def run_tool(runlog: RunLog | None, tool: str, args: dict, fn: Callable[[], dict]) -> dict:
    """Run one tool call, turn errors into {"error", "message"} and write one tool_call log line."""
    with Timer() as t:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - every error goes back to the agent as data
            result = error_result(exc)
    log_tool_call(runlog, tool, args, result, t.ms)
    return result


class ATLService:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        rail_factory: Callable[[], Rail],
        chain: Any,
        evaluate_fn: Callable[..., Decision],
        owner_key: Ed25519PrivateKey,
        atl_key: Ed25519PrivateKey,
        runlog: RunLog | None = None,
        now_fn: Callable[[], datetime] | None = None,
        input_fn: Callable[[], str] | None = None,
        *,
        output_fn: Callable[[str], None] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        verify_fn: Callable[..., VerifyResult] | None = None,
    ):
        self.settings = settings
        self.store = store
        self._rail_factory = rail_factory
        self._rail: Rail | None = None
        self.chain = chain
        self.evaluate_fn = evaluate_fn
        self.owner_key = owner_key
        self.atl_key = atl_key
        self.runlog = runlog
        self.now_fn = now_fn or (lambda: datetime.now(UTC))
        self.input_fn = input_fn or (lambda: input())
        self.output_fn = output_fn or _stderr
        self.sleep_fn = sleep_fn
        self._verify_fn = verify_fn
        # Production guards: on for ATL_ENV=production; `atl demo --production-single` turns them on
        # in sandbox for the dry run.
        self.production_guards = settings.env == "production"
        self.require_confirmation = settings.env == "production"
        self.prod_allowed_pix_key = settings.prod_allowed_pix_key
        self._lock = _ProcessLock(Path(settings.var_dir) / "locks" / "execute.lock")

    # ------------------------------------------------------------------ wiring

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any) -> ATLService:
        from atl.policy import evaluate
        from atl.receipts import ReceiptChain, load_or_create_key
        from atl.store import open_store

        Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        store = open_store(settings.db_path)
        owner_key = load_or_create_key(settings.keys_dir, "owner")
        atl_key = load_or_create_key(settings.keys_dir, "atl")

        def rail_factory() -> Rail:
            from atl.rails import get_rail

            return get_rail(settings)

        kwargs: dict[str, Any] = dict(
            settings=settings,
            store=store,
            rail_factory=rail_factory,
            chain=ReceiptChain(store, atl_key),
            evaluate_fn=evaluate,
            owner_key=owner_key,
            atl_key=atl_key,
            runlog=RunLog(runs_dir=settings.runs_dir),
        )
        kwargs.update(overrides)
        return cls(**kwargs)

    @property
    def rail(self) -> Rail:
        if self._rail is None:
            self._rail = self._rail_factory()
        return self._rail

    @property
    def owner_public_key(self) -> str:
        return public_key_hex(self.owner_key)

    @property
    def atl_public_key(self) -> str:
        return public_key_hex(self.atl_key)

    def _log(self, event: str, **fields: Any) -> None:
        if self.runlog is not None:
            self.runlog.write(event, **fields)

    # ---------------------------------------------------------------- mandates

    def sign_mandate(self, mandate: dict) -> dict:
        data = {**mandate, "signature": ""}
        m = Mandate.model_validate(data)
        signed = m.model_copy(update={"signature": sign_hex(self.owner_key, mandate_signing_bytes(m))})
        return signed.model_dump(mode="json")

    def register_mandate(self, mandate: dict) -> dict:
        m = Mandate.model_validate(mandate)
        if not m.signature or not verify_hex(self.owner_public_key, mandate_signing_bytes(m), m.signature):
            raise ATLError(MANDATE_BAD_SIGNATURE, "mandate signature does not verify with the owner key")
        if self.production_guards and (
            m.per_payment_cap_cents > PROD_MAX_PER_PAYMENT_CENTS or m.daily_cap_cents > PROD_MAX_PER_DAY_CENTS
        ):
            raise ATLError(
                PRODUCTION_CAP_EXCEEDED,
                f"production allows at most {PROD_MAX_PER_PAYMENT_CENTS} cents per payment and "
                f"{PROD_MAX_PER_DAY_CENTS} cents per day",
            )
        self.store.save_mandate(m)
        return {"mandate_id": m.mandate_id}

    def get_mandate(self, mandate_id: str, agent_id: str) -> dict:
        """Agent-safe, read-only view of the mandate the agent pays under (HW9 usability fix).

        Lets an agent map an invoice to the exact payee_id, destination and category before pay_check,
        instead of guessing them. A mandate that does not exist, or belongs to another agent, answers
        MANDATE_NOT_FOUND in both cases so the tool does not reveal other agents' mandates. Nothing here
        can change a mandate or move money.
        """
        from atl.policy import _COUNTS_FOR_DAY, _day_start

        mandate = self.store.get_mandate(mandate_id)
        if mandate is None or mandate.agent_id != agent_id:
            raise ATLError(MANDATE_NOT_FOUND, f"no mandate {mandate_id} for agent {agent_id}")
        now = self.now_fn()
        spent_today = sum(
            i.amount_cents
            for i, d in self.store.decisions_since(mandate.mandate_id, _day_start(now, mandate.currency))
            if d.status in _COUNTS_FOR_DAY
        )
        payees = []
        for p in mandate.payees:
            view = {"payee_id": p.payee_id, "display_name": p.display_name, "categories": list(p.categories)}
            if p.destination_kind == "pix":
                view.update(pix_key=p.pix_key, pix_key_type=p.pix_key_type)
            else:
                view["destinations"] = p.destinations()
            payees.append(view)
        return {
            "mandate_id": mandate.mandate_id,
            "agent_id": mandate.agent_id,
            "currency": mandate.currency,
            "valid_until": mandate.valid_until.isoformat() if mandate.valid_until else None,
            "revoked": mandate.revoked,
            "per_payment_cap_cents": mandate.per_payment_cap_cents,
            "daily_cap_cents": mandate.daily_cap_cents,
            "hard_cap_cents": mandate.hard_cap_cents,
            "spent_today_cents": spent_today,
            "remaining_today_cents": max(0, mandate.daily_cap_cents - spent_today),
            "payees": payees,
            "how_to_use": (
                "Match the invoice's payee to one payee here and pass its payee_id, its exact destination "
                "(pix_key) and one of its categories to pay_check. If the invoice's payee or destination "
                "is not listed, do not pay: report it to the owner."
            ),
        }

    def revoke(self, mandate_id: str) -> dict:
        if self.store.get_mandate(mandate_id) is None:
            raise ATLError(MANDATE_NOT_FOUND, mandate_id)
        self.store.revoke_mandate(mandate_id)
        self._log("owner_revoke", mandate_id=mandate_id)
        return {"mandate_id": mandate_id, "revoked": True}

    # --------------------------------------------------------------- decisions

    def pay_check(
        self,
        *,
        mandate_id: str,
        agent_id: str,
        payee_id: str,
        amount_cents: int,
        category: str,
        invoice_ref: str,
        memo: str,
        idempotency_key: str,
        pix_key: str = "",
        destination: str | None = None,
        currency: str | None = None,
    ) -> dict:
        intent = PaymentIntent(
            intent_id=new_id("int"),
            mandate_id=mandate_id,
            agent_id=agent_id,
            payee_id=payee_id,
            pix_key=pix_key,
            amount_cents=amount_cents,
            category=category,
            invoice_ref=invoice_ref,
            memo=memo,
            idempotency_key=idempotency_key,
            destination=destination,
            currency=currency,
        )
        mandate = self.store.get_mandate(mandate_id)
        decision = self.evaluate_fn(
            intent, mandate, self.store, self.now_fn(), owner_public_key=self.owner_public_key
        )
        decision = self._rail_currency_check(decision, mandate)
        if self.production_guards and pix_key != self.prod_allowed_pix_key:
            decision = decision.model_copy(
                update={
                    "verdict": "refuse",
                    "status": "refused",
                    "rule_hits": [*decision.rule_hits, R_PROD_DESTINATION_NOT_ALLOWED],
                    "reasons": [
                        *decision.reasons,
                        "production allows only the single allowlisted destination key",
                    ],
                }
            )
        self.store.save_intent(intent)
        self.store.save_decision(decision)
        receipt = self.chain.append(decision, intent, None)
        return {
            "decision_id": decision.decision_id,
            "verdict": decision.verdict,
            "status": decision.status,
            "rule_hits": list(decision.rule_hits),
            "reasons": list(decision.reasons),
            "next_action": NEXT_ACTION_BY_VERDICT[decision.verdict],
            "receipt_id": receipt.receipt_id,
            "original_decision_id": decision.original_decision_id,
        }

    def _rail_currency_check(self, decision: Decision, mandate: Mandate | None) -> Decision:
        """Refuse (R11) a mandate whose currency the configured rail does not pay. No rail is built here."""
        from atl.rails import rail_currency

        if mandate is None or R1_MANDATE_INVALID in decision.rule_hits:
            return decision
        rail_cur = rail_currency(self.settings)
        if mandate.currency == rail_cur:
            return decision
        reason = f"Mandate currency {mandate.currency} is not paid by the configured rail ({rail_cur} only)"
        return decision.model_copy(
            update={
                "verdict": "refuse",
                "status": "refused",
                "rule_hits": [*decision.rule_hits, R11_CURRENCY_MISMATCH],
                "reasons": [*decision.reasons, reason],
            }
        )

    def _load(self, decision_id: str) -> Decision:
        decision = self.store.get_decision(decision_id)
        if decision is None:
            raise ATLError(DECISION_NOT_FOUND, decision_id)
        return decision

    def _refresh(self, decision_id: str) -> Decision:
        """Load a decision and apply the 15 minute escalation expiry."""
        decision = self._load(decision_id)
        if decision.status != "pending_owner":
            return decision
        if self.now_fn() - decision.created_at < timedelta(seconds=ESCALATION_TTL_SECONDS):
            return decision
        with self._lock.hold():
            decision = self._load(decision_id)
            if decision.status != "pending_owner":
                return decision
            expired = decision.model_copy(update={"status": "expired"})
            self.store.update_decision_status(decision_id, "expired")
            intent = self._intent_for(decision)
            self.chain.append(expired, intent, None)
            self._log("decision_expired", decision_id=decision_id)
            return expired

    def _intent_for(self, decision: Decision) -> PaymentIntent:
        intent = self.store.get_intent(decision.intent_id)
        if intent is None:
            raise ATLError(DECISION_NOT_FOUND, f"intent {decision.intent_id} missing")
        return intent

    def _latest_receipt(self, decision_id: str, *, with_transfer: bool = False) -> Receipt | None:
        receipts = self.store.receipts_for_decision(decision_id)
        if with_transfer:
            receipts = [r for r in receipts if r.transfer is not None]
        return receipts[-1] if receipts else None

    def get_decision(self, decision_id: str, wait_seconds: int = 0) -> dict:
        wait = max(0, min(int(wait_seconds or 0), MAX_WAIT_SECONDS))
        deadline = time.monotonic() + wait
        decision = self._refresh(decision_id)
        while decision.status in WAITING_STATUSES and time.monotonic() < deadline:
            self.sleep_fn(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
            decision = self._refresh(decision_id)
        return self._decision_view(decision)

    def _decision_view(self, decision: Decision) -> dict:
        transfer = self.store.get_transfer_for_decision(decision.decision_id)
        receipt = self._latest_receipt(decision.decision_id)
        return {
            "decision_id": decision.decision_id,
            "verdict": decision.verdict,
            "status": decision.status,
            "rule_hits": list(decision.rule_hits),
            "reasons": list(decision.reasons),
            "next_action": NEXT_ACTION_BY_STATUS[decision.status],
            "transfer_id": transfer.transfer_id if transfer else None,
            "receipt_id": receipt.receipt_id if receipt else None,
        }

    # -------------------------------------------------------------- approvals

    def approve(self, decision_id: str) -> dict:
        return self._owner_action(decision_id, "approve")

    def refuse(self, decision_id: str) -> dict:
        return self._owner_action(decision_id, "refuse")

    def _owner_action(self, decision_id: str, action: str) -> dict:
        with self._lock.hold():
            decision = self._refresh(decision_id)
            if decision.status != "pending_owner":
                raise ATLError(
                    DECISION_NOT_APPROVED,
                    f"decision is {decision.status}; only pending_owner decisions accept an owner action",
                )
            intent = self._intent_for(decision)
            mandate = self.store.get_mandate(intent.mandate_id)
            owner_id = mandate.owner_id if mandate else "owner"
            record = sign_approval(
                self.owner_key,
                decision_id=decision_id,
                action=action,  # type: ignore[arg-type]
                owner_id=owner_id,
                created_at=self.now_fn(),
            )
            self.store.save_approval(record)
            status = "approved" if action == "approve" else "refused"
            self.store.update_decision_status(decision_id, status)
            updated = decision.model_copy(update={"status": status})
            receipt = self.chain.append(updated, intent, None)
            self._log("owner_approval", decision_id=decision_id, action=action, owner_id=owner_id)
        out = updated.model_dump(mode="json")
        out["receipt_id"] = receipt.receipt_id
        out["next_action"] = NEXT_ACTION_BY_STATUS[status]
        return out

    # -------------------------------------------------------------- execution

    def _executed_view(self, decision_id: str) -> dict:
        transfer = self.store.get_transfer_for_decision(decision_id)
        receipt = self._latest_receipt(decision_id, with_transfer=True)
        return {
            "decision_id": decision_id,
            "status": "executed",
            "transfer_id": transfer.transfer_id if transfer else None,
            "receipt_id": receipt.receipt_id if receipt else None,
            "rail": transfer.rail if transfer else None,
            "amount_cents": transfer.amount_cents if transfer else None,
            "rail_status": transfer.status if transfer else None,
        }

    def _not_approved(self, decision: Decision) -> ATLError:
        self._log(
            "execute_refused", decision_id=decision.decision_id, status=decision.status, rail_called=False
        )
        return ATLError(DECISION_NOT_APPROVED, f"decision is {decision.status}; no payment was made")

    def _today_total_cents(self, rail_name: str) -> int:
        start = sao_paulo_day_start(self.now_fn())
        total = 0
        for r in self.store.list_receipts():
            t = r.transfer
            if t is not None and t.rail == rail_name and t.created_at >= start:
                total += t.amount_cents
        return total

    def _production_checks(self, intent: PaymentIntent, payee_name: str, pix_key: str) -> None:
        if not self.prod_allowed_pix_key or pix_key != self.prod_allowed_pix_key:
            raise ATLError(PRODUCTION_BLOCKED, "destination is not the allowlisted production key")
        if intent.amount_cents > PROD_MAX_PER_PAYMENT_CENTS:
            raise ATLError(PRODUCTION_CAP_EXCEEDED, f"above {PROD_MAX_PER_PAYMENT_CENTS} cents per payment")
        today = self._today_total_cents(self.rail.name)
        if today + intent.amount_cents > PROD_MAX_PER_DAY_CENTS:
            raise ATLError(PRODUCTION_CAP_EXCEEDED, f"above {PROD_MAX_PER_DAY_CENTS} cents per day")

    def _confirm(self, payee_name: str, pix_key: str, amount_cents: int) -> None:
        label = "PRODUCTION" if self.settings.env == "production" else "PRODUCTION (dry run on sandbox rail)"
        self.output_fn(
            f"{label} payment: payee {payee_name}, key {mask_key(pix_key)}, amount {format_brl(amount_cents)}"
        )
        self.output_fn("Type PAY to confirm, anything else cancels:")
        answer = (self.input_fn() or "").strip()
        if answer != "PAY":
            self._log("production_confirmation", confirmed=False)
            raise ATLError(PRODUCTION_BLOCKED, "owner did not type PAY; no payment was made")
        self._log("production_confirmation", confirmed=True)

    def _pending_view(self, decision_id: str) -> dict:
        """Agent-facing view of a payment the rail holds. The confirmation URL is for the owner only."""
        transfer = self.store.get_transfer_for_decision(decision_id)
        receipt = self._latest_receipt(decision_id, with_transfer=True)
        return {
            "decision_id": decision_id,
            "status": "pending_rail_confirmation",
            "transfer_id": transfer.transfer_id if transfer else None,
            "receipt_id": receipt.receipt_id if receipt else None,
            "rail": transfer.rail if transfer else None,
            "amount_cents": transfer.amount_cents if transfer else None,
            "rail_status": transfer.status if transfer else None,
            "next_action": "wait_for_owner",
            "message": "The rail holds this payment for the account owner's confirmation. Do not retry; "
            "the owner confirms it, then get_decision shows executed.",
        }

    def _notify_rail_confirmation(self, decision_id: str, transfer: RailTransfer) -> None:
        url = transfer.confirmation_url or "(Stripe returned no URL; open the Dashboard approvals page)"
        self._log(
            "rail_confirmation_required",
            decision_id=decision_id,
            transfer_id=transfer.transfer_id,
            confirmation_url=transfer.confirmation_url,
        )
        self.output_fn(
            f"Stripe holds payment {decision_id} ({transfer.amount_cents} {transfer.currency} cents to "
            f"{transfer.destination}) for your confirmation.\n"
            f"  1. Approve it in the Stripe Dashboard: {url}\n"
            f"  2. Then run: atl stripe confirm {decision_id} [--token APPROVAL_TOKEN]"
        )

    def _pay(self, decision_id: str, intent: PaymentIntent, mandate: Mandate, payee: Any) -> RailTransfer:
        if payee.destination_kind == "pix":
            return self.rail.transfer_pix(
                pix_key=payee.pix_key,
                pix_key_type=payee.pix_key_type,
                amount_cents=intent.amount_cents,
                idempotency_key=decision_id,
                description=f"ATL {intent.invoice_ref}",
            )
        pay = getattr(self.rail, "pay_destination", None)
        if pay is None:
            raise ATLError(DECISION_NOT_APPROVED, f"rail {self.rail.name} cannot pay Stripe destinations")
        return pay(
            payee=payee,
            amount_cents=intent.amount_cents,
            currency=mandate.currency,
            idempotency_key=decision_id,
            description=f"ATL {intent.invoice_ref[:80]} {decision_id}",
            metadata={
                "atl_decision_id": decision_id,
                "atl_intent_id": intent.intent_id,
                "atl_invoice_ref": intent.invoice_ref[:200],
            },
        )

    def execute_payment(self, decision_id: str) -> dict:
        decision = self._refresh(decision_id)
        if decision.status == "executed":
            return self._executed_view(decision_id)
        if decision.status == "pending_rail_confirmation":
            return self._pending_view(decision_id)
        if decision.status != "approved":
            raise self._not_approved(decision)

        intent = self._intent_for(decision)
        mandate = self.store.get_mandate(intent.mandate_id)
        if mandate is None:
            raise ATLError(MANDATE_NOT_FOUND, intent.mandate_id)
        if mandate.revoked:
            raise self._not_approved(decision.model_copy(update={"status": "refused"}))
        payee = next((p for p in mandate.payees if p.payee_id == intent.payee_id), None)
        if payee is None or intent.amount_cents <= 0:
            raise ATLError(DECISION_NOT_APPROVED, "intent key or amount does not match the mandate payee")
        if payee.destination_kind == "pix":
            if payee.pix_key != intent.pix_key:
                raise ATLError(DECISION_NOT_APPROVED, "intent key or amount does not match the mandate payee")
        elif intent.requested_destination.strip() not in payee.destinations():
            raise ATLError(DECISION_NOT_APPROVED, "intent destination does not match the mandate payee")
        rail_cur = RAIL_CURRENCY.get(self.rail.name, "BRL")
        if mandate.currency != rail_cur or intent.currency not in (None, mandate.currency):
            raise ATLError(
                DECISION_NOT_APPROVED,
                f"mandate currency {mandate.currency} does not match rail {self.rail.name} ({rail_cur}); "
                "no payment was made",
            )

        if self.production_guards:
            self._production_checks(intent, payee.display_name, payee.pix_key)
        if self.require_confirmation:
            self._confirm(payee.display_name, payee.pix_key, intent.amount_cents)

        with self._lock.hold():
            decision = self._refresh(decision_id)
            if decision.status == "executed":
                return self._executed_view(decision_id)
            if decision.status == "pending_rail_confirmation":
                return self._pending_view(decision_id)
            if decision.status != "approved":
                raise self._not_approved(decision)
            transfer: RailTransfer = self._pay(decision_id, intent, mandate, payee)
            self.store.save_transfer(decision_id, transfer)
            if transfer.status == PENDING_RAIL_CONFIRMATION:
                self.store.update_decision_status(decision_id, "pending_rail_confirmation")
                held = decision.model_copy(update={"status": "pending_rail_confirmation"})
                self.chain.append(held, intent, transfer)
                self._notify_rail_confirmation(decision_id, transfer)
                return self._pending_view(decision_id)
            self.store.update_decision_status(decision_id, "executed")
            executed = decision.model_copy(update={"status": "executed"})
            receipt = self.chain.append(executed, intent, transfer)
        return {
            "decision_id": decision_id,
            "status": "executed",
            "transfer_id": transfer.transfer_id,
            "receipt_id": receipt.receipt_id,
            "rail": transfer.rail,
            "amount_cents": transfer.amount_cents,
            # PENDING on Asaas: the owner still has to authorize the transfer with the critical-action token.
            "rail_status": transfer.status,
        }

    # ------------------------------------------------------- rail confirmation (owner)

    def confirm_rail_payment(self, decision_id: str, approval_token: str | None = None) -> dict:
        """Owner action after approving the held payment in the Stripe Dashboard: retry it through the rail.
        The decision becomes executed only when Stripe now reports the payment; otherwise it stays pending."""
        with self._lock.hold():
            decision = self._load(decision_id)
            if decision.status == "executed":
                return self._executed_view(decision_id)
            if decision.status != "pending_rail_confirmation":
                raise ATLError(
                    DECISION_NOT_APPROVED, f"decision is {decision.status}, not pending rail confirmation"
                )
            intent = self._intent_for(decision)
            mandate = self.store.get_mandate(intent.mandate_id)
            if mandate is None or mandate.revoked:
                raise ATLError(DECISION_NOT_APPROVED, "mandate is revoked or missing; not confirming")
            confirm = getattr(self.rail, "confirm_payment", None)
            if confirm is None:
                raise ATLError(DECISION_NOT_APPROVED, f"rail {self.rail.name} has no confirmation step")
            transfer: RailTransfer = confirm(decision_id, approval_token)
            self.store.save_transfer(decision_id, transfer)
            if transfer.status == PENDING_RAIL_CONFIRMATION:
                self._log("rail_confirmation_still_pending", decision_id=decision_id)
                out = self._pending_view(decision_id)
                out["confirmation_url"] = transfer.confirmation_url  # owner-facing call
                return out
            self.store.update_decision_status(decision_id, "executed")
            executed = decision.model_copy(update={"status": "executed"})
            receipt = self.chain.append(executed, intent, transfer)
            self._log("rail_confirmation_done", decision_id=decision_id, transfer_id=transfer.transfer_id)
        return {
            "decision_id": decision_id,
            "status": "executed",
            "transfer_id": transfer.transfer_id,
            "receipt_id": receipt.receipt_id,
            "rail": transfer.rail,
            "amount_cents": transfer.amount_cents,
            "rail_status": transfer.status,
        }

    def abandon_rail_payment(self, decision_id: str) -> dict:
        """Owner action: give up a payment the rail still holds (for example the 24 h Stripe approval
        expired). Marks the decision expired; ATL will not send it again."""
        with self._lock.hold():
            decision = self._load(decision_id)
            if decision.status != "pending_rail_confirmation":
                raise ATLError(
                    DECISION_NOT_APPROVED, f"decision is {decision.status}, not pending rail confirmation"
                )
            abandon = getattr(self.rail, "abandon_payment", None)
            if abandon is not None:
                abandon(decision_id)
            intent = self._intent_for(decision)
            self.store.update_decision_status(decision_id, "expired")
            expired = decision.model_copy(update={"status": "expired"})
            receipt = self.chain.append(expired, intent, self.store.get_transfer_for_decision(decision_id))
            self._log("rail_confirmation_abandoned", decision_id=decision_id)
        out = expired.model_dump(mode="json")
        out["receipt_id"] = receipt.receipt_id
        return out

    def pending_rail_payments(self) -> list[dict]:
        pending = getattr(self.rail, "pending_confirmations", None)
        return list(pending()) if pending is not None else []

    def setup_stripe_recipient(
        self, payee_id: str, display_name: str, email: str, country: str = "US"
    ) -> dict:
        setup = getattr(self.rail, "setup_recipient", None)
        if setup is None:
            raise ATLError(INVALID_ARGUMENT, f"rail {self.rail.name} does not set up Stripe recipients")
        rec = setup(payee_id=payee_id, display_name=display_name, email=email, country=country)
        self._log("owner_stripe_recipient", payee_id=payee_id, recipient_id=rec.get("recipient_id"))
        return rec

    # ---------------------------------------------------------------- receipts

    def get_receipt(self, receipt_id: str) -> dict:
        receipt = self.store.get_receipt(receipt_id)
        if receipt is None:
            raise ATLError(RECEIPT_NOT_FOUND, receipt_id)
        return receipt.model_dump(mode="json")

    def verify_receipt(self, receipt_id: str | None = None, receipt: dict | None = None) -> dict:
        if receipt is not None:
            rec = Receipt.model_validate(receipt)
        elif receipt_id:
            found = self.store.get_receipt(receipt_id)
            if found is None:
                raise ATLError(RECEIPT_NOT_FOUND, receipt_id)
            rec = found
        else:
            raise ATLError(INVALID_ARGUMENT, "pass receipt_id or receipt")
        verify = self._verify_fn
        if verify is None:
            from atl.receipts import verify_receipt as verify
        result = verify(rec, expected_public_key=self.atl_public_key)
        return result.model_dump(mode="json")
