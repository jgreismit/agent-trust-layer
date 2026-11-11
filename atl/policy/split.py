"""R8 helpers: read split-purchase evidence from an untrusted memo.

HW7 v2 change (evidence: benchmark tuning cases T07, F07 and F02, see docs/hw7/). R8 used to look only at the
money already sent to the same payee in 24 h, so it missed the first installment of a split order (nothing
sent yet) and flagged two separate orders from the same supplier on one day. It now also uses two facts a
memo often states:

- a declared part ("parcela 1 de 3", "lote A de 2", "installment 1 of 3", "parte 1/2") and, when given, the
  order total. A part of an order whose total is above the per-payment cap is a split even when it is the
  first invoice to arrive.
- an order reference ("pedido 4510", "order PO-330"). Earlier payments to the same payee count toward the
  split total unless both payments name an order and the orders differ.

The memo is untrusted. Reading it can only add an R8 hit (declared part) or leave an earlier payment out of
the running total when both memos name different orders, and the per-payment and daily caps still apply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from atl.policy.markers import normalize

_PART_WORD = r"(?:parcela|parte|lote|part|installment|tranche)"
_PART_OF_RE = re.compile(_PART_WORD + r"\s+([a-z0-9]{1,3})\s+(?:de|of)\s+(\d{1,2})\b")
_PART_SLASH_RE = re.compile(_PART_WORD + r"\s+(\d{1,2})\s*/\s*(\d{1,2})\b")
_TOTAL_RE = re.compile(r"\btotal\b[^0-9]{0,12}?(?:r\$\s*)?(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})|\d+)")
_ORDER_RE = re.compile(r"\b(?:pedido|order|ordem|po|ped)\b\s*(?:n[o.]?\s*|#|:)?\s*([a-z]{0,4}-?\d{2,})\b")


@dataclass(frozen=True)
class PartInfo:
    parts: int
    total_cents: int | None


def _cents(raw: str) -> int:
    """'75,00', '84.00', '1.234,56', '52' -> cents."""
    if len(raw) > 3 and raw[-3] in ",.":
        whole, frac = raw[:-3], raw[-2:]
    else:
        whole, frac = raw, "00"
    return int(re.sub(r"[.,]", "", whole) or "0") * 100 + int(frac)


def declared_part(memo: str) -> PartInfo | None:
    """The 'part k of n' statement in a memo, with the order total when the memo gives one."""
    text = normalize(memo)
    m = _PART_OF_RE.search(text) or _PART_SLASH_RE.search(text)
    if m is None:
        return None
    parts = int(m.group(2))
    if parts < 2:
        return None
    total = _TOTAL_RE.search(text)
    return PartInfo(parts=parts, total_cents=_cents(total.group(1)) if total else None)


def order_ref(memo: str) -> str | None:
    """The order the memo names ('pedido 4510' -> '4510', 'order PO-330' -> 'po-330'), or None."""
    m = _ORDER_RE.search(normalize(memo))
    return m.group(1) if m else None


def same_order_possible(a: str | None, b: str | None) -> bool:
    """False only when both memos name an order and the orders differ."""
    return a is None or b is None or a == b
