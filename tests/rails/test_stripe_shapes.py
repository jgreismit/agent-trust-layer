"""Unit tests for atl/rails/stripe_shapes.py (the one module holding Stripe tool shape assumptions)."""

from __future__ import annotations

from atl.rails import stripe_shapes as sh


def test_fit_to_schema():
    args = {"recipient": "acct_1", "amount": 1, "metadata": {}}
    kept, dropped = sh.fit_to_schema(args, {"properties": {"recipient": {}, "amount": {}}})
    assert kept == {"recipient": "acct_1", "amount": 1} and dropped == ["metadata"]
    assert sh.fit_to_schema(args, None) == (args, [])
    assert sh.fit_to_schema(args, {"type": "object"}) == (args, [])


def test_parse_recipient_structured_and_text():
    r = sh.parse_recipient(
        sh.ToolResult(False, {"recipient": {"id": "acct_test_9"}, "url": "https://connect.stripe.com/x"})
    )
    assert r.recipient_id == "acct_test_9" and r.onboarding_url == "https://connect.stripe.com/x"
    r2 = sh.parse_recipient(
        sh.ToolResult(False, None, "Recipient acct_test_42 created. Onboarding: https://x.stripe.com/o")
    )
    assert r2.recipient_id == "acct_test_42" and r2.onboarding_url == "https://x.stripe.com/o"
    assert sh.parse_recipient(sh.ToolResult(False, None, "done")) is None


def test_parse_send_money_kinds():
    sent = sh.parse_send_money(
        sh.ToolResult(
            False,
            None,
            '{"id": "obp_test_1", "status": "posted", "amount": {"value": 900, "currency": "usd"}}',
        )
    )
    assert (sent.kind, sent.payment_id, sent.status, sent.amount_cents, sent.currency) == (
        "sent",
        "obp_test_1",
        "posted",
        900,
        "USD",
    )
    text_only = sh.parse_send_money(
        sh.ToolResult(False, None, "Sent. Outbound payment obp_test_7 is processing.")
    )
    assert text_only.kind == "sent" and text_only.payment_id == "obp_test_7"
    held = sh.parse_send_money(
        sh.ToolResult(
            False,
            {"approval_request_id": "apr_1"},
            "Human confirmation required: approve at https://dashboard.stripe.com/test/approvals/apr_1",
        )
    )
    assert held.kind == "confirmation_required"
    assert held.confirmation_url == "https://dashboard.stripe.com/test/approvals/apr_1"
    assert held.approval_ref == {"approval_request_id": "apr_1"}
    assert sh.parse_send_money(sh.ToolResult(True, None, "insufficient_funds")).kind == "error"
    assert sh.parse_send_money(sh.ToolResult(False, {"ok": True}, "")).kind == "unknown"


def test_unrelated_url_is_not_a_confirmation():
    r = sh.ToolResult(False, {"id": "obp_test_1", "receipt_url": "https://payments.stripe.com/receipt/x"}, "")
    assert sh.parse_send_money(r).kind == "sent"


def test_send_money_args():
    a = sh.send_money_args(
        recipient_id="acct_1",
        amount_cents=4200,
        currency="USD",
        description="d",
        idempotency_key="dec_1",
        metadata={"m": "1"},
        approval_ref={"approval_token": "t"},
    )
    assert a["amount"] == 4200 and a["currency"] == "usd" and a["approval_token"] == "t"
    assert "financial_account" not in a


def test_parse_balance_v1_and_v2():
    assert (
        sh.parse_balance(
            {"available": [{"amount": 5, "currency": "usd"}, {"amount": 7, "currency": "eur"}]}, "USD"
        )
        == 5
    )
    assert (
        sh.parse_balance({"balance": {"available": {"usd": {"value": 123, "currency": "usd"}}}}, "USD") == 123
    )
    assert sh.parse_balance({"nothing": 1}, "USD") is None


def test_find_outbound_payment():
    data = {
        "data": [{"id": "obp_test_a", "description": "ATL INV dec_9"}, {"id": "obp_test_b", "metadata": {}}]
    }
    assert sh.find_outbound_payment(data, "dec_9").payment_id == "obp_test_a"
    assert sh.find_outbound_payment(data, "dec_0") is None


def test_rest_form_and_errors():
    f = sh.rest_transfer_form(
        destination="acct_1",
        amount_cents=10,
        currency="USD",
        description="d",
        idempotency_key="dec_1",
        metadata={"atl_intent_id": "int_1"},
    )
    assert f["metadata[atl_intent_id]"] == "int_1" and f["transfer_group"] == "dec_1"
    assert sh.rest_error_message({"error": {"type": "t", "code": "c", "message": "m"}}) == "t: c: m"
