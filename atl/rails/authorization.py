"""Owner authorization of rail transfers (Asaas critical-action token).

Asaas holds every transfer created by API as PENDING with `authorized: false` until the account owner confirms
it with the critical-action token: an SMS or app code in production, `000000` in the sandbox web UI. ATL never
holds or sends that token, so the owner keeps a second factor that neither ATL nor the agent can bypass. This
module only tells whether a transfer still waits for the owner and watches the rail until the owner acts.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable

from atl.contracts import RailTransfer

AWAITING_STATUS = "PENDING"
AUTHORIZED_PENDING = "PENDING (authorized)"
DEFAULT_POLL_SECONDS = 5.0


def awaiting_owner(transfer: RailTransfer) -> bool:
    """True when the rail holds this transfer for the owner's critical-action token."""
    return transfer.rail != "simulated" and transfer.status == AWAITING_STATUS


def owner_instruction(rail_name: str) -> str:
    if rail_name == "asaas_production":
        return "Authorize it in the Asaas app with your SMS or app token. ATL never sees the token."
    return "Authorize it in the Asaas sandbox web UI (Transferencias) with the sandbox token 000000."


def wait_for_authorization(
    rail,
    transfer_ids: Iterable[str],
    timeout_s: float,
    *,
    poll_s: float = DEFAULT_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, str]:
    """Poll the rail until every transfer is authorized or `timeout_s` passes. Returns transfer id -> state.

    A transfer is settled when its status leaves PENDING, or when the rail reports `authorized` (Asaas keeps
    an authorized transfer PENDING until it is processed); that state reads `PENDING (authorized)`.
    Read only: it never authorizes, cancels or retries anything.
    """
    deadline = clock() + max(0.0, timeout_s)
    waiting = list(transfer_ids)
    statuses: dict[str, str] = {}
    authorized = getattr(rail, "transfer_authorized", None)
    while True:
        for tid in list(waiting):
            statuses[tid] = rail.get_transfer(tid).status
            if statuses[tid] == AWAITING_STATUS and authorized is not None and authorized(tid):
                statuses[tid] = AUTHORIZED_PENDING
            if statuses[tid] != AWAITING_STATUS:
                waiting.remove(tid)
        remaining = deadline - clock()
        if not waiting or remaining <= 0:
            return statuses
        sleep(min(poll_s, remaining))
