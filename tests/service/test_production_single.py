"""S-46: `--production-single` dry run on the sandbox rail with a fixture typing PAY."""

from __future__ import annotations

from l4_fakes import Harness

from atl.approvals.production import (
    PROD_SINGLE_AMOUNT_CENTS,
    SANDBOX_DRY_RUN_PIX_KEY,
    run_production_single,
)
from atl.config import PRODUCTION_HOST


def test_s46_dry_run_with_pay(tmp_path):
    h = Harness(tmp_path, rail_name="asaas_sandbox")
    assert h.settings.env == "sandbox" and PRODUCTION_HOST + "/" not in h.settings.base_url + "/"
    out: list[str] = []
    code = run_production_single(h.settings, service=h.svc, input_fn=lambda: "PAY", output_fn=out.append)
    assert code == 0, out
    assert len(h.rail.calls) == 1
    call = h.rail.calls[0]
    assert call["pix_key"] == SANDBOX_DRY_RUN_PIX_KEY and call["pix_key_type"] == "EMAIL"
    assert call["amount_cents"] == PROD_SINGLE_AMOUNT_CENTS
    text = "\n".join(out)
    assert "PRODUCTION" in text and "PASS" in text and "R$ 1.00" in text
    assert SANDBOX_DRY_RUN_PIX_KEY not in text  # key is masked
    assert not any("api.asaas.com" in line for line in out)


def test_s46_dry_run_without_pay_does_not_pay(tmp_path):
    import pytest

    from atl.contracts import PRODUCTION_BLOCKED, ATLError

    h = Harness(tmp_path, rail_name="asaas_sandbox")
    with pytest.raises(ATLError) as e:
        run_production_single(h.settings, service=h.svc, input_fn=lambda: "no", output_fn=lambda s: None)
    assert e.value.code == PRODUCTION_BLOCKED
    assert h.rail.calls == []
