"""Prefixed ids: mdt_, int_, dec_, rcp_, run_. Owned by L0."""

from __future__ import annotations

import secrets
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid() -> str:
    ms = int(time.time() * 1000)
    rand = secrets.randbits(80)
    value = (ms << 80) | rand
    out = []
    for _ in range(26):
        out.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def new_id(prefix: str) -> str:
    return f"{prefix}_{_ulid()}"
