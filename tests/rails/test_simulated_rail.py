from __future__ import annotations

import json
import threading

import pytest

from atl.contracts import RAIL_ERROR, TRANSFER_NOT_FOUND, Rail
from atl.rails import RailError, SimulatedRail


def _pay(rail, cents=1200, key="idem-1"):
    return rail.transfer_pix(
        pix_key="cliente-a00001@pix.bcb.gov.br",
        pix_key_type="EMAIL",
        amount_cents=cents,
        idempotency_key=key,
        description="x",
    )


def test_starts_at_20000_and_persists(tmp_path):
    path = tmp_path / "var" / "sim_rail.json"
    rail = SimulatedRail(path)
    assert isinstance(rail, Rail)
    assert rail.balance_cents() == 20000
    t = _pay(rail)
    assert t.rail == "simulated"
    assert t.transfer_id.startswith("sim_")
    assert t.status == "DONE"
    assert t.amount_cents == 1200
    again = SimulatedRail(path)
    assert again.balance_cents() == 18800
    assert again.get_transfer(t.transfer_id) == t


def test_first_balance_read_persists_state(tmp_path):
    path = tmp_path / "var" / "sim_rail.json"
    assert SimulatedRail(path).balance_cents() == 20000
    assert path.is_file()
    assert json.loads(path.read_text()) == {"balance_cents": 20000, "transfers": []}


def test_same_idempotency_key_returns_same_transfer(tmp_path):
    rail = SimulatedRail(tmp_path / "s.json")
    a = _pay(rail)
    b = _pay(rail)
    assert a == b
    assert rail.balance_cents() == 18800


def test_not_found_and_errors(tmp_path):
    rail = SimulatedRail(tmp_path / "s.json")
    with pytest.raises(RailError) as e:
        rail.get_transfer("sim_missing")
    assert e.value.code == TRANSFER_NOT_FOUND
    with pytest.raises(RailError) as e2:
        _pay(rail, cents=20001)
    assert e2.value.code == RAIL_ERROR
    with pytest.raises(RailError):
        _pay(rail, cents=0)


def test_set_balance(tmp_path):
    rail = SimulatedRail(tmp_path / "s.json")
    _pay(rail, cents=15000)
    rail.set_balance_cents(20000)
    assert rail.balance_cents() == 20000


def test_concurrent_transfers_are_consistent(tmp_path):
    path = tmp_path / "s.json"
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            _pay(SimulatedRail(path), cents=100, key=f"k{i}")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert SimulatedRail(path).balance_cents() == 18000
