"""Rail error type, kept in its own module so rail implementations can import it without cycles."""

from __future__ import annotations

from atl.contracts import ATLError


class RailError(ATLError):
    pass


class RailOutcomeUnknown(RailError):
    """The rail may or may not have moved money (timeout after sending, unreadable answer).

    Code RAIL_ERROR. The Stripe rail records the attempt as uncertain and never sends again for the same
    idempotency key until a lookup finds the payment.
    """
