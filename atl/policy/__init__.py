"""Deterministic policy engine (lane L2).

Public API (frozen):
    evaluate(intent, mandate, view, now, *, owner_public_key) -> Decision

- `mandate` is None when the mandate id is unknown (R1).
- `owner_public_key` is the hex owner key used to check the mandate signature (R1).
- Verdict to status: approve -> approved, escalate -> pending_owner, refuse -> refused.
- All hits are reported; refuse wins over escalate. On R5, `original_decision_id` is set.
- Pure: no writes, no clock reads (use `now`), no randomness except the new decision id.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from atl.canonical import mandate_signing_bytes, verify_hex
from atl.contracts import (
    R1_MANDATE_INVALID,
    R2_UNKNOWN_PAYEE,
    R3_KEY_MISMATCH,
    R4_INJECTION_MARKER,
    R5_DUPLICATE,
    R6_OVER_PER_PAYMENT_CAP,
    R7_OVER_DAILY_CAP,
    R8_SPLIT_PATTERN,
    R9_CATEGORY_NOT_ALLOWED,
    R10_OVER_HARD_CAP,
    Decision,
    Mandate,
    PaymentIntent,
    StoreView,
    Verdict,
)
from atl.ids import new_id
from atl.policy.markers import find_markers
from atl.policy.split import declared_part, order_ref, same_order_possible

SAO_PAULO = ZoneInfo("America/Sao_Paulo")

_REFUSE_RULES = {
    R1_MANDATE_INVALID,
    R2_UNKNOWN_PAYEE,
    R9_CATEGORY_NOT_ALLOWED,
    R3_KEY_MISMATCH,
    R5_DUPLICATE,
    R10_OVER_HARD_CAP,
}
_STATUS = {"approve": "approved", "escalate": "pending_owner", "refuse": "refused"}
_COUNTS_FOR_DAY = {"approved", "executed"}
_COUNTS_FOR_SPLIT = {"approved", "executed", "pending_owner"}


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}R$ {cents // 100}.{cents % 100:02d}"


def _day_start(now: datetime) -> datetime:
    local = now.astimezone(SAO_PAULO)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _mandate_problem(
    intent: PaymentIntent, mandate: Mandate | None, now: datetime, owner_public_key: str
) -> str | None:
    if mandate is None:
        return "mandate not found"
    if mandate.revoked:
        return "mandate is revoked"
    if now > mandate.valid_until:
        return "mandate has expired"
    if intent.mandate_id != mandate.mandate_id:
        return "intent mandate_id does not match the mandate"
    if intent.agent_id != mandate.agent_id:
        return "intent agent_id does not match the mandate"
    if not verify_hex(owner_public_key, mandate_signing_bytes(mandate), mandate.signature):
        return "mandate signature is invalid"
    return None


def _same_key(registered: str, given: str, key_type: str) -> bool:
    a, b = registered.strip(), given.strip()
    if key_type == "EMAIL":
        return a.lower() == b.lower()
    return a == b


def evaluate(
    intent: PaymentIntent,
    mandate: Mandate | None,
    view: StoreView,
    now: datetime,
    *,
    owner_public_key: str,
) -> Decision:
    hits: list[str] = []
    reasons: list[str] = []
    original: str | None = None

    def hit(rule: str, reason: str) -> None:
        hits.append(rule)
        reasons.append(reason)

    problem = _mandate_problem(intent, mandate, now, owner_public_key)
    if problem is not None or mandate is None:
        hit(R1_MANDATE_INVALID, f"Mandate invalid: {problem}")
        return _decision(intent, now, hits, reasons, None)

    amount = intent.amount_cents

    # G2 payee identity
    payee = next((p for p in mandate.payees if p.payee_id == intent.payee_id), None)
    if payee is None:
        hit(R2_UNKNOWN_PAYEE, f"Payee {intent.payee_id} is not in the mandate")
    else:
        if intent.category not in payee.categories:
            hit(
                R9_CATEGORY_NOT_ALLOWED,
                f"Category {intent.category} is not allowed for payee {payee.payee_id}",
            )
        if not _same_key(payee.pix_key, intent.pix_key, payee.pix_key_type):
            hit(R3_KEY_MISMATCH, f"Pix key differs from the registered key of {payee.payee_id}")

    # G3 untrusted content
    markers = find_markers(intent.memo)
    if markers:
        hit(R4_INJECTION_MARKER, f"Memo contains instruction-like text or a payment key: {markers[0]!r}")

    # G4 integrity
    prior = view.find_by_idempotency_key(intent.idempotency_key)
    if prior is not None:
        original = prior[1].decision_id
        hit(R5_DUPLICATE, f"Idempotency key already used by decision {original}")
    else:
        for _, dec in view.find_by_invoice(intent.payee_id, intent.invoice_ref):
            if dec.status in _COUNTS_FOR_DAY:
                original = dec.decision_id
                hit(
                    R5_DUPLICATE,
                    f"Invoice {intent.invoice_ref} was already {dec.status} (decision {original})",
                )
                break

    # G5 amount limits
    if amount > mandate.hard_cap_cents:
        hit(R10_OVER_HARD_CAP, f"{money(amount)} is above the hard cap of {money(mandate.hard_cap_cents)}")
    elif amount > mandate.per_payment_cap_cents:
        hit(
            R6_OVER_PER_PAYMENT_CAP,
            f"{money(amount)} is above the per-payment cap of {money(mandate.per_payment_cap_cents)}",
        )

    day_total = sum(
        i.amount_cents
        for i, d in view.decisions_since(mandate.mandate_id, _day_start(now))
        if d.status in _COUNTS_FOR_DAY
    )
    if day_total + amount > mandate.daily_cap_cents:
        hit(
            R7_OVER_DAILY_CAP,
            f"Today's total {money(day_total)} plus {money(amount)} is above the daily cap "
            f"of {money(mandate.daily_cap_cents)}",
        )

    if amount <= mandate.per_payment_cap_cents:
        cap = mandate.per_payment_cap_cents
        # HW7 v2: a declared part of an order above the cap is a split even when it arrives first, and an
        # earlier payment for a different named order is not part of this one (atl/policy/split.py).
        part = declared_part(intent.memo)
        order = order_ref(intent.memo)
        since = now - timedelta(hours=24)
        recent = sum(
            i.amount_cents
            for i, d in view.decisions_since(mandate.mandate_id, since)
            if i.payee_id == intent.payee_id
            and i.intent_id != intent.intent_id
            and i.amount_cents <= cap
            and d.status in _COUNTS_FOR_SPLIT
            and d.created_at >= since
            and same_order_possible(order, order_ref(i.memo))
        )
        order_total = (part.total_cents or part.parts * amount) if part else 0
        if order_total > cap:
            hit(
                R8_SPLIT_PATTERN,
                f"Invoice is one of {part.parts} parts of an order totalling {money(order_total)}, "  # type: ignore[union-attr]
                f"above the per-payment cap of {money(cap)}",
            )
        elif recent + amount > cap:
            hit(
                R8_SPLIT_PATTERN,
                f"{money(recent)} already sent to {intent.payee_id} in 24 h plus {money(amount)} "
                f"is above the per-payment cap of {money(cap)}",
            )

    return _decision(intent, now, hits, reasons, original)


def _decision(
    intent: PaymentIntent, now: datetime, hits: list[str], reasons: list[str], original: str | None
) -> Decision:
    verdict: Verdict
    if not hits:
        verdict = "approve"
    elif any(h in _REFUSE_RULES for h in hits):
        verdict = "refuse"
    else:
        verdict = "escalate"
    return Decision(
        decision_id=new_id("dec"),
        intent_id=intent.intent_id,
        verdict=verdict,
        rule_hits=hits,
        reasons=reasons,
        status=_STATUS[verdict],  # type: ignore[arg-type]
        created_at=now,
        original_decision_id=original if R5_DUPLICATE in hits else None,
    )
