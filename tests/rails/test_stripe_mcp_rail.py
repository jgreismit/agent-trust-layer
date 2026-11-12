"""Offline tests for StripeMCPRail with a fake Stripe MCP transport. No network, no key."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stripe_fakes import (  # noqa: E402
    CONFIRM_URL,
    RECIPIENT,
    TEST_AGENT_KEY,
    FakeStripeMCP,
    err,
    obp,
    ok,
    stripe_settings,
)

from atl.contracts import (  # noqa: E402
    PENDING_RAIL_CONFIRMATION,
    PRODUCTION_BLOCKED,
    RAIL_ERROR,
    TRANSFER_NOT_FOUND,
    ATLError,
    DestinationRail,
    PayeeRule,
    Rail,
)
from atl.rails import get_rail, rail_currency  # noqa: E402
from atl.rails.errors import RailError, RailOutcomeUnknown  # noqa: E402
from atl.rails.stripe_mcp import MCPStripeTransport, StripeMCPRail, map_transport_error  # noqa: E402
from atl.runlog import RunLog  # noqa: E402

PAYEE = PayeeRule(
    payee_id="acme-supply",
    display_name="Acme Supply Co",
    stripe_recipient_id=RECIPIENT,
    categories=["materials"],
)


def make_rail(tmp_path, fake=None, **over):
    fake = fake or FakeStripeMCP()
    rail = StripeMCPRail(
        stripe_settings(tmp_path, **over), RunLog("run_test", tmp_path / "runs"), transport=fake
    )
    return rail, fake


def pay(rail, key="dec_1", amount=4200, **over):
    args = dict(
        payee=PAYEE,
        amount_cents=amount,
        currency="USD",
        idempotency_key=key,
        description="ATL INV-1 dec_1",
        metadata={"atl_decision_id": key, "atl_intent_id": "int_1"},
    )
    args.update(over)
    return rail.pay_destination(**args)


# ------------------------------------------------------------------ selection and guards


def test_get_rail_selects_stripe_and_protocols(tmp_path):
    settings = stripe_settings(tmp_path)
    rail = get_rail(settings)
    assert isinstance(rail, StripeMCPRail) and rail.name == "stripe_sandbox"
    assert isinstance(rail, Rail) and isinstance(rail, DestinationRail)
    assert rail_currency(settings) == "USD"


@pytest.mark.parametrize("key", ["rk_live_" + "x" * 30, "sk_live_" + "x" * 30, "ak_live_abc"])
def test_live_agent_key_refused_before_io(tmp_path, key):
    fake = FakeStripeMCP()
    with pytest.raises(ATLError) as exc:
        StripeMCPRail(stripe_settings(tmp_path, stripe_agent_key=key), transport=fake)
    assert exc.value.code == PRODUCTION_BLOCKED
    assert key not in str(exc.value)
    assert fake.calls == [] and fake.list_calls == 0


def test_unrecognized_key_mode_refused(tmp_path):
    with pytest.raises(ATLError) as exc:
        StripeMCPRail(
            stripe_settings(tmp_path, stripe_agent_key="mystery_key_123"), transport=FakeStripeMCP()
        )
    assert exc.value.code == PRODUCTION_BLOCKED


def test_production_env_refused(tmp_path):
    with pytest.raises(ATLError) as exc:
        StripeMCPRail(stripe_settings(tmp_path, env="production"), transport=FakeStripeMCP())
    assert exc.value.code == PRODUCTION_BLOCKED


def test_non_stripe_host_refused(tmp_path):
    with pytest.raises(ATLError) as exc:
        StripeMCPRail(
            stripe_settings(tmp_path, stripe_mcp_url="https://mcp.evil.example"), transport=FakeStripeMCP()
        )
    assert exc.value.code == PRODUCTION_BLOCKED


def test_real_transport_refuses_live_key_and_needs_a_key():
    with pytest.raises(ATLError) as exc:
        MCPStripeTransport(url="https://mcp.stripe.com", api_key="rk_live_" + "z" * 20)
    assert exc.value.code == PRODUCTION_BLOCKED
    t = MCPStripeTransport(url="https://mcp.stripe.com", api_key=None)
    with pytest.raises(RailError) as exc2:
        t.call_tool("send_money", {})  # fails on the missing key before opening any connection
    assert "STRIPE_AGENT_KEY" in exc2.value.message


def test_pix_transfer_refused(tmp_path):
    rail, fake = make_rail(tmp_path)
    with pytest.raises(RailError):
        rail.transfer_pix(
            pix_key="k", pix_key_type="EVP", amount_cents=1, idempotency_key="x", description=""
        )
    assert fake.calls == []


# ------------------------------------------------------------------ payment


def test_send_money_called_once_with_recipient_amount_and_ids(tmp_path):
    rail, fake = make_rail(tmp_path)
    t = pay(rail)
    sends = fake.calls_to("send_money")
    assert len(sends) == 1
    args = sends[0]
    assert args["recipient"] == RECIPIENT
    assert args["amount"] == 4200 and args["currency"] == "usd"
    assert args["idempotency_key"] == "dec_1"
    assert args["metadata"]["atl_idempotency_key"] == "dec_1"
    assert args["metadata"]["atl_intent_id"] == "int_1"
    assert t.transfer_id == "obp_test_0001" and t.status == "processing"
    assert t.rail == "stripe_sandbox" and t.currency == "USD" and t.destination == RECIPIENT
    assert t.amount_cents == 4200 and t.pix_key == ""


def test_same_idempotency_key_never_pays_twice(tmp_path):
    rail, fake = make_rail(tmp_path)
    first = pay(rail)
    second = pay(rail)
    assert first.transfer_id == second.transfer_id
    assert len(fake.calls_to("send_money")) == 1
    # A fresh rail object on the same state file still knows the payment.
    rail2, fake2 = make_rail(tmp_path)
    assert pay(rail2).transfer_id == first.transfer_id
    assert fake2.calls_to("send_money") == []


def test_same_key_different_payment_refused(tmp_path):
    rail, fake = make_rail(tmp_path)
    pay(rail)
    with pytest.raises(RailError):
        pay(rail, amount=9999)
    assert len(fake.calls_to("send_money")) == 1


def test_wrong_currency_refused_before_call(tmp_path):
    rail, fake = make_rail(tmp_path)
    with pytest.raises(RailError):
        pay(rail, currency="BRL")
    assert fake.calls_to("send_money") == []


def test_payee_without_recipient_refused(tmp_path):
    rail, fake = make_rail(tmp_path)
    connect_only = PayeeRule(payee_id="x", display_name="X", stripe_account_id="acct_1X", categories=["m"])
    with pytest.raises(RailError) as exc:
        pay(rail, payee=connect_only)
    assert "setup-recipient" in exc.value.message
    assert fake.calls_to("send_money") == []


def test_args_not_in_live_schema_are_dropped(tmp_path):
    schemas = [
        {
            "name": "send_money",
            "input_schema": {"properties": {"recipient": {}, "amount": {}, "currency": {}}},
        },
    ]
    rail, fake = make_rail(tmp_path, FakeStripeMCP(schemas=schemas))
    pay(rail)
    assert set(fake.calls_to("send_money")[0]) == {"recipient", "amount", "currency"}
    lines = rail._runlog.path.read_text()
    assert "dropped_args" in lines


# ------------------------------------------------------------------ human confirmation


def test_confirmation_required_goes_pending_then_confirm_completes(tmp_path):
    held = ok(
        {
            "status": "requires_confirmation",
            "confirmation_url": CONFIRM_URL,
            "message": "This action requires human confirmation. Approve it in the Dashboard.",
        }
    )
    fake = FakeStripeMCP({"send_money": [held, lambda a: ok(obp(a["amount"], pid="obp_test_after"))]})
    rail, _ = make_rail(tmp_path, fake)

    t = pay(rail)
    assert t.status == PENDING_RAIL_CONFIRMATION
    assert t.confirmation_url == CONFIRM_URL and t.transfer_id.startswith("spc_")
    assert rail.pending_confirmations()[0]["confirmation_url"] == CONFIRM_URL
    # Asking again does not call Stripe again.
    assert pay(rail).transfer_id == t.transfer_id
    assert len(fake.calls_to("send_money")) == 1
    assert rail.get_transfer(t.transfer_id).status == PENDING_RAIL_CONFIRMATION

    done = rail.confirm_payment("dec_1", approval_token="tok_approved")
    sends = fake.calls_to("send_money")
    assert len(sends) == 2
    assert sends[1]["approval_token"] == "tok_approved"
    assert sends[1]["recipient"] == sends[0]["recipient"] and sends[1]["amount"] == sends[0]["amount"]
    assert done.transfer_id == "obp_test_after" and done.status == "processing"
    assert rail.pending_confirmations() == []
    # Confirming again is a no-op.
    assert rail.confirm_payment("dec_1").transfer_id == "obp_test_after"
    assert len(fake.calls_to("send_money")) == 2


def test_approval_required_error_with_url_in_text_is_pending_not_failure(tmp_path):
    text = f"approval_required: this send_money call needs approval. Review: {CONFIRM_URL}"
    rail, _ = make_rail(tmp_path, FakeStripeMCP({"send_money": [err(text)]}))
    t = pay(rail)
    assert t.status == PENDING_RAIL_CONFIRMATION and t.confirmation_url == CONFIRM_URL


def test_confirm_still_pending_keeps_pending(tmp_path):
    held = ok({"confirmation_url": CONFIRM_URL, "message": "requires confirmation"})
    rail, fake = make_rail(tmp_path, FakeStripeMCP({"send_money": [held]}))
    pay(rail)
    again = rail.confirm_payment("dec_1")
    assert again.status == PENDING_RAIL_CONFIRMATION
    assert len(rail.pending_confirmations()) == 1


def test_abandon_blocks_any_resend(tmp_path):
    held = ok({"confirmation_url": CONFIRM_URL, "message": "requires confirmation"})
    rail, fake = make_rail(tmp_path, FakeStripeMCP({"send_money": [held]}))
    pay(rail)
    rail.abandon_payment("dec_1")
    with pytest.raises(RailError):
        pay(rail)
    assert len(fake.calls_to("send_money")) == 1


def test_confirm_unknown_key(tmp_path):
    rail, _ = make_rail(tmp_path)
    with pytest.raises(RailError) as exc:
        rail.confirm_payment("dec_nope")
    assert exc.value.code == TRANSFER_NOT_FOUND


# ------------------------------------------------------------------ errors


def test_tool_error_is_rail_error_and_can_be_retried(tmp_path):
    fake = FakeStripeMCP(
        {"send_money": [err("insufficient_funds: not enough money"), lambda a: ok(obp(a["amount"]))]}
    )
    rail, _ = make_rail(tmp_path, fake)
    with pytest.raises(RailError) as exc:
        pay(rail)
    assert exc.value.code == RAIL_ERROR and "insufficient_funds" in exc.value.message
    # A definite refusal moved no money, so a later attempt may send.
    assert pay(rail).transfer_id == "obp_test_0001"
    assert len(fake.calls_to("send_money")) == 2


def test_timeout_is_rail_error_and_never_resent(tmp_path):
    fake = FakeStripeMCP({"send_money": [TimeoutError("read timed out")]})
    rail, _ = make_rail(tmp_path, fake)
    with pytest.raises(RailError) as exc:
        pay(rail)
    assert exc.value.code == RAIL_ERROR and isinstance(exc.value, RailOutcomeUnknown)
    with pytest.raises(RailError) as exc2:
        pay(rail)
    assert "will not send again" in exc2.value.message
    assert len(fake.calls_to("send_money")) == 1


def test_timeout_then_lookup_finds_payment(tmp_path):
    listing = ok({"data": [obp(4200, pid="obp_test_found", metadata={"atl_idempotency_key": "dec_1"})]})
    fake = FakeStripeMCP({"send_money": [TimeoutError()], "stripe_api_read": [listing]})
    rail, _ = make_rail(tmp_path, fake)
    t = pay(rail)
    assert t.transfer_id == "obp_test_found"
    assert len(fake.calls_to("send_money")) == 1


def test_unreadable_success_is_uncertain(tmp_path):
    rail, fake = make_rail(tmp_path, FakeStripeMCP({"send_money": [ok({"result": "done"})]}))
    with pytest.raises(RailOutcomeUnknown):
        pay(rail)
    with pytest.raises(RailError):
        pay(rail)
    assert len(fake.calls_to("send_money")) == 1


class _Resp:
    status_code = 401
    headers = {"www-authenticate": 'Bearer resource_metadata="https://mcp.stripe.com/.well-known/..."'}


class _HTTPStatusError(Exception):
    def __init__(self):
        super().__init__("Client error '401 Unauthorized'")
        self.response = _Resp()


def test_401_oauth_challenge_is_rail_error(tmp_path):
    rail, fake = make_rail(tmp_path, FakeStripeMCP({"send_money": [_HTTPStatusError()]}))
    with pytest.raises(RailError) as exc:
        pay(rail)
    assert (
        exc.value.code == RAIL_ERROR and "401" in exc.value.message and "agent API key" in exc.value.message
    )
    assert not isinstance(exc.value, RailOutcomeUnknown)
    # 401 is definite: nothing was sent, so the key is free for a later attempt with a valid key.
    assert rail.state.snapshot()["payments"] == {}


def test_map_transport_error_unwraps_groups_and_scrubs_key():
    group = BaseExceptionGroup("tg", [ValueError("x"), BaseExceptionGroup("inner", [_HTTPStatusError()])])
    assert "401" in map_transport_error(group).message
    assert isinstance(map_transport_error(ExceptionGroup("g", [TimeoutError()])), RailOutcomeUnknown)

    class MCPError(Exception):
        class error:  # noqa: N801 - mimics mcp.shared.exceptions.MCPError.error
            code = -32602
            message = "Invalid params"

    mapped = map_transport_error(MCPError())
    assert mapped.code == RAIL_ERROR and "-32602" in mapped.message
    leak = map_transport_error(RuntimeError(f"boom {TEST_AGENT_KEY}"), secret=TEST_AGENT_KEY)
    assert TEST_AGENT_KEY not in leak.message


def test_key_never_in_run_log(tmp_path):
    rail, _ = make_rail(tmp_path)
    pay(rail)
    rail._runlog.write("probe", note=f"Bearer {TEST_AGENT_KEY}")
    assert TEST_AGENT_KEY not in rail._runlog.path.read_text()


# ------------------------------------------------------------------ status, balance, recipients


def test_get_transfer_and_balance(tmp_path):
    rail, fake = make_rail(tmp_path)
    t = rail.get_transfer("obp_test_xyz")
    assert t.status == "posted" and t.amount_cents == 4200 and t.currency == "USD"
    assert (
        fake.calls_to("stripe_api_read")[0]["path"] == "/v2/money_management/outbound_payments/obp_test_xyz"
    )
    assert rail.balance_cents() == 500000
    with pytest.raises(RailError) as exc:
        rail.get_transfer("obp_test_missing/../x")
    assert exc.value.code == TRANSFER_NOT_FOUND


def test_get_transfer_not_found(tmp_path):
    rail, _ = make_rail(
        tmp_path, FakeStripeMCP({"stripe_api_read": [err("resource_missing: No such object")]})
    )
    with pytest.raises(RailError) as exc:
        rail.get_transfer("obp_test_gone")
    assert exc.value.code == TRANSFER_NOT_FOUND


def test_setup_recipient_stored(tmp_path):
    rail, fake = make_rail(tmp_path)
    rec = rail.setup_recipient(payee_id="acme-supply", display_name="Acme Supply Co", email="ap@acme.example")
    assert rec["recipient_id"] == RECIPIENT
    assert fake.calls_to("setup_recipient")[0]["name"] == "Acme Supply Co"
    assert rail.recipients()["acme-supply"]["recipient_id"] == RECIPIENT


def test_setup_recipient_error(tmp_path):
    rail, _ = make_rail(tmp_path, FakeStripeMCP({"setup_recipient": [err("Treasury is not enabled")]}))
    with pytest.raises(RailError) as exc:
        rail.setup_recipient(payee_id="a", display_name="A", email="a@a.example")
    assert "Treasury" in exc.value.message
