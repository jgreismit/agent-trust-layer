"""Regression tests for the independent tester's findings (HW9, 2026-11-17).

1. Her agent ended its turn about 2 minutes into a 15-minute escalation. Every decision now carries a plain
   next_step sentence, and an escalation carries expires_at, so the agent knows to keep waiting and until when.
2. With ATL_RAIL unset, ATL used to pick the Asaas rail. A missing setting now means the simulated rail and an
   unknown value is refused, so a typo can never reach a real rail.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from atl.config import _rail_kind, load_settings
from atl.contracts import CONFIG_INVALID, ESCALATION_TTL_SECONDS, ATLError


def test_escalation_says_keep_waiting_and_until_when(h):
    h.register()
    r = h.svc.pay_check(**h.intent(amount_cents=4000))  # above the 3000 per-payment cap: escalate
    assert r["next_action"] == "wait_for_owner"
    assert "Do not end your turn" in r["next_step"] and "get_decision" in r["next_step"]
    expires = datetime.fromisoformat(r["expires_at"])
    view = h.svc.get_decision(r["decision_id"])
    assert view["expires_at"] == r["expires_at"]
    assert "Do not end your turn" in view["next_step"]
    created = h.store.get_decision(r["decision_id"]).created_at
    assert expires - created == timedelta(seconds=ESCALATION_TTL_SECONDS)


def test_after_owner_approval_next_step_says_execute_and_no_expiry(h):
    h.register()
    r = h.svc.pay_check(**h.intent(amount_cents=4000))
    h.svc.approve(r["decision_id"])
    view = h.svc.get_decision(r["decision_id"])
    assert view["next_action"] == "call_execute_payment"
    assert "execute_payment" in view["next_step"] and "does not expire" in view["next_step"]
    assert "expires_at" not in view


def test_approve_and_refuse_have_a_next_step(h):
    h.register()
    ok = h.svc.pay_check(**h.intent())
    assert ok["next_step"].startswith("Approved")
    bad = h.svc.pay_check(**h.intent(payee_id="someone-else"))
    assert "Do not pay" in bad["next_step"]


@pytest.mark.parametrize("value", ["", "  ", "simulated", "SIMULATED"])
def test_missing_rail_is_simulated(value):
    assert _rail_kind(value) == "simulated"


def test_asaas_and_stripe_need_their_names():
    assert _rail_kind("asaas") == "asaas"
    assert _rail_kind("stripe_sandbox") == "stripe"


@pytest.mark.parametrize("value", ["simulted", "pix", "live", "production"])
def test_unknown_rail_is_refused(value):
    with pytest.raises(ATLError) as e:
        _rail_kind(value)
    assert e.value.code == CONFIG_INVALID


def test_load_settings_without_atl_rail_is_simulated(monkeypatch, tmp_path):
    monkeypatch.delenv("ATL_RAIL", raising=False)
    monkeypatch.setenv("ATL_VAR_DIR", str(tmp_path))
    assert load_settings(stdin_isatty=False).rail == "simulated"
