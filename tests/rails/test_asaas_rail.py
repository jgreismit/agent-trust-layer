"""Offline tests for AsaasRail with respx. No network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from atl.config import SANDBOX_BASE_URL, Settings
from atl.contracts import (
    PRODUCTION_BLOCKED,
    RAIL_DUPLICATE_TRANSFER,
    RAIL_ERROR,
    TRANSFER_NOT_FOUND,
    ATLError,
    Rail,
)
from atl.rails import AsaasRail, RailError, SimulatedRail, get_rail
from atl.rails.asaas import cents_to_value, value_to_cents
from atl.runlog import RunLog

FAKE_KEY = "$aact_hmlg_" + "k" * 48
BASE = SANDBOX_BASE_URL
TRANSFER_ID = "4e8f9314-3e50-4ca2-9bbe-cbcdfef26a0f"


def make_settings(tmp_path: Path, **over) -> Settings:
    data = dict(
        env="sandbox",
        rail="asaas",
        base_url=BASE,
        api_key=FAKE_KEY,
        root=tmp_path,
        var_dir=tmp_path / "var",
        db_path=tmp_path / "var" / "atl.db",
        keys_dir=tmp_path / "var" / "keys",
        runs_dir=tmp_path / "runs",
        run_id="run_test",
    )
    data.update(over)
    return Settings(**data)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def runlog(tmp_path) -> RunLog:
    return RunLog("run_test", tmp_path / "runs")


def _transfer_json(**over) -> dict:
    data = {
        "object": "transfer",
        "id": TRANSFER_ID,
        "dateCreated": "2026-10-28",
        "status": "PENDING",
        "value": 12.0,
        "netValue": 12.0,
        "transferFee": 0,
        "authorized": False,
        "canBeCancelled": True,
        "externalReference": "idem-1",
        "operationType": "PIX",
        "bankAccount": {"pixAddressKey": "cliente-a00001@pix.bcb.gov.br"},
    }
    data.update(over)
    return data


def _rail(settings, runlog) -> AsaasRail:
    return AsaasRail(settings, runlog=runlog, sleep=lambda s: None)


def _log_lines(runlog) -> list[dict]:
    return [json.loads(line) for line in runlog.path.read_text().splitlines()]


def _pay(rail, amount_cents=1200, key="idem-1"):
    return rail.transfer_pix(
        pix_key="cliente-a00001@pix.bcb.gov.br",
        pix_key_type="EMAIL",
        amount_cents=amount_cents,
        idempotency_key=key,
        description="invoice 1",
    )


def test_cents_conversion_both_ways():
    assert cents_to_value(1200) == 12.0
    assert cents_to_value(1) == 0.01
    assert value_to_cents(208.01) == 20801
    assert value_to_cents(0.29) == 29
    assert value_to_cents(1.005) == 101
    assert value_to_cents(0) == 0
    assert value_to_cents(None) == 0
    with pytest.raises(RailError):
        cents_to_value(12.5)  # type: ignore[arg-type]


@respx.mock
def test_balance_and_headers(settings, runlog):
    route = respx.get(f"{BASE}/finance/balance").respond(200, json={"balance": 208.01})
    rail = _rail(settings, runlog)
    assert rail.balance_cents() == 20801
    req = route.calls.last.request
    assert req.headers["access_token"] == FAKE_KEY
    assert req.headers["User-Agent"] == "atl/0.1"
    assert rail.name == "asaas_sandbox"
    assert isinstance(rail, Rail)
    line = _log_lines(runlog)[-1]
    assert line["event"] == "rail_call"
    assert line["method"] == "GET" and line["path"] == "/finance/balance"
    assert line["status"] == 200 and isinstance(line["ms"], int)


@respx.mock
def test_transfer_body_shape_and_result(settings, runlog):
    route = respx.post(f"{BASE}/transfers").respond(200, json=_transfer_json(transferFee=0.5))
    t = _pay(_rail(settings, runlog))
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "value": 12.0,
        "operationType": "PIX",
        "pixAddressKey": "cliente-a00001@pix.bcb.gov.br",
        "pixAddressKeyType": "EMAIL",
        "description": "invoice 1",
        "externalReference": "idem-1",
    }
    assert t.rail == "asaas_sandbox"
    assert t.transfer_id == TRANSFER_ID
    assert t.status == "PENDING"
    assert t.amount_cents == 1200
    assert t.fee_cents == 50
    assert t.pix_key == "cliente-a00001@pix.bcb.gov.br"
    assert t.created_at.tzinfo is not None
    line = [x for x in _log_lines(runlog) if x["event"] == "rail_call"][-1]
    assert line["request"]["externalReference"] == "idem-1"
    assert line["response"]["id"] == TRANSFER_ID


@respx.mock
def test_authorized_false_is_logged(settings, runlog):
    respx.post(f"{BASE}/transfers").respond(200, json=_transfer_json())
    _pay(_rail(settings, runlog))
    events = [x for x in _log_lines(runlog) if x["event"] == "sandbox_critical_action_pending"]
    assert len(events) == 1
    assert events[0]["transfer_id"] == TRANSFER_ID
    assert "000000" in events[0]["note"]


@respx.mock
def test_authorized_true_not_logged(settings, runlog):
    respx.post(f"{BASE}/transfers").respond(200, json=_transfer_json(authorized=True, status="DONE"))
    _pay(_rail(settings, runlog))
    assert not [x for x in _log_lines(runlog) if x["event"] == "sandbox_critical_action_pending"]


@respx.mock
def test_get_transfer_404_maps_to_not_found(settings, runlog):
    respx.get(f"{BASE}/transfers/nope").respond(
        404, json={"errors": [{"code": "not_found", "description": "x"}]}
    )
    with pytest.raises(RailError) as e:
        _rail(settings, runlog).get_transfer("nope")
    assert e.value.code == TRANSFER_NOT_FOUND


@respx.mock
def test_get_transfer_ok(settings, runlog):
    respx.get(f"{BASE}/transfers/{TRANSFER_ID}").respond(200, json=_transfer_json(value=1.0))
    t = _rail(settings, runlog).get_transfer(TRANSFER_ID)
    assert t.amount_cents == 100 and t.status == "PENDING"
    assert t.created_at.isoformat().startswith("2026-10-28")


@respx.mock
def test_400_maps_to_rail_error_with_descriptions(settings, runlog):
    respx.post(f"{BASE}/transfers").respond(
        400, json={"errors": [{"code": "invalid_value", "description": "Saldo insuficiente."}]}
    )
    with pytest.raises(RailError) as e:
        _pay(_rail(settings, runlog))
    assert e.value.code == RAIL_ERROR
    assert "Saldo insuficiente." in e.value.message
    assert FAKE_KEY not in str(e.value)


@respx.mock
def test_409_names_checkout_already_requested(settings, runlog):
    post = respx.post(f"{BASE}/transfers").respond(
        409, json={"errors": [{"code": "checkout.already.requested", "description": "Ja solicitado"}]}
    )
    with pytest.raises(RailError) as e:
        _pay(_rail(settings, runlog))
    assert e.value.code == RAIL_DUPLICATE_TRANSFER
    assert "checkout.already.requested" in e.value.message
    assert "no money moved" in e.value.message
    assert post.call_count == 1


@respx.mock
def test_cancel_transfer_uses_delete(settings, runlog):
    route = respx.delete(f"{BASE}/transfers/{TRANSFER_ID}/cancel").respond(
        200, json=_transfer_json(status="CANCELLED")
    )
    assert _rail(settings, runlog).cancel_transfer(TRANSFER_ID)["status"] == "CANCELLED"
    assert route.call_count == 1
    respx.delete(f"{BASE}/transfers/nope/cancel").respond(404)
    with pytest.raises(RailError) as e:
        _rail(settings, runlog).cancel_transfer("nope")
    assert e.value.code == TRANSFER_NOT_FOUND


@respx.mock
def test_get_retries_on_5xx(settings, runlog):
    route = respx.get(f"{BASE}/finance/balance").mock(
        side_effect=[httpx.Response(502), httpx.Response(503), httpx.Response(200, json={"balance": 1})]
    )
    assert _rail(settings, runlog).balance_cents() == 100
    assert route.call_count == 3


@respx.mock
def test_get_gives_up_after_three_5xx(settings, runlog):
    route = respx.get(f"{BASE}/finance/balance").respond(500)
    with pytest.raises(RailError) as e:
        _rail(settings, runlog).balance_cents()
    assert e.value.code == RAIL_ERROR
    assert route.call_count == 3


@respx.mock
def test_get_does_not_retry_4xx(settings, runlog):
    route = respx.get(f"{BASE}/finance/balance").respond(401, json={"errors": []})
    with pytest.raises(RailError):
        _rail(settings, runlog).balance_cents()
    assert route.call_count == 1


@respx.mock
def test_post_5xx_finds_existing_transfer_and_does_not_repost(settings, runlog):
    post = respx.post(f"{BASE}/transfers").respond(502)
    lookup = respx.get(f"{BASE}/transfers").respond(
        200,
        json={"data": [_transfer_json(externalReference="other"), _transfer_json()], "hasMore": False},
    )
    t = _pay(_rail(settings, runlog))
    assert t.transfer_id == TRANSFER_ID
    assert post.call_count == 1
    assert lookup.call_count == 1
    assert lookup.calls.last.request.url.params["externalReference"] == "idem-1"
    assert [x for x in _log_lines(runlog) if x["event"] == "rail_transfer_recovered"]


@respx.mock
def test_post_timeout_reposts_only_when_not_found(settings, runlog):
    post = respx.post(f"{BASE}/transfers").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json=_transfer_json())]
    )
    lookup = respx.get(f"{BASE}/transfers").respond(
        200, json={"data": [_transfer_json(externalReference="other")], "hasMore": False}
    )
    t = _pay(_rail(settings, runlog))
    assert t.transfer_id == TRANSFER_ID
    assert post.call_count == 2
    assert lookup.call_count == 1


@respx.mock
def test_post_fails_twice_raises(settings, runlog):
    post = respx.post(f"{BASE}/transfers").respond(500)
    respx.get(f"{BASE}/transfers").respond(200, json={"data": [], "hasMore": False})
    with pytest.raises(RailError) as e:
        _pay(_rail(settings, runlog))
    assert e.value.code == RAIL_ERROR
    assert post.call_count == 2


def test_rejects_float_and_non_positive_amount(settings, runlog):
    rail = _rail(settings, runlog)
    with pytest.raises(RailError):
        _pay(rail, amount_cents=0)
    with pytest.raises(RailError):
        _pay(rail, amount_cents=12.0)  # type: ignore[arg-type]


@respx.mock(assert_all_called=False)
def test_production_host_blocked_before_io(tmp_path, runlog, respx_mock):
    prod = respx_mock.route(host="api.asaas.com").respond(200, json={"balance": 1})
    bad = make_settings(tmp_path, base_url="https://api.asaas.com/v3")
    with pytest.raises(ATLError) as e:
        AsaasRail(bad, runlog=runlog)
    assert e.value.code == PRODUCTION_BLOCKED
    # Also guarded per request, even if the base URL is swapped after construction.
    rail = _rail(make_settings(tmp_path), runlog)
    rail._base_url = "https://api.asaas.com/v3"
    with pytest.raises(ATLError) as e2:
        rail.balance_cents()
    assert e2.value.code == PRODUCTION_BLOCKED
    assert prod.call_count == 0


@respx.mock
def test_key_never_in_log_file(settings, runlog):
    respx.get(f"{BASE}/finance/balance").respond(200, json={"balance": 1, "echo": FAKE_KEY})
    respx.post(f"{BASE}/transfers").respond(400, json={"errors": [{"description": f"bad {FAKE_KEY}"}]})
    rail = _rail(settings, runlog)
    rail.balance_cents()
    with pytest.raises(RailError):
        _pay(rail)
    text = runlog.path.read_text()
    assert FAKE_KEY not in text
    assert "aact_" not in text
    assert "access_token" not in text


def test_missing_key_raises_rail_error(tmp_path, runlog):
    rail = _rail(make_settings(tmp_path, api_key=None), runlog)
    with pytest.raises(RailError) as e:
        rail.balance_cents()
    assert e.value.code == RAIL_ERROR


def test_get_rail_selects_implementation(tmp_path):
    assert isinstance(get_rail(make_settings(tmp_path)), AsaasRail)
    sim = get_rail(make_settings(tmp_path, rail="simulated"))
    assert isinstance(sim, SimulatedRail)
    assert sim.name == "simulated"
    assert sim.state_path == tmp_path / "var" / "sim_rail.json"


def test_production_name(tmp_path, runlog):
    rail = AsaasRail(make_settings(tmp_path, env="production", base_url="https://api.asaas.com/v3"), runlog)
    assert rail.name == "asaas_production"
    with pytest.raises(RailError):
        rail.confirm_sandbox_payment("pay_1")
