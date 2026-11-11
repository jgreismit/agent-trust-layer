"""Owner authorization watch (atl/rails/authorization.py). Offline, fake rail and clock."""

from __future__ import annotations

from datetime import UTC, datetime

from atl.contracts import RailTransfer
from atl.rails.authorization import awaiting_owner, owner_instruction, wait_for_authorization


def _t(status: str, rail: str = "asaas_sandbox", tid: str = "t1") -> RailTransfer:
    return RailTransfer(
        rail=rail,
        transfer_id=tid,
        status=status,
        amount_cents=1200,
        pix_key="cliente-a00001@pix.bcb.gov.br",
        created_at=datetime(2026, 10, 28, tzinfo=UTC),
    )


class ScriptedRail:
    """get_transfer returns the next status in each transfer's script, then repeats the last one."""

    def __init__(self, scripts: dict[str, list[str]]):
        self.scripts = scripts
        self.calls: list[str] = []

    def get_transfer(self, tid: str) -> RailTransfer:
        self.calls.append(tid)
        script = self.scripts[tid]
        status = script.pop(0) if len(script) > 1 else script[0]
        return _t(status, tid=tid)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s


def test_awaiting_owner():
    assert awaiting_owner(_t("PENDING"))
    assert not awaiting_owner(_t("DONE"))
    assert not awaiting_owner(_t("PENDING", rail="simulated"))


def test_owner_instruction_never_gives_sandbox_token_in_production():
    assert "000000" in owner_instruction("asaas_sandbox")
    assert "000000" not in owner_instruction("asaas_production")


def test_wait_returns_when_owner_authorizes():
    rail = ScriptedRail({"a": ["PENDING", "PENDING", "DONE"], "b": ["BANK_PROCESSING"]})
    clock = FakeClock()
    out = wait_for_authorization(rail, ["a", "b"], 600, poll_s=5, sleep=clock.sleep, clock=clock)
    assert out == {"a": "DONE", "b": "BANK_PROCESSING"}
    assert clock.now == 10
    assert rail.calls.count("b") == 1  # settled transfers are not polled again


def test_wait_times_out_and_reports_pending():
    rail = ScriptedRail({"a": ["PENDING"]})
    clock = FakeClock()
    out = wait_for_authorization(rail, ["a"], 12, poll_s=5, sleep=clock.sleep, clock=clock)
    assert out == {"a": "PENDING"}
    assert clock.now == 12


def test_zero_wait_reads_once():
    rail = ScriptedRail({"a": ["PENDING"]})
    clock = FakeClock()
    assert wait_for_authorization(rail, ["a"], 0, sleep=clock.sleep, clock=clock) == {"a": "PENDING"}
    assert rail.calls == ["a"] and clock.now == 0


def test_authorized_but_still_pending_counts_as_authorized():
    """Live sandbox 2026-10-28: after the owner typed 000000 the transfer read PENDING, authorized true."""

    class AuthorizedRail(ScriptedRail):
        def transfer_authorized(self, tid: str) -> bool:
            return tid == "a"

    rail = AuthorizedRail({"a": ["PENDING"], "b": ["PENDING"]})
    clock = FakeClock()
    out = wait_for_authorization(rail, ["a", "b"], 10, poll_s=5, sleep=clock.sleep, clock=clock)
    assert out == {"a": "PENDING (authorized)", "b": "PENDING"}
    assert rail.calls.count("a") == 1
