"""Real Asaas sandbox contract tests. Skipped by default.

Run with `uv run pytest -q -m sandbox tests/contract`. Read-only calls only.
Sleep 1 s between calls (rate limit).
"""

from __future__ import annotations

import time
from datetime import date

import pytest

from atl.config import load_settings
from atl.contracts import TRANSFER_NOT_FOUND
from atl.rails import AsaasRail, RailError
from atl.runlog import RunLog

pytestmark = pytest.mark.sandbox

SPIKE_TRANSFER_ID = "4e8f9314-3e50-4ca2-9bbe-cbcdfef26a0f"


@pytest.fixture(scope="module")
def rail():
    settings = load_settings()
    if settings.env != "sandbox" or settings.rail != "asaas":
        pytest.skip("contract tests need ATL_ENV=sandbox and the asaas rail")
    if not settings.api_key:
        pytest.skip("no sandbox key")
    return AsaasRail(settings, runlog=RunLog("contract-tests", settings.runs_dir))


@pytest.fixture(autouse=True)
def _rate_limit():
    yield
    time.sleep(1)


def test_balance_read(rail):
    cents = rail.balance_cents()
    assert isinstance(cents, int)
    assert cents >= 0


def test_account_status(rail):
    status = rail.account_status()
    assert status.get("general") == "APPROVED"


def test_get_spike_transfer(rail):
    t = rail.get_transfer(SPIKE_TRANSFER_ID)
    assert t.transfer_id == SPIKE_TRANSFER_ID
    assert t.rail == "asaas_sandbox"
    assert t.amount_cents == 100
    assert t.pix_key == "cliente-a00001@pix.bcb.gov.br"
    assert t.status in ("PENDING", "BANK_PROCESSING", "DONE")


def test_unknown_transfer_is_not_found(rail):
    with pytest.raises(RailError) as e:
        rail.get_transfer("00000000-0000-0000-0000-000000000000")
    assert e.value.code == TRANSFER_NOT_FOUND


def test_validation_error_body_shape(rail):
    # A customer without a name fails validation; nothing is created.
    with pytest.raises(RailError) as e:
        rail._post_ok("/customers", {})
    assert "HTTP 400" in e.value.message
    assert "no error description" not in e.value.message


def test_lookup_by_external_reference(rail):
    raw = rail.list_transfers(limit=100)
    spike = next(t for t in raw["data"] if t["id"] == SPIKE_TRANSFER_ID)
    ref = spike["externalReference"]
    time.sleep(1)
    found = rail.find_transfer_by_external_reference(ref, since=date(2026, 10, 1))
    assert found is not None and found["id"] == SPIKE_TRANSFER_ID
    time.sleep(1)
    assert rail.find_transfer_by_external_reference("atl-no-such-ref", since=date(2026, 10, 1)) is None
