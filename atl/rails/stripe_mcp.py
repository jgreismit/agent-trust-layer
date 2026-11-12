"""Stripe rail (US, USD) with the Stripe MCP server as ATL's way to talk to Stripe. Sandbox only.

    agent -> ATL MCP (pay_check, execute_payment) -> rules R1..R11 + owner mandate
          -> ATL as MCP client -> Stripe MCP (https://mcp.stripe.com) -> Stripe sandbox

The paying agent never sees the Stripe key or the Stripe MCP: ATL alone is the MCP client, and it calls
`send_money` only from ATLService.execute_payment, after the rules (and the owner, on escalation) approved.

Selection: ATL_RAIL=stripe_sandbox. Transports: ATL_STRIPE_TRANSPORT=mcp (default; agent API key in
STRIPE_AGENT_KEY, Treasury tools setup_recipient / send_money / stripe_api_read) or rest (fallback in
atl/rails/stripe_rest.py; Connect transfers with STRIPE_SECRET_KEY).

Guards, before any I/O: ATL_ENV must be sandbox; hosts must be mcp.stripe.com or api.stripe.com; a key that
contains `_live_` (or lacks `_test_`) is refused with PRODUCTION_BLOCKED. The key is never logged or printed.

Idempotency: ATL's idempotency key (the decision id) indexes a local state file (var/stripe_rail.json). A
second call with the same key never reaches Stripe again: it returns the recorded payment, the recorded
pending confirmation, or (after a timeout or an unreadable answer) refuses until a lookup finds the payment.

Stripe human confirmation: when send_money answers with a Dashboard approval URL instead of paying, the
rail returns a RailTransfer with status PENDING_RAIL_CONFIRMATION and that URL. The service marks the
decision pending_rail_confirmation and shows the URL to the owner. `atl stripe confirm ID` retries with the
same arguments (plus the approval token, when the owner pastes one) and only then is the payment executed.
ATL never approves anything in Stripe.

Every shape assumption (tool arguments and results) is in atl/rails/stripe_shapes.py.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import fcntl
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from atl.config import Settings, assert_stripe_test_key, assert_stripe_url
from atl.contracts import (
    PENDING_RAIL_CONFIRMATION,
    PRODUCTION_BLOCKED,
    RAIL_ERROR,
    TRANSFER_NOT_FOUND,
    ATLError,
    PayeeRule,
    RailName,
    RailTransfer,
)
from atl.ids import new_id
from atl.rails import stripe_shapes as shapes
from atl.rails.errors import RailError, RailOutcomeUnknown
from atl.runlog import RunLog

TOOL_TIMEOUT_S = 30.0
PENDING_ID_PREFIX = "spc"  # local id of a payment held for Stripe confirmation (no Stripe id exists yet)
_HTTP_401 = re.compile(r"\b401\b|unauthori[sz]ed|www-authenticate", re.I)
_thread_lock = threading.Lock()


# ================================================================================================ transport


@runtime_checkable
class StripeTransport(Protocol):
    """How the rail reaches the Stripe MCP. Tests inject a fake; production uses MCPStripeTransport."""

    def call_tool(self, name: str, arguments: dict[str, Any]) -> shapes.ToolResult: ...

    def list_tools(self) -> list[dict[str, Any]]:
        """[{"name", "description", "input_schema"}] as the server reports them."""
        ...


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        out: list[BaseException] = []
        for e in exc.exceptions:
            out.extend(_leaves(e))
        return out
    return [exc]


def map_transport_error(exc: BaseException, *, secret: str | None = None, tool: str = "") -> RailError:
    """Map MCP SDK, HTTP and timeout failures to RailError with ATL's existing codes (RAIL_ERROR)."""
    if isinstance(exc, RailError):
        return exc
    leaves = _leaves(exc)
    where = f" ({tool})" if tool else ""

    def scrub(text: str) -> str:
        return text.replace(secret, "[REDACTED]") if secret else text

    for leaf in leaves:
        if isinstance(leaf, RailError):
            return leaf
    for leaf in leaves:
        status = getattr(getattr(leaf, "response", None), "status_code", None)
        if status == 401 or (status is None and _HTTP_401.search(str(leaf))):
            return RailError(
                RAIL_ERROR,
                f"Stripe MCP answered 401 with an OAuth challenge{where}: STRIPE_AGENT_KEY is missing, "
                "expired, or not an agent API key (from December 2026 Stripe MCP accepts only agent "
                "keys or OAuth)",
            )
    for leaf in leaves:
        name = type(leaf).__name__
        if isinstance(leaf, TimeoutError) or "Timeout" in name:
            return RailOutcomeUnknown(RAIL_ERROR, f"Stripe MCP timed out{where}")
    for leaf in leaves:
        if type(leaf).__name__ in ("MCPError", "McpError"):
            err = getattr(leaf, "error", None)
            code = getattr(err, "code", "")
            msg = getattr(err, "message", str(leaf))
            return RailError(RAIL_ERROR, scrub(f"Stripe MCP error {code}{where}: {msg}")[:400])
    leaf = leaves[0] if leaves else exc
    return RailError(RAIL_ERROR, scrub(f"Stripe MCP call failed{where}: {type(leaf).__name__}: {leaf}")[:400])


def _tool_result(result: Any) -> shapes.ToolResult:
    """CallToolResult (mcp 2.x snake_case or 1.x camelCase) -> ToolResult."""
    texts = [
        str(getattr(c, "text", ""))
        for c in (getattr(result, "content", None) or [])
        if getattr(c, "type", None) == "text"
    ]
    text = "\n".join(texts)
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    is_error = getattr(result, "is_error", None)
    if is_error is None:
        is_error = getattr(result, "isError", False)
    return shapes.ToolResult(
        is_error=bool(is_error),
        data=structured if structured is not None else shapes.parse_text_json(text),
        text=text,
    )


class MCPStripeTransport:
    """Official MCP Python SDK, streamable HTTP client to https://mcp.stripe.com, Bearer agent API key.

    ATL's service API is synchronous and may run inside the ATL MCP server's event loop, so each call runs
    its own event loop in a worker thread and opens a fresh MCP session (initialize, call, close).
    """

    def __init__(self, *, url: str, api_key: str | None, timeout_s: float = TOOL_TIMEOUT_S):
        assert_stripe_url(url)
        assert_stripe_test_key(api_key, "STRIPE_AGENT_KEY")
        self._url = url
        self._key = api_key
        self._timeout = timeout_s

    def _headers(self) -> dict[str, str]:
        if not self._key:
            raise RailError(RAIL_ERROR, "no STRIPE_AGENT_KEY configured for the Stripe MCP")
        assert_stripe_test_key(self._key, "STRIPE_AGENT_KEY")
        return {"Authorization": f"Bearer {self._key}", "User-Agent": "atl/0.1"}

    async def _with_session(self, fn: Callable[[Any], Any]) -> Any:
        import anyio
        from mcp.client import streamable_http
        from mcp.client.session import ClientSession

        headers = self._headers()
        assert_stripe_url(self._url)  # before any I/O
        with anyio.fail_after(self._timeout):
            if hasattr(streamable_http, "streamable_http_client"):  # mcp 2.x
                import httpx2

                async with httpx2.AsyncClient(
                    headers=headers, timeout=httpx2.Timeout(self._timeout)
                ) as client:
                    async with streamable_http.streamable_http_client(self._url, http_client=client) as (
                        r,
                        w,
                    ):
                        async with ClientSession(r, w) as session:
                            await session.initialize()
                            return await fn(session)
            # mcp 1.x
            async with streamable_http.streamablehttp_client(self._url, headers=headers) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    return await fn(session)

    def _run(self, fn: Callable[[Any], Any], tool: str) -> Any:
        import anyio

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(anyio.run, self._with_session, fn)
            try:
                return future.result()
            except BaseException as exc:  # noqa: BLE001 - mapped to RailError
                raise map_transport_error(exc, secret=self._key, tool=tool) from None

    def call_tool(self, name: str, arguments: dict[str, Any]) -> shapes.ToolResult:
        async def go(session: Any) -> Any:
            return await session.call_tool(name, arguments)

        return _tool_result(self._run(go, name))

    def list_tools(self) -> list[dict[str, Any]]:
        async def go(session: Any) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            result = await session.list_tools()
            for _ in range(20):
                for t in getattr(result, "tools", []) or []:
                    schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}
                    if hasattr(schema, "model_dump"):
                        schema = schema.model_dump(mode="json", by_alias=True, exclude_none=True)
                    out.append({"name": t.name, "description": t.description or "", "input_schema": schema})
                cursor = getattr(result, "next_cursor", None) or getattr(result, "nextCursor", None)
                if not cursor:
                    break
                import mcp_types

                result = await session.list_tools(params=mcp_types.PaginatedRequestParams(cursor=cursor))
            return out

        return self._run(go, "tools/list")


# ================================================================================================ backends


class StripeMcpBackend:
    """Treasury money movement through Stripe MCP tools. One rail_call log line per tool call."""

    kind = "mcp"

    def __init__(
        self,
        transport: StripeTransport,
        *,
        runlog: RunLog | None,
        rail_name: str,
        financial_account: str | None = None,
    ):
        self._t = transport
        self._runlog = runlog
        self._rail = rail_name
        self._fa = financial_account
        self._schemas: dict[str, dict] | None = None

    def _schema(self, tool: str) -> dict | None:
        if self._schemas is None:
            try:
                self._schemas = {t["name"]: t.get("input_schema") or {} for t in self._t.list_tools()}
            except RailError:
                self._schemas = {}  # schema unknown: send arguments as built
        return self._schemas.get(tool)

    def _call(self, tool: str, arguments: dict[str, Any]) -> shapes.ToolResult:
        args, dropped = shapes.fit_to_schema(arguments, self._schema(tool))
        t0 = time.perf_counter()
        result: shapes.ToolResult | None = None
        error: str | None = None
        try:
            result = self._t.call_tool(tool, args)
            return result
        except RailError as exc:
            error = exc.message
            raise
        except Exception as exc:  # noqa: BLE001 - any transport failure becomes a RailError
            mapped = map_transport_error(exc, tool=tool)
            error = mapped.message
            raise mapped from None
        finally:
            if self._runlog is not None:
                fields: dict[str, Any] = {
                    "rail": self._rail,
                    "transport": "mcp",
                    "tool": tool,
                    "request": args,
                    "ms": int((time.perf_counter() - t0) * 1000),
                }
                if dropped:
                    fields["dropped_args"] = dropped
                if result is not None:
                    fields["is_error"] = result.is_error
                    fields["response"] = result.text[:500] if result.data is None else result.data
                if error is not None:
                    fields["error"] = error
                self._runlog.write("rail_call", **fields)

    def _read(self, path: str, params: dict | None = None) -> Any:
        result = self._call(shapes.TOOL_API_READ, shapes.read_args(path, params))
        if result.is_error:
            if re.search(r"not[ _]found|resource_missing|\b404\b", result.text, re.I):
                raise RailError(TRANSFER_NOT_FOUND, f"GET {path}: not found")
            raise RailError(RAIL_ERROR, f"stripe_api_read GET {path} failed: {result.text[:300]}")
        return shapes.read_data(result)

    # ------------------------------------------------------------------ backend interface

    def destination_for(self, payee: PayeeRule) -> str:
        if not payee.stripe_recipient_id:
            raise RailError(
                RAIL_ERROR,
                f"payee {payee.payee_id} has no stripe_recipient_id; run `atl stripe setup-recipient` first",
            )
        return payee.stripe_recipient_id

    def send(
        self,
        *,
        destination: str,
        amount_cents: int,
        currency: str,
        description: str,
        idempotency_key: str,
        metadata: dict[str, str],
        approval_ref: dict[str, str] | None = None,
    ) -> shapes.SendOutcome:
        args = shapes.send_money_args(
            recipient_id=destination,
            amount_cents=amount_cents,
            currency=currency,
            description=description,
            idempotency_key=idempotency_key,
            metadata=metadata,
            financial_account=self._fa,
            approval_ref=approval_ref,
        )
        try:
            result = self._call(shapes.TOOL_SEND_MONEY, args)
        except RailOutcomeUnknown:
            raise
        outcome = shapes.parse_send_money(result)
        if outcome.kind == "error":
            raise RailError(RAIL_ERROR, f"Stripe send_money refused: {outcome.message}")
        if outcome.kind == "unknown":
            raise RailOutcomeUnknown(
                RAIL_ERROR, f"Stripe send_money answer not understood: {outcome.message}"
            )
        return outcome

    def find_payment(self, idempotency_key: str) -> shapes.PaymentView | None:
        return shapes.find_outbound_payment(
            self._read(shapes.OUTBOUND_PAYMENTS_PATH, {"limit": 50}), idempotency_key
        )

    def get_payment(self, payment_id: str) -> shapes.PaymentView:
        if not payment_id.startswith("obp_") or "/" in payment_id:
            raise RailError(TRANSFER_NOT_FOUND, "invalid outbound payment id")
        view = shapes.parse_outbound_payment(self._read(shapes.OUTBOUND_PAYMENT_PATH.format(id=payment_id)))
        if view is None:
            raise RailError(RAIL_ERROR, "stripe_api_read: unexpected outbound payment body")
        return view

    def balance_cents(self, currency: str) -> int:
        if self._fa:
            data = self._read(shapes.FINANCIAL_ACCOUNT_PATH.format(id=self._fa))
        else:
            data = self._read(shapes.BALANCE_PATH)
        cents = shapes.parse_balance(data, currency)
        if cents is None:
            raise RailError(RAIL_ERROR, "balance missing in stripe_api_read response")
        return cents

    def setup_recipient(
        self, *, display_name: str, email: str, country: str, entity_type: str
    ) -> shapes.RecipientSetup:
        result = self._call(
            shapes.TOOL_SETUP_RECIPIENT,
            shapes.setup_recipient_args(
                display_name=display_name, email=email, country=country, entity_type=entity_type
            ),
        )
        if result.is_error:
            raise RailError(RAIL_ERROR, f"Stripe setup_recipient refused: {result.text[:300]}")
        setup = shapes.parse_recipient(result)
        if setup is None:
            raise RailError(RAIL_ERROR, f"setup_recipient answer has no recipient id: {result.text[:300]}")
        return setup

    def list_tools(self) -> list[dict[str, Any]]:
        return self._t.list_tools()

    def close(self) -> None:
        pass


# ================================================================================================ state


class StripeState:
    """JSON state (var/stripe_rail.json): {"payments": {idempotency_key: rec}, "recipients": {payee_id: rec}}.

    Read-modify-write under a thread lock and an fcntl file lock; atomic rewrite. Same pattern as the
    simulated rail.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock_path = self.path.with_name(self.path.name + ".lock")

    @contextlib.contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _thread_lock, self._lock_path.open("a") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                state = self._read()
                yield state
                self._write(state)
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def snapshot(self) -> dict[str, Any]:
        with self.locked() as state:
            return json.loads(json.dumps(state))

    def _read(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {}
        state.setdefault("payments", {})
        state.setdefault("recipients", {})
        return state

    def _write(self, state: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".stripe_rail.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


# ================================================================================================ rail


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class StripeMCPRail:
    """Rail protocol plus DestinationRail (pay_destination, confirm_payment). Name "stripe_sandbox", USD."""

    name: RailName = "stripe_sandbox"
    currency = "USD"

    def __init__(
        self,
        settings: Settings,
        runlog: RunLog | None = None,
        *,
        transport: StripeTransport | None = None,
        http_client: Any = None,
        state_path: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if settings.env != "sandbox":
            raise ATLError(PRODUCTION_BLOCKED, "the Stripe rail runs only with ATL_ENV=sandbox")
        self._settings = settings
        self._runlog = runlog if runlog is not None else RunLog(runs_dir=settings.runs_dir)
        self.transport_kind = settings.stripe_transport
        self.state = StripeState(state_path or Path(settings.var_dir) / "stripe_rail.json")
        if self.transport_kind == "rest":
            from atl.rails.stripe_rest import StripeRestBackend

            self._backend: Any = StripeRestBackend(
                base_url=settings.stripe_api_base,
                api_key=settings.stripe_secret_key,
                runlog=self._runlog,
                rail_name=self.name,
                client=http_client,
                sleep=sleep,
            )
        else:
            assert_stripe_url(settings.stripe_mcp_url)
            assert_stripe_test_key(settings.stripe_agent_key, "STRIPE_AGENT_KEY")
            self._backend = StripeMcpBackend(
                transport
                or MCPStripeTransport(url=settings.stripe_mcp_url, api_key=settings.stripe_agent_key),
                runlog=self._runlog,
                rail_name=self.name,
                financial_account=settings.stripe_financial_account,
            )

    def _log(self, event: str, **fields: Any) -> None:
        self._runlog.write(event, rail=self.name, transport=self.transport_kind, **fields)

    # ------------------------------------------------------------------ conversions

    def _transfer(self, rec: dict[str, Any]) -> RailTransfer:
        pending = rec["status"] == "pending_confirmation"
        return RailTransfer(
            rail=self.name,
            transfer_id=rec["local_id"] if pending else rec["stripe_id"],
            status=PENDING_RAIL_CONFIRMATION if pending else rec["stripe_status"],
            amount_cents=rec["amount_cents"],
            pix_key="",
            created_at=datetime.fromisoformat(rec["created_at"]),
            destination=rec["destination"],
            currency=rec["currency"],
            confirmation_url=rec.get("confirmation_url") if pending else None,
        )

    def _record_outcome(self, rec: dict[str, Any], outcome: shapes.SendOutcome) -> None:
        if outcome.kind == "confirmation_required":
            rec["status"] = "pending_confirmation"
            rec["confirmation_url"] = outcome.confirmation_url
            rec["approval_ref"] = outcome.approval_ref
            rec["held_at"] = _now_iso()
            self._log(
                "stripe_confirmation_required",
                local_id=rec["local_id"],
                idempotency_key=rec["idempotency_key"],
                confirmation_url=outcome.confirmation_url,
                note="the Stripe account owner approves this payment in the Dashboard; ATL never approves",
            )
            return
        if outcome.amount_cents is not None and outcome.amount_cents != rec["amount_cents"]:
            self._log(
                "stripe_amount_mismatch",
                stripe_id=outcome.payment_id,
                expected=rec["amount_cents"],
                got=outcome.amount_cents,
            )
        rec["status"] = "sent"
        rec["stripe_id"] = outcome.payment_id
        rec["stripe_status"] = outcome.status or "processing"
        rec["sent_at"] = _now_iso()

    def _recover(self, rec: dict[str, Any]) -> bool:
        """After an uncertain attempt: look the payment up by ATL's idempotency key. True when found."""
        try:
            found = self._backend.find_payment(rec["idempotency_key"])
        except RailError as exc:
            self._log("stripe_lookup_failed", idempotency_key=rec["idempotency_key"], error=exc.message)
            return False
        if found is None:
            return False
        rec.update(status="sent", stripe_id=found.payment_id, stripe_status=found.status, sent_at=_now_iso())
        self._log(
            "stripe_payment_recovered", idempotency_key=rec["idempotency_key"], stripe_id=found.payment_id
        )
        return True

    # ------------------------------------------------------------------ DestinationRail

    def pay_destination(
        self,
        *,
        payee: PayeeRule,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        description: str,
        metadata: dict[str, str],
    ) -> RailTransfer:
        if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
            raise RailError(RAIL_ERROR, "amount must be positive integer cents")
        if not idempotency_key:
            raise RailError(RAIL_ERROR, "idempotency_key is required")
        if currency.upper() != self.currency:
            raise RailError(RAIL_ERROR, f"the Stripe rail pays {self.currency} only, not {currency}")
        destination = self._backend.destination_for(payee)
        meta = {"atl_idempotency_key": idempotency_key, **metadata}
        with self.state.locked() as state:
            rec = state["payments"].get(idempotency_key)
            if rec is not None:
                if rec["destination"] != destination or rec["amount_cents"] != amount_cents:
                    raise RailError(RAIL_ERROR, "idempotency key already used for a different payment")
                if rec["status"] in ("sent", "pending_confirmation"):
                    self._log("stripe_replay", idempotency_key=idempotency_key, status=rec["status"])
                    return self._transfer(rec)
                # "uncertain": never send again; only a lookup can settle it.
                if self._recover(rec):
                    return self._transfer(rec)
                raise RailError(
                    RAIL_ERROR,
                    "an earlier Stripe attempt for this payment has an unknown outcome and no payment "
                    "was found; ATL will not send again. Check the Stripe Dashboard.",
                )
            rec = {
                "idempotency_key": idempotency_key,
                "local_id": new_id(PENDING_ID_PREFIX),
                "status": "uncertain",  # written before the call: a crash mid-call is never resent
                "destination": destination,
                "payee_id": payee.payee_id,
                "amount_cents": amount_cents,
                "currency": self.currency,
                "description": description,
                "metadata": meta,
                "transport": self.transport_kind,
                "created_at": _now_iso(),
            }
            state["payments"][idempotency_key] = rec
        try:
            outcome = self._backend.send(
                destination=destination,
                amount_cents=amount_cents,
                currency=self.currency,
                description=description,
                idempotency_key=idempotency_key,
                metadata=meta,
            )
        except RailOutcomeUnknown as exc:
            with self.state.locked() as state:
                rec = state["payments"][idempotency_key]
                if self._recover(rec):
                    return self._transfer(rec)
            self._log("stripe_outcome_unknown", idempotency_key=idempotency_key, error=exc.message)
            raise
        except RailError:
            with self.state.locked() as state:  # definite refusal: nothing moved, a later retry may send
                state["payments"].pop(idempotency_key, None)
            raise
        with self.state.locked() as state:
            rec = state["payments"][idempotency_key]
            self._record_outcome(rec, outcome)
            return self._transfer(rec)

    def confirm_payment(self, idempotency_key: str, approval_token: str | None = None) -> RailTransfer:
        """Owner side: after approving in the Stripe Dashboard, retry the held send_money with the same
        arguments (plus the approval token when given). Never approves anything itself."""
        snap = self.state.snapshot()["payments"].get(idempotency_key)
        if snap is None:
            raise RailError(TRANSFER_NOT_FOUND, f"no Stripe payment recorded for {idempotency_key}")
        if snap["status"] == "sent":
            return self._transfer(snap)
        if snap["status"] != "pending_confirmation":
            raise RailError(RAIL_ERROR, f"payment is {snap['status']}, not pending Stripe confirmation")
        approval = dict(snap.get("approval_ref") or {})
        if approval_token:
            approval["approval_token"] = approval_token
        try:
            outcome = self._backend.send(
                destination=snap["destination"],
                amount_cents=snap["amount_cents"],
                currency=snap["currency"],
                description=snap["description"],
                idempotency_key=idempotency_key,
                metadata=snap["metadata"],
                approval_ref=approval,
            )
        except RailOutcomeUnknown as exc:
            with self.state.locked() as state:
                rec = state["payments"][idempotency_key]
                rec["status"] = "uncertain"
                if self._recover(rec):
                    return self._transfer(rec)
            self._log("stripe_outcome_unknown", idempotency_key=idempotency_key, error=exc.message)
            raise
        with self.state.locked() as state:
            rec = state["payments"][idempotency_key]
            self._record_outcome(rec, outcome)
            return self._transfer(rec)

    # ------------------------------------------------------------------ Rail protocol

    def balance_cents(self) -> int:
        return self._backend.balance_cents(self.currency)

    def transfer_pix(self, **_: Any) -> RailTransfer:
        raise RailError(
            RAIL_ERROR, "the Stripe rail pays registered Stripe destinations in USD, not Pix keys"
        )

    def get_transfer(self, transfer_id: str) -> RailTransfer:
        if transfer_id.startswith(PENDING_ID_PREFIX + "_"):
            for rec in self.state.snapshot()["payments"].values():
                if rec.get("local_id") == transfer_id:
                    if rec["status"] == "sent":
                        transfer_id = rec["stripe_id"]
                        break
                    return self._transfer(rec)
            else:
                raise RailError(TRANSFER_NOT_FOUND, f"transfer {transfer_id} not found")
        view = self._backend.get_payment(transfer_id)
        return RailTransfer(
            rail=self.name,
            transfer_id=view.payment_id,
            status=view.status,
            amount_cents=view.amount_cents,
            pix_key="",
            created_at=view.created_at,
            destination=view.destination,
            currency=view.currency,
        )

    # ------------------------------------------------------------------ owner helpers (not protocol)

    def setup_recipient(
        self,
        *,
        payee_id: str,
        display_name: str,
        email: str,
        country: str = "US",
        entity_type: str = "company",
    ) -> dict[str, Any]:
        setup = self._backend.setup_recipient(
            display_name=display_name, email=email, country=country, entity_type=entity_type
        )
        rec = {
            "payee_id": payee_id,
            "recipient_id": setup.recipient_id,
            "display_name": display_name,
            "email": email,
            "country": country,
            "onboarding_url": setup.onboarding_url,
            "created_at": _now_iso(),
        }
        with self.state.locked() as state:
            state["recipients"][payee_id] = rec
        self._log("stripe_recipient_setup", payee_id=payee_id, recipient_id=setup.recipient_id)
        return rec

    def recipients(self) -> dict[str, dict[str, Any]]:
        return self.state.snapshot()["recipients"]

    def pending_confirmations(self) -> list[dict[str, Any]]:
        return [
            r for r in self.state.snapshot()["payments"].values() if r["status"] == "pending_confirmation"
        ]

    def list_tools(self) -> list[dict[str, Any]]:
        if self.transport_kind != "mcp":
            raise RailError(RAIL_ERROR, "list_tools needs the MCP transport")
        return self._backend.list_tools()

    def close(self) -> None:
        self._backend.close()
