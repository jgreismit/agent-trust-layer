"""Offline tests for the Stripe REST fallback (ATL_STRIPE_TRANSPORT=rest) with httpx.MockTransport."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stripe_fakes import CONNECT_ACCOUNT, TEST_SECRET_KEY, stripe_settings  # noqa: E402

from atl.contracts import (  # noqa: E402
    PRODUCTION_BLOCKED,
    RAIL_DUPLICATE_TRANSFER,
    RAIL_ERROR,
    TRANSFER_NOT_FOUND,
    ATLError,
    PayeeRule,
)
from atl.rails.errors import RailError  # noqa: E402
from atl.rails.stripe_mcp import StripeMCPRail  # noqa: E402
from atl.runlog import RunLog  # noqa: E402

PAYEE = PayeeRule(
    payee_id="acme-supply", display_name="Acme Supply Co", stripe_account_id=CONNECT_ACCOUNT, categories=["m"]
)


def transfer_json(amount=4200, tid="tr_test_1", **over):
    data = {
        "id": tid,
        "object": "transfer",
        "amount": amount,
        "currency": "usd",
        "destination": CONNECT_ACCOUNT,
        "created": 1794495600,
        "reversed": False,
        "metadata": {"atl_idempotency_key": "dec_1"},
    }
    data.update(over)
    return data


class Recorder:
    def __init__(self, responses):
        self.requests: list[httpx.Request] = []
        self.responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        resp = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(resp, Exception):
            raise resp
        return resp(request) if callable(resp) else resp


def make_rail(tmp_path, responses, **over):
    rec = Recorder(responses)
    client = httpx.Client(transport=httpx.MockTransport(rec))
    settings = stripe_settings(tmp_path, stripe_transport="rest", **over)
    rail = StripeMCPRail(
        settings, RunLog("run_test", tmp_path / "runs"), http_client=client, sleep=lambda s: None
    )
    return rail, rec


def pay(rail, key="dec_1", amount=4200):
    return rail.pay_destination(
        payee=PAYEE,
        amount_cents=amount,
        currency="USD",
        idempotency_key=key,
        description="ATL INV-1 dec_1",
        metadata={"atl_decision_id": key, "atl_intent_id": "int_1"},
    )


def test_rest_happy_path(tmp_path):
    rail, rec = make_rail(tmp_path, [httpx.Response(200, json=transfer_json())])
    t = pay(rail)
    assert t.transfer_id == "tr_test_1" and t.status == "paid" and t.amount_cents == 4200
    assert t.destination == CONNECT_ACCOUNT and t.currency == "USD"
    req = rec.requests[0]
    assert req.method == "POST" and req.url == "https://api.stripe.com/v1/transfers"
    assert req.headers["Idempotency-Key"] == "dec_1"
    assert req.headers["Authorization"] == f"Bearer {TEST_SECRET_KEY}"
    form = parse_qs(req.content.decode())
    assert form["amount"] == ["4200"] and form["currency"] == ["usd"]
    assert form["destination"] == [CONNECT_ACCOUNT] and form["transfer_group"] == ["dec_1"]
    assert form["metadata[atl_intent_id]"] == ["int_1"]
    # Replay: no second request.
    assert pay(rail).transfer_id == "tr_test_1"
    assert len(rec.requests) == 1
    assert TEST_SECRET_KEY not in rail._runlog.path.read_text()


def test_rest_5xx_retried_with_same_idempotency_key(tmp_path):
    rail, rec = make_rail(
        tmp_path,
        [
            httpx.Response(500, json={"error": {"type": "api_error"}}),
            httpx.Response(200, json=transfer_json()),
        ],
    )
    assert pay(rail).transfer_id == "tr_test_1"
    assert [r.headers["Idempotency-Key"] for r in rec.requests] == ["dec_1", "dec_1"]


def test_rest_error_path(tmp_path):
    body = {
        "error": {
            "type": "invalid_request_error",
            "code": "balance_insufficient",
            "message": "Insufficient funds",
        }
    }
    rail, rec = make_rail(tmp_path, [httpx.Response(400, json=body)])
    with pytest.raises(RailError) as exc:
        pay(rail)
    assert exc.value.code == RAIL_ERROR and "balance_insufficient" in exc.value.message
    assert len(rec.requests) == 1


def test_rest_409_idempotency_conflict(tmp_path):
    body = {
        "error": {"type": "idempotency_error", "message": "Keys for idempotent requests can only be used..."}
    }
    rail, _ = make_rail(tmp_path, [httpx.Response(409, json=body)])
    with pytest.raises(RailError) as exc:
        pay(rail)
    assert exc.value.code == RAIL_DUPLICATE_TRANSFER


def test_rest_401(tmp_path):
    rail, _ = make_rail(tmp_path, [httpx.Response(401, json={"error": {"type": "authentication_error"}})])
    with pytest.raises(RailError) as exc:
        pay(rail)
    assert "401" in exc.value.message


def test_rest_timeout_then_lookup_by_transfer_group(tmp_path):
    def listing(request):
        assert request.url.params["transfer_group"] == "dec_1"
        return httpx.Response(200, json={"object": "list", "data": [transfer_json(tid="tr_test_found")]})

    rail, rec = make_rail(tmp_path, [httpx.ReadTimeout("t"), httpx.ReadTimeout("t"), listing])
    assert pay(rail).transfer_id == "tr_test_found"
    assert [r.method for r in rec.requests] == ["POST", "POST", "GET"]


def test_rest_get_transfer_and_balance(tmp_path):
    def route(request):
        if request.url.path == "/v1/balance":
            return httpx.Response(200, json={"available": [{"amount": 12345, "currency": "usd"}]})
        if request.url.path == "/v1/transfers/tr_test_1":
            return httpx.Response(200, json=transfer_json(reversed=True))
        return httpx.Response(
            404, json={"error": {"type": "invalid_request_error", "code": "resource_missing"}}
        )

    rail, _ = make_rail(tmp_path, [route])
    assert rail.balance_cents() == 12345
    assert rail.get_transfer("tr_test_1").status == "reversed"
    with pytest.raises(RailError) as exc:
        rail.get_transfer("tr_test_nope")
    assert exc.value.code == TRANSFER_NOT_FOUND


def test_rest_live_secret_key_refused(tmp_path):
    with pytest.raises(ATLError) as exc:
        make_rail(tmp_path, [httpx.Response(200)], stripe_secret_key="sk_live_" + "q" * 30)
    assert exc.value.code == PRODUCTION_BLOCKED


def test_rest_needs_connect_account(tmp_path):
    rail, rec = make_rail(tmp_path, [httpx.Response(200, json=transfer_json())])
    treasury_only = PayeeRule(
        payee_id="t", display_name="T", stripe_recipient_id="acct_test_r", categories=["m"]
    )
    with pytest.raises(RailError):
        rail.pay_destination(
            payee=treasury_only,
            amount_cents=1,
            currency="USD",
            idempotency_key="k",
            description="",
            metadata={},
        )
    assert rec.requests == []
