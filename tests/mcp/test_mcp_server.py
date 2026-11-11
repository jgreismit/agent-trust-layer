"""In-process MCP client against the ATL server (fakes behind the service)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from l4_fakes import Harness
from mcp.client import Client

from atl.mcp_server import TOOL_NAMES, build_server


def _payload(result) -> dict:
    if isinstance(result.structured_content, dict) and result.structured_content:
        sc = result.structured_content
        return sc.get("result", sc) if set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


async def test_lists_exactly_six_tools(tmp_path):
    h = Harness(tmp_path)
    server = build_server(lambda: h.svc, h.runlog)
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    names = sorted(t.name for t in tools)
    assert names == sorted(TOOL_NAMES)
    by_name = {t.name: t for t in tools}
    desc = by_name["pay_check"].description.lower()
    assert "always call this first" in desc and "next_action" in desc and "never retry" in desc
    props = by_name["pay_check"].input_schema["properties"]
    assert props["amount_cents"]["type"] == "integer"
    assert "wait_seconds" in by_name["get_decision"].input_schema["properties"]


async def test_pay_check_and_execute_through_mcp(tmp_path):
    h = Harness(tmp_path)
    h.register()
    server = build_server(lambda: h.svc, h.runlog)
    async with Client(server) as client:
        res = _payload(await client.call_tool("pay_check", h.intent()))
        assert res["verdict"] == "approve" and res["next_action"] == "call_execute_payment"
        ex = _payload(await client.call_tool("execute_payment", {"decision_id": res["decision_id"]}))
        assert ex["status"] == "executed" and ex["transfer_id"] == "tr_1"
        again = _payload(await client.call_tool("execute_payment", {"decision_id": res["decision_id"]}))
        assert again["transfer_id"] == "tr_1"
        gd = _payload(await client.call_tool("get_decision", {"decision_id": res["decision_id"]}))
        assert gd["next_action"] == "already_executed"
        v = _payload(await client.call_tool("verify_receipt", {"receipt_id": ex["receipt_id"]}))
        assert v["valid"] is True
        r = _payload(await client.call_tool("get_receipt", {"receipt_id": ex["receipt_id"]}))
        assert r["receipt_id"] == ex["receipt_id"]
        err = _payload(await client.call_tool("execute_payment", {"decision_id": "dec_missing"}))
        assert err["error"] == "DECISION_NOT_FOUND"
    assert len(h.rail.calls) == 1
    lines = [x for x in h.log_lines() if x["event"] == "tool_call"]
    assert [x["tool"] for x in lines] == [
        "pay_check",
        "execute_payment",
        "execute_payment",
        "get_decision",
        "verify_receipt",
        "get_receipt",
        "execute_payment",
    ]
    assert lines[0]["result"]["verdict"] == "approve"
    assert lines[1]["result"]["transfer_id"] == "tr_1"
    assert lines[-1]["result"]["error"] == "DECISION_NOT_FOUND"
    assert all(isinstance(x["latency_ms"], int) for x in lines)


async def test_refused_through_mcp_cannot_execute(tmp_path):
    h = Harness(tmp_path)
    h.register()
    server = build_server(lambda: h.svc, h.runlog)
    async with Client(server) as client:
        res = _payload(await client.call_tool("pay_check", h.intent(pix_key="attacker")))
        assert res["next_action"] == "do_not_pay_report_to_owner"
        ex = _payload(await client.call_tool("execute_payment", {"decision_id": res["decision_id"]}))
    assert ex["error"] == "DECISION_NOT_APPROVED"
    assert h.rail.calls == []


async def test_get_decision_wait_returns_after_owner_approves(tmp_path):
    import anyio

    h = Harness(tmp_path)
    h.register()
    server = build_server(lambda: h.svc, h.runlog)
    async with Client(server) as client:
        res = _payload(await client.call_tool("pay_check", h.intent(amount_cents=4800)))
        assert res["next_action"] == "wait_for_owner"

        async def owner():
            await anyio.sleep(1.2)
            h.svc.approve(res["decision_id"])

        async with anyio.create_task_group() as tg:
            tg.start_soon(owner)
            args = {"decision_id": res["decision_id"], "wait_seconds": 10}
            gd = _payload(await client.call_tool("get_decision", args))
    assert gd["status"] == "approved" and gd["next_action"] == "call_execute_payment"


def test_server_module_starts_without_stdout_noise(tmp_path):
    """`python -m atl.mcp_server` starts and exits cleanly on EOF, printing nothing to stdout."""
    env = {
        k: v for k, v in os.environ.items() if not k.startswith("ASAAS_") and not k.startswith("ATL_")
    }
    env.update(ATL_VAR_DIR=str(tmp_path / "var"), ATL_RUNS_DIR=str(tmp_path / "runs"), ATL_RAIL="simulated")
    proc = subprocess.run(
        [sys.executable, "-m", "atl.mcp_server"],
        input=b"",
        capture_output=True,
        env=env,
        cwd=Path(__file__).resolve().parents[2],
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr.decode()[-2000:]
    assert proc.stdout == b""
