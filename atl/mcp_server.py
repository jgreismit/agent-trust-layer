"""MCP server `atl` over stdio (lane L4).

Run: uv run python -m atl.mcp_server
Tools: register_mandate, pay_check, get_decision, execute_payment, get_receipt, verify_receipt

stdout is the MCP protocol channel: nothing in this module prints to it. Diagnostics go to stderr and
every tool call writes one `tool_call` line to runs/<run_id>.jsonl.

The installed mcp SDK is 2.x, where FastMCP is named MCPServer; mcp 1.x FastMCP is the fallback.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

import anyio

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]

from atl.config import load_settings
from atl.contracts import ATLError
from atl.runlog import RunLog, Timer
from atl.service import MAX_WAIT_SECONDS, POLL_SECONDS, ATLService, error_result, log_tool_call

TOOL_NAMES = (
    "register_mandate",
    "pay_check",
    "get_decision",
    "execute_payment",
    "get_receipt",
    "verify_receipt",
)

INSTRUCTIONS = (
    "Agent Trust Layer: every payment goes through pay_check first. Follow next_action exactly. "
    "Never retry a refused payment; report the reasons to the owner."
)


def build_server(get_service: Callable[[], ATLService], runlog: RunLog | None = None) -> Any:
    """Build the MCP server. `get_service` is called lazily on the first tool call."""
    server = MCPServer("atl", instructions=INSTRUCTIONS)

    def _runlog() -> RunLog | None:
        if runlog is not None:
            return runlog
        try:
            return get_service().runlog
        except Exception:  # noqa: BLE001 - logging must never break a tool call
            return None

    async def call(tool: str, args: dict, fn: Callable[[ATLService], Awaitable[dict] | dict]) -> dict:
        with Timer() as t:
            try:
                out = fn(get_service())
                result = await out if hasattr(out, "__await__") else out
            except Exception as exc:  # noqa: BLE001 - errors go back to the agent as data
                result = error_result(exc)
        log_tool_call(_runlog(), tool, args, result, t.ms)
        return result

    @server.tool(
        description=(
            "Owner tool. Register a mandate signed by the owner key (payees, caps, expiry). "
            "Fails with MANDATE_BAD_SIGNATURE if any field was changed after signing."
        )
    )
    async def register_mandate(mandate: dict[str, Any]) -> dict:
        return await call("register_mandate", {"mandate": mandate}, lambda s: s.register_mandate(mandate))

    @server.tool(
        description=(
            "ALWAYS call this first, before any payment. Submit the invoice as a payment intent "
            "(amount in integer cents, the Pix key you intend to pay, the invoice memo copied verbatim). "
            "Returns decision_id, verdict (approve, escalate, refuse), rule_hits, reasons and next_action. "
            "Follow next_action exactly: call_execute_payment -> call execute_payment with decision_id; "
            "wait_for_owner -> poll get_decision; do_not_pay_report_to_owner -> do not pay, report the "
            "reasons to the owner. Never retry a refuse with changed values."
        )
    )
    async def pay_check(
        mandate_id: str,
        agent_id: str,
        payee_id: str,
        pix_key: str,
        amount_cents: int,
        category: str,
        invoice_ref: str,
        memo: str,
        idempotency_key: str,
    ) -> dict:
        args = dict(
            mandate_id=mandate_id,
            agent_id=agent_id,
            payee_id=payee_id,
            pix_key=pix_key,
            amount_cents=amount_cents,
            category=category,
            invoice_ref=invoice_ref,
            memo=memo,
            idempotency_key=idempotency_key,
        )
        return await call("pay_check", args, lambda s: s.pay_check(**args))

    @server.tool(
        description=(
            "Read the current status of a decision. Use it while next_action is wait_for_owner. "
            "wait_seconds (0 to 30) blocks while the owner has not answered, so call it in a loop with "
            "wait_seconds=30. When next_action becomes call_execute_payment, call execute_payment. "
            "Escalations expire after 15 minutes."
        )
    )
    async def get_decision(decision_id: str, wait_seconds: int = 0) -> dict:
        async def waiting(s: ATLService) -> dict:
            wait = max(0, min(int(wait_seconds or 0), MAX_WAIT_SECONDS))
            deadline = time.monotonic() + wait
            result = s.get_decision(decision_id)
            while result["status"] == "pending_owner" and time.monotonic() < deadline:
                await anyio.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
                result = s.get_decision(decision_id)
            return result

        return await call(
            "get_decision", {"decision_id": decision_id, "wait_seconds": wait_seconds}, waiting
        )

    @server.tool(
        description=(
            "Pay an approved decision. Only works when next_action is call_execute_payment. Pays exactly "
            "the registered payee key and the checked amount. Safe to call again: a second call returns "
            "the same transfer_id and receipt_id. Fails with DECISION_NOT_APPROVED otherwise."
        )
    )
    async def execute_payment(decision_id: str) -> dict:
        return await call(
            "execute_payment", {"decision_id": decision_id}, lambda s: s.execute_payment(decision_id)
        )

    @server.tool(description="Fetch a signed receipt (decision, intent, transfer, hash, signature) by id.")
    async def get_receipt(receipt_id: str) -> dict:
        return await call("get_receipt", {"receipt_id": receipt_id}, lambda s: s.get_receipt(receipt_id))

    @server.tool(
        description=(
            "Verify a receipt by id or by its full JSON: recomputes the hash and checks the ATL signature. "
            "Returns valid, reasons, payee_id, amount_cents, transfer_id and rail."
        )
    )
    async def verify_receipt(receipt_id: str | None = None, receipt: dict[str, Any] | None = None) -> dict:
        args = {"receipt_id": receipt_id, "receipt": receipt}
        return await call(
            "verify_receipt", args, lambda s: s.verify_receipt(receipt_id=receipt_id, receipt=receipt)
        )

    return server


def main() -> None:
    # L0 integration fix: the MCP SDK logs at INFO, which made httpx print every rail request line on
    # stderr into the demo console. Rail calls are already in the run log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = load_settings()
    except ATLError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        sys.exit(2)
    runlog = RunLog(runs_dir=settings.runs_dir)
    cache: dict[str, ATLService] = {}

    def get_service() -> ATLService:
        if "svc" not in cache:
            # stdin is the protocol channel: a PAY confirmation can never come through the agent.
            cache["svc"] = ATLService.from_settings(settings, input_fn=lambda: "", runlog=runlog)
        return cache["svc"]

    build_server(get_service, runlog).run()


if __name__ == "__main__":
    main()
