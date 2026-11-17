"""get_mandate: the read-only mandate view added after the HW9 cold-start run.

In that run a fresh agent had to guess the payee_id ("papelaria_central" instead of the registered
"papelaria-central") and ATL refused with R2. These tests pin the fix: the agent can read the exact payee_id,
destination and categories before pay_check, and the view never leaks another agent's mandate.
"""

from __future__ import annotations

import pytest
from l4_fakes import KEY_CF

from atl.contracts import MANDATE_NOT_FOUND, ATLError


def _code(fn, *a, **kw) -> str:
    with pytest.raises(ATLError) as e:
        fn(*a, **kw)
    return e.value.code


def test_view_lists_exact_payee_destination_and_categories(h):
    h.register()
    view = h.svc.get_mandate("mdt_demo", "procurement-01")
    assert view["mandate_id"] == "mdt_demo"
    assert view["currency"] == "BRL"
    assert view["payees"] == [
        {
            "payee_id": "concreto-forte",
            "display_name": "Concreto Forte",
            "categories": ["materials"],
            "pix_key": KEY_CF,
            "pix_key_type": "EVP",
        }
    ]
    assert (view["per_payment_cap_cents"], view["daily_cap_cents"], view["hard_cap_cents"]) == (3000, 8000, 10000)
    assert view["spent_today_cents"] == 0
    assert view["remaining_today_cents"] == 8000


def test_view_counts_todays_payments(h):
    h.register()
    dec = h.svc.pay_check(**h.intent(amount_cents=1200))
    assert dec["verdict"] == "approve"
    h.svc.execute_payment(dec["decision_id"])
    view = h.svc.get_mandate("mdt_demo", "procurement-01")
    assert view["spent_today_cents"] == 1200
    assert view["remaining_today_cents"] == 6800


def test_other_agent_or_unknown_mandate_is_not_found(h):
    h.register()
    assert _code(h.svc.get_mandate, "mdt_demo", "someone-else") == MANDATE_NOT_FOUND
    assert _code(h.svc.get_mandate, "mdt_missing", "procurement-01") == MANDATE_NOT_FOUND


def test_view_is_read_only(h):
    h.register()
    before = h.store.get_mandate("mdt_demo")
    h.svc.get_mandate("mdt_demo", "procurement-01")
    assert h.store.get_mandate("mdt_demo") == before


def test_guessed_payee_refused_but_listed_payee_approved(h):
    """Regression for the HW9 friction: a guessed id is refused, the id read from get_mandate passes."""
    h.register()
    guessed = h.svc.pay_check(**h.intent(payee_id="concreto_forte", invoice_ref="NF-1", idempotency_key="NF-1"))
    assert guessed["verdict"] == "refuse"
    listed = h.svc.get_mandate("mdt_demo", "procurement-01")["payees"][0]
    ok = h.svc.pay_check(
        **h.intent(
            payee_id=listed["payee_id"],
            pix_key=listed["pix_key"],
            category=listed["categories"][0],
            invoice_ref="NF-2",
            idempotency_key="NF-2",
        )
    )
    assert ok["verdict"] == "approve"
