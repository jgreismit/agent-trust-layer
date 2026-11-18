"""Frozen contracts shared by every lane (design.md section 3).

Owned by L0. Lanes may not edit this file; ask L0 through NOTES.md or BLOCKED.md.

Conventions:
- Money is integer cents (BRL). Never floats.
- Datetimes must be timezone aware; they are normalized to UTC on validation so canonical JSON is stable.

L0 additions beyond design section 3 (recorded in NOTES.md):
- Decision.original_decision_id (R5 must return the original decision id).
- ApprovalRecord, VerifyResult, ATLError, StoreView and the concrete Store protocol methods.

Stripe rail additions (stripe-rail branch, see docs/stripe-rail.md):
- Mandate.currency ("BRL" default, "USD" for the Stripe rail). Money is integer minor units of that currency.
- PayeeRule carries either a Pix key or a Stripe destination (stripe_recipient_id, stripe_account_id).
- PaymentIntent.destination and PaymentIntent.currency (optional), RailTransfer.destination, .currency and
  .confirmation_url (optional), decision status "pending_rail_confirmation", rule R11_CURRENCY_MISMATCH.
- Every new optional field is left out of the serialized form while it is unset (and Mandate.currency while
  it is "BRL"), so a Pix-only mandate, intent or transfer serializes byte for byte as before: existing owner
  signatures and receipt hashes keep verifying.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import AfterValidator, BaseModel, BeforeValidator, model_serializer, model_validator


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone aware")
    return value.astimezone(UTC)


def _upper(value: Any) -> Any:
    return value.strip().upper() if isinstance(value, str) else value


UtcDatetime = Annotated[datetime, AfterValidator(_to_utc)]

PixKeyType = Literal["EVP", "CPF", "CNPJ", "EMAIL", "PHONE"]
Currency = Annotated[Literal["BRL", "USD"], BeforeValidator(_upper)]
Verdict = Literal["approve", "escalate", "refuse"]
# pending_rail_confirmation: ATL approved and asked the rail to pay, and the rail (Stripe human confirmation)
# holds the payment until the account owner approves it in the rail's own dashboard.
DecisionStatus = Literal[
    "pending_owner", "approved", "refused", "executed", "expired", "pending_rail_confirmation"
]
RailName = Literal["asaas_sandbox", "asaas_production", "simulated", "stripe_sandbox"]
NextAction = Literal["call_execute_payment", "wait_for_owner", "do_not_pay_report_to_owner"]
DEFAULT_CURRENCY = "BRL"
RAIL_CURRENCY: dict[str, str] = {
    "asaas_sandbox": "BRL",
    "asaas_production": "BRL",
    "simulated": "BRL",
    "stripe_sandbox": "USD",
}


def _drop_unset(data: Any, keys: tuple[str, ...], defaults: dict[str, Any] | None = None) -> Any:
    """Remove optional extension fields while unset so older objects keep their canonical bytes."""
    if isinstance(data, dict):
        for k in keys:
            if k in data and data[k] is None:
                del data[k]
        for k, v in (defaults or {}).items():
            if k in data and data[k] == v:
                del data[k]
    return data

# Stable rule ids (design section 4).
R1_MANDATE_INVALID = "R1_MANDATE_INVALID"
R2_UNKNOWN_PAYEE = "R2_UNKNOWN_PAYEE"
R3_KEY_MISMATCH = "R3_KEY_MISMATCH"
R4_INJECTION_MARKER = "R4_INJECTION_MARKER"
R5_DUPLICATE = "R5_DUPLICATE"
R6_OVER_PER_PAYMENT_CAP = "R6_OVER_PER_PAYMENT_CAP"
R7_OVER_DAILY_CAP = "R7_OVER_DAILY_CAP"
R8_SPLIT_PATTERN = "R8_SPLIT_PATTERN"
R9_CATEGORY_NOT_ALLOWED = "R9_CATEGORY_NOT_ALLOWED"
R10_OVER_HARD_CAP = "R10_OVER_HARD_CAP"
R_PROD_DESTINATION_NOT_ALLOWED = "R_PROD_DESTINATION_NOT_ALLOWED"
# The intent's currency differs from the mandate's, or the mandate's currency is not the active rail's.
R11_CURRENCY_MISMATCH = "R11_CURRENCY_MISMATCH"

ALL_RULE_IDS = (
    R1_MANDATE_INVALID,
    R2_UNKNOWN_PAYEE,
    R9_CATEGORY_NOT_ALLOWED,
    R3_KEY_MISMATCH,
    R4_INJECTION_MARKER,
    R5_DUPLICATE,
    R10_OVER_HARD_CAP,
    R6_OVER_PER_PAYMENT_CAP,
    R7_OVER_DAILY_CAP,
    R8_SPLIT_PATTERN,
    R11_CURRENCY_MISMATCH,
)

ESCALATION_TTL_SECONDS = 15 * 60

# Stable error codes.
MANDATE_BAD_SIGNATURE = "MANDATE_BAD_SIGNATURE"
MANDATE_NOT_FOUND = "MANDATE_NOT_FOUND"
DECISION_NOT_FOUND = "DECISION_NOT_FOUND"
DECISION_NOT_APPROVED = "DECISION_NOT_APPROVED"
RECEIPT_NOT_FOUND = "RECEIPT_NOT_FOUND"
PRODUCTION_BLOCKED = "PRODUCTION_BLOCKED"
PROMOTION_REQUIRES_HUMAN = "PROMOTION_REQUIRES_HUMAN"
PROMOTION_BLOCKED_ACCEPTANCE = "PROMOTION_BLOCKED_ACCEPTANCE"
PRODUCTION_CAP_EXCEEDED = "PRODUCTION_CAP_EXCEEDED"
TRANSFER_NOT_FOUND = "TRANSFER_NOT_FOUND"
RAIL_ERROR = "RAIL_ERROR"
# ATL_RAIL names no known rail (a typo must never pick a real rail).
CONFIG_INVALID = "CONFIG_INVALID"
# The rail refused a second transfer with the same amount and key (Asaas 409 checkout.already.requested).
RAIL_DUPLICATE_TRANSFER = "RAIL_DUPLICATE_TRANSFER"
HASH_MISMATCH = "HASH_MISMATCH"
BAD_SIGNATURE = "BAD_SIGNATURE"
CHAIN_BROKEN = "CHAIN_BROKEN"


class ATLError(Exception):
    """Error with a stable code. MCP tools return {"error": code, "message": ...}."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


_PAYEE_OPTIONAL = ("pix_key", "pix_key_type", "stripe_recipient_id", "stripe_account_id")


class PayeeRule(BaseModel):
    """A registered payee. Exactly one kind of destination: a Pix key (BRL) or a Stripe destination (USD)."""

    payee_id: str  # e.g. "concreto-forte"
    display_name: str
    pix_key: str | None = None
    pix_key_type: PixKeyType | None = None
    # Stripe Treasury recipient (what the Stripe MCP `setup_recipient` returns; paid with `send_money`).
    stripe_recipient_id: str | None = None
    # Stripe Connect connected account (acct_...), paid by the REST fallback with POST /v1/transfers.
    stripe_account_id: str | None = None
    categories: list[str]

    @model_validator(mode="after")
    def _one_destination_kind(self) -> PayeeRule:
        pix = self.pix_key is not None or self.pix_key_type is not None
        stripe = bool(self.stripe_recipient_id or self.stripe_account_id)
        if pix and stripe:
            raise ValueError(f"payee {self.payee_id}: a Pix key and a Stripe destination cannot be combined")
        if pix and not (self.pix_key and self.pix_key_type):
            raise ValueError(f"payee {self.payee_id}: pix_key and pix_key_type are both required")
        if not pix and not stripe:
            raise ValueError(f"payee {self.payee_id}: needs a pix_key or a Stripe destination")
        return self

    @property
    def destination_kind(self) -> Literal["pix", "stripe"]:
        return "pix" if self.pix_key else "stripe"

    def destinations(self) -> list[str]:
        """Every destination the owner registered for this payee (exact strings)."""
        if self.destination_kind == "pix":
            return [self.pix_key or ""]
        return [d for d in (self.stripe_recipient_id, self.stripe_account_id) if d]

    @model_serializer(mode="wrap")
    def _ser(self, handler: Any) -> Any:
        return _drop_unset(handler(self), _PAYEE_OPTIONAL)


class Mandate(BaseModel):
    mandate_id: str  # mdt_...
    owner_id: str
    agent_id: str
    payees: list[PayeeRule]
    per_payment_cap_cents: int
    daily_cap_cents: int
    hard_cap_cents: int  # above this: refuse, never escalate
    valid_until: UtcDatetime
    revoked: bool = False
    # ISO 4217 code of every amount in this mandate and its intents.
    # BRL pays Pix payees, USD pays Stripe ones.
    currency: Currency = DEFAULT_CURRENCY
    # Ed25519 by owner key over canonical JSON of the mandate without `signature` and `revoked`
    # (revocation is owner state kept by the store). See atl.canonical.mandate_signing_bytes.
    signature: str

    @model_validator(mode="after")
    def _payees_match_currency(self) -> Mandate:
        want = "pix" if self.currency == "BRL" else "stripe"
        for p in self.payees:
            if p.destination_kind != want:
                raise ValueError(
                    f"payee {p.payee_id}: a {self.currency} mandate needs "
                    f"{'a Pix key' if want == 'pix' else 'a Stripe destination'}"
                )
        return self

    @model_serializer(mode="wrap")
    def _ser(self, handler: Any) -> Any:
        return _drop_unset(handler(self), (), {"currency": DEFAULT_CURRENCY})


class PaymentIntent(BaseModel):
    intent_id: str  # int_...
    mandate_id: str
    agent_id: str
    payee_id: str
    pix_key: str = ""  # Pix key the agent intends to pay (may differ from mandate); "" for Stripe payees
    amount_cents: int
    category: str
    invoice_ref: str
    memo: str  # free text copied from the invoice; untrusted
    idempotency_key: str
    # Non-Pix destination the agent intends to pay (a Stripe recipient or account id).
    # Untrusted, checked by R3.
    destination: str | None = None
    # Currency the agent believes it is paying in; checked by R11 against the mandate. None means "the
    # mandate's currency". Kept as free text (upper-cased) so an unexpected code is refused, not rejected.
    currency: Annotated[str | None, BeforeValidator(_upper)] = None

    @property
    def requested_destination(self) -> str:
        return self.destination if self.destination is not None else self.pix_key

    @model_serializer(mode="wrap")
    def _ser(self, handler: Any) -> Any:
        return _drop_unset(handler(self), ("destination", "currency"))


class Decision(BaseModel):
    decision_id: str  # dec_...
    intent_id: str
    verdict: Verdict
    rule_hits: list[str]  # e.g. ["R6_OVER_PER_PAYMENT_CAP"]
    reasons: list[str]  # human readable, one per hit
    status: DecisionStatus
    created_at: UtcDatetime
    original_decision_id: str | None = None  # set on R5_DUPLICATE


class RailTransfer(BaseModel):
    rail: RailName
    transfer_id: str
    status: str  # raw rail status
    amount_cents: int
    pix_key: str  # "" on non-Pix rails; see destination
    created_at: UtcDatetime
    fee_cents: int = 0
    destination: str | None = None  # Stripe recipient or connected account that was paid
    currency: str | None = None  # None means BRL (every pre-Stripe transfer)
    # Stripe human confirmation: the Dashboard URL where the account owner approves the held payment.
    confirmation_url: str | None = None

    @model_serializer(mode="wrap")
    def _ser(self, handler: Any) -> Any:
        return _drop_unset(handler(self), ("destination", "currency", "confirmation_url"))


class Receipt(BaseModel):
    receipt_id: str  # rcp_...
    seq: int
    prev_hash: str  # "0"*64 for the first
    decision: Decision
    intent: PaymentIntent
    transfer: RailTransfer | None
    # hash = sha256(canonical_json(every field except `hash` and `signature`)), hex.
    # This includes public_key (receipts spec). See atl.canonical.receipt_hash.
    hash: str
    # Ed25519 by the ATL key over the ASCII bytes of the hex `hash`, hex encoded.
    signature: str
    public_key: str  # hex of the raw 32 byte Ed25519 public key


class ApprovalRecord(BaseModel):
    decision_id: str
    action: Literal["approve", "refuse"]
    owner_id: str
    # Ed25519 by owner key over canonical JSON {"action", "decision_id", "owner_id", "created_at"}
    signature: str
    created_at: UtcDatetime


class VerifyResult(BaseModel):
    valid: bool
    reasons: list[str]  # empty when valid; else codes like HASH_MISMATCH, BAD_SIGNATURE
    receipt_id: str
    payee_id: str
    amount_cents: int
    transfer_id: str | None
    rail: RailName | None
    verdict: Verdict
    status: DecisionStatus


@runtime_checkable
class Rail(Protocol):
    name: RailName

    def balance_cents(self) -> int: ...

    def transfer_pix(
        self,
        *,
        pix_key: str,
        pix_key_type: str,
        amount_cents: int,
        idempotency_key: str,
        description: str,
    ) -> RailTransfer: ...

    def get_transfer(self, transfer_id: str) -> RailTransfer: ...


@runtime_checkable
class DestinationRail(Rail, Protocol):
    """A rail that pays registered non-Pix destinations (the Stripe rail). ATL calls it only after approval.

    pay_destination returns a RailTransfer whose status is PENDING_RAIL_CONFIRMATION when the rail holds the
    payment for its own human confirmation; confirm_payment retries it after the account owner approved.
    """

    currency: str

    def pay_destination(
        self,
        *,
        payee: PayeeRule,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        description: str,
        metadata: dict[str, str],
    ) -> RailTransfer: ...

    def confirm_payment(self, idempotency_key: str, approval_token: str | None = None) -> RailTransfer: ...


PENDING_RAIL_CONFIRMATION = "pending_stripe_confirmation"


class StoreView(Protocol):
    """Read-only queries the policy engine needs. Pure reads, no writes."""

    def find_by_idempotency_key(self, idempotency_key: str) -> tuple[PaymentIntent, Decision] | None: ...

    def find_by_invoice(self, payee_id: str, invoice_ref: str) -> list[tuple[PaymentIntent, Decision]]: ...

    def decisions_since(self, mandate_id: str, since: datetime) -> list[tuple[PaymentIntent, Decision]]:
        """All intents with their current decision under this mandate with decision created_at >= since."""
        ...


class Store(StoreView, Protocol):
    """SQLite implementation lives in atl/store (lane L3)."""

    def save_mandate(self, mandate: Mandate) -> None: ...

    def get_mandate(self, mandate_id: str) -> Mandate | None: ...

    def revoke_mandate(self, mandate_id: str) -> None: ...

    def save_intent(self, intent: PaymentIntent) -> None: ...

    def get_intent(self, intent_id: str) -> PaymentIntent | None: ...

    def save_decision(self, decision: Decision) -> None:
        """Insert, or replace the row with the same decision_id."""
        ...

    def get_decision(self, decision_id: str) -> Decision | None: ...

    def update_decision_status(self, decision_id: str, status: DecisionStatus) -> None: ...

    def save_approval(self, approval: ApprovalRecord) -> None: ...

    def get_approval(self, decision_id: str) -> ApprovalRecord | None: ...

    def save_transfer(self, decision_id: str, transfer: RailTransfer) -> None: ...

    def get_transfer_for_decision(self, decision_id: str) -> RailTransfer | None: ...

    def append_receipt(self, receipt: Receipt) -> None: ...

    def last_receipt(self) -> Receipt | None: ...

    def get_receipt(self, receipt_id: str) -> Receipt | None: ...

    def list_receipts(self) -> list[Receipt]:
        """Ordered by seq ascending."""
        ...

    def receipts_for_decision(self, decision_id: str) -> list[Receipt]: ...
