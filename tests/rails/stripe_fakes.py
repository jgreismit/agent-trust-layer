"""Offline fakes for the Stripe rail: a scripted Stripe MCP transport and settings helpers. No network."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from atl.config import SANDBOX_BASE_URL, Settings
from atl.rails.stripe_shapes import ToolResult

TEST_AGENT_KEY = "rk_test_" + "a" * 40
TEST_SECRET_KEY = "sk_test_" + "b" * 40
RECIPIENT = "acct_test_acme0001"
CONNECT_ACCOUNT = "acct_1TestConnect01"
FORGED = "acct_test_attacker99"
CONFIRM_URL = "https://dashboard.stripe.com/test/approvals/apr_test_123"

SCHEMAS = [
    {
        "name": "setup_recipient",
        "description": "Sets up a recipient account",
        "input_schema": {
            "type": "object",
            "properties": {k: {"type": "string"} for k in ("name", "email", "country", "entity_type")},
        },
    },
    {
        "name": "send_money",
        "description": "Sends money from a Treasury account to a recipient (Human confirmation)",
        "input_schema": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string"},
                "amount": {"type": "integer"},
                "currency": {"type": "string"},
                "description": {"type": "string"},
                "metadata": {"type": "object"},
                "idempotency_key": {"type": "string"},
                "approval_token": {"type": "string"},
            },
            "required": ["recipient", "amount", "currency"],
        },
    },
    {
        "name": "stripe_api_read",
        "description": "Read data with any Stripe API GET method",
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {"type": "string"},
                "path": {"type": "string"},
                "params": {"type": "object"},
            },
        },
    },
]


def ok(data: Any) -> ToolResult:
    return ToolResult(is_error=False, data=data, text=json.dumps(data))


def err(text: str) -> ToolResult:
    return ToolResult(is_error=True, data=None, text=text)


def obp(amount: int, *, pid: str = "obp_test_0001", status: str = "processing", **extra: Any) -> dict:
    return {
        "id": pid,
        "object": "v2.money_management.outbound_payment",
        "amount": {"value": amount, "currency": "usd"},
        "to": {"recipient": RECIPIENT},
        "status": status,
        "created": "2026-11-12T15:00:00.000Z",
        **extra,
    }


class FakeStripeMCP:
    """Scripted stand-in for MCPStripeTransport. `script[tool]` is a list of responses consumed in order
    (the last one repeats); a response is a ToolResult, an exception to raise, or a callable(arguments)."""

    def __init__(self, script: dict[str, list[Any]] | None = None, schemas: list[dict] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.list_calls = 0
        self.schemas = SCHEMAS if schemas is None else schemas
        self.script: dict[str, list[Any]] = {
            "send_money": [lambda a: ok(obp(a["amount"]))],
            "setup_recipient": [ok({"id": RECIPIENT, "object": "v2.core.account"})],
            "stripe_api_read": [self._default_read],
        }
        self.script.update(script or {})

    @staticmethod
    def _default_read(args: dict) -> ToolResult:
        path = args.get("path", "")
        if path == "/v1/balance":
            return ok({"object": "balance", "available": [{"amount": 500000, "currency": "usd"}]})
        if path.startswith("/v2/money_management/outbound_payments/"):
            return ok(obp(4200, pid=path.rsplit("/", 1)[1], status="posted"))
        if path == "/v2/money_management/outbound_payments":
            return ok({"data": []})
        return err("resource_missing: not found")

    def calls_to(self, tool: str) -> list[dict]:
        return [a for n, a in self.calls if n == tool]

    def list_tools(self) -> list[dict]:
        self.list_calls += 1
        return self.schemas

    def call_tool(self, name: str, arguments: dict) -> ToolResult:
        self.calls.append((name, json.loads(json.dumps(arguments))))
        queue = self.script.get(name)
        if not queue:
            return err(f"unknown tool {name}")
        resp = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(resp, BaseException):
            raise resp
        if callable(resp):
            resp = resp(arguments)
        return resp


def stripe_settings(tmp_path: Path, **over: Any) -> Settings:
    data: dict[str, Any] = dict(
        env="sandbox",
        rail="stripe",
        base_url=SANDBOX_BASE_URL,
        api_key=None,
        root=tmp_path,
        var_dir=tmp_path / "var",
        db_path=tmp_path / "var" / "atl.db",
        keys_dir=tmp_path / "var" / "keys",
        runs_dir=tmp_path / "runs",
        run_id="run_test",
        stripe_agent_key=TEST_AGENT_KEY,
        stripe_secret_key=TEST_SECRET_KEY,
    )
    data.update(over)
    return Settings(**data)


def us_mandate(**over: Any) -> dict:
    data: dict[str, Any] = dict(
        mandate_id="mdt_us_demo",
        owner_id="owner-us",
        agent_id="procurement-us-01",
        currency="USD",
        payees=[
            dict(
                payee_id="acme-supply",
                display_name="Acme Supply Co",
                stripe_recipient_id=RECIPIENT,
                stripe_account_id=CONNECT_ACCOUNT,
                categories=["materials"],
            )
        ],
        per_payment_cap_cents=50000,
        daily_cap_cents=150000,
        hard_cap_cents=200000,
        valid_until=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    data.update(over)
    return data


def us_intent(**over: Any) -> dict:
    data: dict[str, Any] = dict(
        mandate_id="mdt_us_demo",
        agent_id="procurement-us-01",
        payee_id="acme-supply",
        destination=RECIPIENT,
        currency="USD",
        amount_cents=4200,
        category="materials",
        invoice_ref="INV-US-1001",
        memo="Lumber delivery, PO 7781",
        idempotency_key="idem-us-1",
    )
    data.update(over)
    return data


Handler = Callable[[dict], ToolResult]
