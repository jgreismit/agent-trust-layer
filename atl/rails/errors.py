"""Rail error type, kept in its own module so rail implementations can import it without cycles."""

from __future__ import annotations

from atl.contracts import ATLError


class RailError(ATLError):
    pass
