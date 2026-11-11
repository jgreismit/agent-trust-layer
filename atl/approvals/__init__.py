"""Owner approvals and production promotion guards (lane L4).

- sign_approval / verify_approval: Ed25519 owner signature over atl.canonical.approval_signing_bytes.
- Production hard limits and helpers used by ATLService.execute_payment and `atl demo --production-single`.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atl.canonical import approval_signing_bytes, sign_hex, verify_hex
from atl.contracts import ApprovalRecord

PROD_MAX_PER_PAYMENT_CENTS = 500
PROD_MAX_PER_DAY_CENTS = 1000
SAO_PAULO = ZoneInfo("America/Sao_Paulo")


def _created_at_iso(record: ApprovalRecord) -> str:
    return record.model_dump(mode="json")["created_at"]


def sign_approval(
    owner_key: Ed25519PrivateKey,
    *,
    decision_id: str,
    action: Literal["approve", "refuse"],
    owner_id: str,
    created_at: datetime,
) -> ApprovalRecord:
    record = ApprovalRecord(
        decision_id=decision_id, action=action, owner_id=owner_id, signature="", created_at=created_at
    )
    payload = approval_signing_bytes(decision_id, action, owner_id, _created_at_iso(record))
    return record.model_copy(update={"signature": sign_hex(owner_key, payload)})


def verify_approval(record: ApprovalRecord, owner_public_key: str) -> bool:
    payload = approval_signing_bytes(
        record.decision_id, record.action, record.owner_id, _created_at_iso(record)
    )
    return verify_hex(owner_public_key, payload, record.signature)


def mask_key(key: str) -> str:
    """Show only enough of a Pix key for a human to recognize it."""
    if "@" in key:
        local, _, domain = key.partition("@")
        return f"{local[:2]}***@{domain}"
    if len(key) <= 6:
        return "***"
    return f"{key[:3]}***{key[-3:]}"


def format_brl(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}R$ {cents // 100}.{cents % 100:02d}"


def sao_paulo_day_start(now: datetime) -> datetime:
    """Midnight America/Sao_Paulo of the day containing `now`, as an aware datetime."""
    local = now.astimezone(SAO_PAULO)
    start = datetime.combine(local.date(), time(0, 0), tzinfo=SAO_PAULO)
    return start if start <= local else start - timedelta(days=1)
