"""The smoke script imports and prints usage without a key, and never connects in that case."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from atl.contracts import Mandate

REPO = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "stripe_smoke", REPO / "scripts" / "stripe_sandbox_smoke.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("argv", [[], ["--list-tools"], ["--transport", "rest"]])
def test_usage_without_key(monkeypatch, capsys, argv):
    monkeypatch.delenv("STRIPE_AGENT_KEY", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    mod = _load()
    assert mod.main(argv) == 2
    out = capsys.readouterr()
    assert "usage: stripe_sandbox_smoke.py" in out.out
    assert "is not set" in out.err


def test_live_key_refused_before_connecting(monkeypatch, capsys):
    monkeypatch.setenv("STRIPE_AGENT_KEY", "rk_live_" + "n" * 24)
    mod = _load()
    assert mod.main(["--list-tools"]) == 2
    err = capsys.readouterr().err
    assert "PRODUCTION_BLOCKED" in err and "rk_live_" not in err


def test_example_mandate_is_a_valid_usd_mandate():
    data = json.loads((REPO / "examples" / "us-mandate.json").read_text())
    m = Mandate.model_validate({**data, "signature": ""})
    assert m.currency == "USD" and m.payees[0].destination_kind == "stripe"
