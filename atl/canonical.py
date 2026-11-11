"""Canonical JSON, hashing and Ed25519 helpers. Owned by L0, frozen with the contracts.

Canonical JSON: sorted keys, no spaces, UTF-8, non-ASCII kept as is.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel

ZERO_HASH = "0" * 64


def canonical_json(obj: Any) -> bytes:
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def mandate_signing_bytes(mandate: BaseModel) -> bytes:
    return canonical_json(mandate.model_dump(mode="json", exclude={"signature", "revoked"}))


def receipt_hash(receipt: BaseModel) -> str:
    return sha256_hex(canonical_json(receipt.model_dump(mode="json", exclude={"hash", "signature"})))


def approval_signing_bytes(decision_id: str, action: str, owner_id: str, created_at_iso: str) -> bytes:
    return canonical_json(
        {"action": action, "created_at": created_at_iso, "decision_id": decision_id, "owner_id": owner_id}
    )


def public_key_hex(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return raw.hex()


def sign_hex(private_key: Ed25519PrivateKey, data: bytes) -> str:
    return private_key.sign(data).hex()


def verify_hex(public_key_hex_str: str, data: bytes, signature_hex: str) -> bool:
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex_str))
        key.verify(bytes.fromhex(signature_hex), data)
        return True
    except (InvalidSignature, ValueError):
        return False
