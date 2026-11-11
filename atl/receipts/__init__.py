"""Signed, hash-chained receipts and key management (lane L3).

Each receipt embeds the decision, intent and transfer, links to the previous receipt by hash and is signed
with the ATL Ed25519 key over the ASCII hex hash. Anyone holding the receipts can verify them offline.

Public API (frozen):
    load_or_create_key(keys_dir: Path, name: Literal["atl", "owner"]) -> Ed25519PrivateKey
        PEM files in keys_dir (never committed; var/ is gitignored), mode 0600.
    class ReceiptChain(store: Store, atl_key: Ed25519PrivateKey):
        append(decision, intent, transfer) -> Receipt   # next seq, prev_hash, hash, signature
    verify_receipt(receipt: Receipt, expected_public_key: str | None = None) -> VerifyResult
        reasons use HASH_MISMATCH / BAD_SIGNATURE from atl.contracts
    verify_chain(receipts: list[Receipt]) -> list[str]
        empty list when the chain is intact; else codes like CHAIN_BROKEN with receipt ids
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atl.canonical import ZERO_HASH, public_key_hex, receipt_hash, sign_hex, verify_hex
from atl.contracts import (
    BAD_SIGNATURE,
    CHAIN_BROKEN,
    HASH_MISMATCH,
    Decision,
    PaymentIntent,
    RailTransfer,
    Receipt,
    Store,
    VerifyResult,
)
from atl.ids import new_id

_MAX_APPEND_ATTEMPTS = 5


def load_or_create_key(keys_dir: Path, name: Literal["atl", "owner"]) -> Ed25519PrivateKey:
    keys_dir = Path(keys_dir)
    keys_dir.mkdir(parents=True, exist_ok=True)
    path = keys_dir / f"{name}.pem"
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError(f"{path} is not an Ed25519 key")
        return key
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    os.chmod(path, 0o600)
    return key


class ReceiptChain:
    def __init__(self, store: Store, atl_key: Ed25519PrivateKey):
        self.store = store
        self.atl_key = atl_key

    def append(self, decision: Decision, intent: PaymentIntent, transfer: RailTransfer | None) -> Receipt:
        last_error: Exception | None = None
        for _ in range(_MAX_APPEND_ATTEMPTS):
            last = self.store.last_receipt()
            receipt = Receipt(
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
            receipt.hash = receipt_hash(receipt)
            receipt.signature = sign_hex(self.atl_key, receipt.hash.encode("ascii"))
            try:
                self.store.append_receipt(receipt)
            except sqlite3.IntegrityError as exc:
                last_error = exc
                continue
            return receipt
        raise RuntimeError("could not append receipt after seq conflicts") from last_error


def verify_receipt(receipt: Receipt, expected_public_key: str | None = None) -> VerifyResult:
    reasons: list[str] = []
    recomputed = receipt_hash(receipt)
    if recomputed != receipt.hash:
        reasons.append(HASH_MISMATCH)
    key_ok = expected_public_key is None or receipt.public_key == expected_public_key
    if not key_ok or not verify_hex(receipt.public_key, receipt.hash.encode("ascii"), receipt.signature):
        reasons.append(BAD_SIGNATURE)
    transfer = receipt.transfer
    return VerifyResult(
        valid=not reasons,
        reasons=reasons,
        receipt_id=receipt.receipt_id,
        payee_id=receipt.intent.payee_id,
        amount_cents=receipt.intent.amount_cents,
        transfer_id=transfer.transfer_id if transfer else None,
        rail=transfer.rail if transfer else None,
        verdict=receipt.decision.verdict,
        status=receipt.decision.status,
    )


def verify_chain(receipts: list[Receipt]) -> list[str]:
    problems: list[str] = []
    prev_hash = ZERO_HASH
    for index, receipt in enumerate(receipts):
        expected_seq = index + 1
        if receipt.seq != expected_seq:
            problems.append(f"{CHAIN_BROKEN} {receipt.receipt_id} seq {receipt.seq}")
        elif receipt.prev_hash != prev_hash:
            problems.append(f"{CHAIN_BROKEN} {receipt.receipt_id} seq {receipt.seq}")
        result = verify_receipt(receipt)
        for reason in result.reasons:
            problems.append(f"{reason} {receipt.receipt_id} seq {receipt.seq}")
        prev_hash = receipt.hash
    return problems
