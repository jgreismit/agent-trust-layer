"""Asaas rail (sandbox by default). Sandbox host guard before every request.

Units: Asaas uses decimal BRL. Conversion happens only here: cents = round(value * 100), value = cents / 100.
The API key is sent in the `access_token` header and never logged (the run log also redacts).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from atl.config import Settings, assert_sandbox_url
from atl.contracts import RAIL_DUPLICATE_TRANSFER, RAIL_ERROR, TRANSFER_NOT_FOUND, RailName, RailTransfer
from atl.rails.errors import RailError
from atl.runlog import RunLog

USER_AGENT = "atl/0.1"
TIMEOUT_S = 20.0
GET_TRIES = 3
BACKOFF_S = (0.5, 1.5)
POST_TRANSFER_TRIES = 2
LOOKUP_MAX_PAGES = 5
SANDBOX_TOKEN_NOTE = (
    "Asaas sandbox holds API transfers for critical-action token authorization (sandbox token 000000). "
    "POST /transfers has no token field and account settings must not change, so the transfer stays "
    "PENDING until the owner authorizes it in the sandbox UI. The debit is immediate."
)
_SUMMARY_KEYS = (
    "id",
    "object",
    "status",
    "value",
    "netValue",
    "balance",
    "authorized",
    "transferFee",
    "externalReference",
    "totalCount",
    "hasMore",
    "general",
    "commercialInfo",
    "bankAccountInfo",
    "documentation",
    "errors",
    "deleted",
)


def cents_to_value(cents: int) -> float:
    if not isinstance(cents, int) or isinstance(cents, bool):
        raise RailError(RAIL_ERROR, "amount must be integer cents")
    return cents / 100


def value_to_cents(value: Any) -> int:
    if value is None:
        return 0
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _summary(data: Any) -> Any:
    if isinstance(data, dict):
        out = {k: data[k] for k in _SUMMARY_KEYS if k in data}
        if isinstance(data.get("data"), list):
            out["data_len"] = len(data["data"])
            out["data_ids"] = [d.get("id") for d in data["data"][:10] if isinstance(d, dict)]
        return out
    if isinstance(data, str):
        return data[:300]
    return data


def error_descriptions(data: Any) -> str:
    """Asaas error body: {"errors": [{"code": "...", "description": "..."}]}."""
    if isinstance(data, dict) and isinstance(data.get("errors"), list):
        parts = []
        for e in data["errors"]:
            if isinstance(e, dict):
                code = e.get("code") or ""
                desc = e.get("description") or ""
                parts.append(f"{code}: {desc}" if code else desc)
        if parts:
            return "; ".join(parts)
    if isinstance(data, str) and data:
        return data[:300]
    return "no error description"


def _parse_date(value: Any) -> datetime:
    if isinstance(value, str) and value:
        try:
            if len(value) == 10:
                d = date.fromisoformat(value)
                return datetime(d.year, d.month, d.day, tzinfo=UTC)
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


class AsaasRail:
    def __init__(
        self,
        settings: Settings,
        runlog: RunLog | None = None,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._settings = settings
        self._production = settings.env == "production"
        self._base_url = settings.base_url.rstrip("/")
        self._guard(self._base_url)
        self.name: RailName = "asaas_production" if self._production else "asaas_sandbox"
        self._runlog = runlog if runlog is not None else RunLog(runs_dir=settings.runs_dir)
        self._client = client or httpx.Client(timeout=TIMEOUT_S)
        self._sleep = sleep

    # ------------------------------------------------------------------ plumbing

    def _guard(self, url: str) -> None:
        if not self._production:
            assert_sandbox_url(url)

    def _headers(self) -> dict[str, str]:
        key = self._settings.api_key
        if not key:
            raise RailError(RAIL_ERROR, "no Asaas API key configured")
        return {
            "access_token": key,
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _send(
        self, method: str, path: str, *, params: dict | None = None, body: dict | None = None
    ) -> tuple[int | None, Any, Exception | None]:
        """One HTTP request and one runlog line. Returns (status, data, transport_exception)."""
        url = self._base_url + path
        self._guard(url)  # before any I/O
        headers = self._headers()
        t0 = time.perf_counter()
        status: int | None = None
        data: Any = None
        exc: Exception | None = None
        try:
            resp = self._client.request(method, url, params=params, json=body, headers=headers)
            status = resp.status_code
            try:
                data = resp.json() if resp.content else None
            except ValueError:
                data = resp.text[:500]
        except httpx.HTTPError as e:  # timeouts and transport errors
            exc = e
        ms = int((time.perf_counter() - t0) * 1000)
        fields: dict[str, Any] = {
            "rail": self.name,
            "method": method,
            "path": path,
            "status": status,
            "ms": ms,
            "request": body,
            "response": _summary(data),
        }
        if params:
            fields["params"] = params
        if exc is not None:
            fields["error"] = type(exc).__name__
        self._runlog.write("rail_call", **fields)
        return status, data, exc

    def _get(self, path: str, params: dict | None = None) -> tuple[int, Any]:
        """GET with retries on 5xx and transport errors (3 tries, backoff)."""
        last = ""
        for attempt in range(GET_TRIES):
            status, data, exc = self._send("GET", path, params=params)
            if exc is None and status is not None and status < 500:
                return status, data
            last = f"{type(exc).__name__}" if exc else f"HTTP {status}: {error_descriptions(data)}"
            if attempt < GET_TRIES - 1:
                self._sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
        raise RailError(RAIL_ERROR, f"GET {path} failed after {GET_TRIES} tries: {last}")

    def _get_ok(self, path: str, params: dict | None = None) -> Any:
        status, data = self._get(path, params)
        if status >= 400:
            raise RailError(RAIL_ERROR, f"GET {path} HTTP {status}: {error_descriptions(data)}")
        return data

    def _post_ok(self, path: str, body: dict) -> Any:
        """POST without retry (non-idempotent)."""
        status, data, exc = self._send("POST", path, body=body)
        if exc is not None:
            raise RailError(RAIL_ERROR, f"POST {path} failed: {type(exc).__name__}")
        if status is None or status >= 400:
            raise RailError(RAIL_ERROR, f"POST {path} HTTP {status}: {error_descriptions(data)}")
        return data

    def _to_transfer(
        self, data: dict, *, pix_key: str = "", created_at: datetime | None = None
    ) -> RailTransfer:
        bank = data.get("bankAccount") or {}
        return RailTransfer(
            rail=self.name,
            transfer_id=str(data["id"]),
            status=str(data.get("status") or "UNKNOWN"),
            amount_cents=value_to_cents(data.get("value")),
            pix_key=bank.get("pixAddressKey") or pix_key,
            created_at=created_at or _parse_date(data.get("dateCreated")),
            fee_cents=value_to_cents(data.get("transferFee")),
        )

    def _note_authorization(self, data: dict) -> None:
        if data.get("authorized") is False:
            if self._production:
                # Never use the sandbox token in production; the owner authorizes in the Asaas app.
                self._runlog.write(
                    "critical_action_pending",
                    rail=self.name,
                    transfer_id=data.get("id"),
                    status=data.get("status"),
                    note="owner must authorize this transfer in the Asaas app",
                )
            else:
                self._runlog.write(
                    "sandbox_critical_action_pending",
                    rail=self.name,
                    transfer_id=data.get("id"),
                    status=data.get("status"),
                    note=SANDBOX_TOKEN_NOTE,
                )

    # ------------------------------------------------------------------ Rail protocol

    def balance_cents(self) -> int:
        data = self._get_ok("/finance/balance")
        if not isinstance(data, dict) or "balance" not in data:
            raise RailError(RAIL_ERROR, "balance missing in response")
        return value_to_cents(data["balance"])

    def transfer_pix(
        self,
        *,
        pix_key: str,
        pix_key_type: str,
        amount_cents: int,
        idempotency_key: str,
        description: str,
    ) -> RailTransfer:
        value = cents_to_value(amount_cents)
        if amount_cents <= 0:
            raise RailError(RAIL_ERROR, "amount must be positive")
        if not idempotency_key:
            raise RailError(RAIL_ERROR, "idempotency_key is required")
        body = {
            "value": value,
            "operationType": "PIX",
            "pixAddressKey": pix_key,
            "pixAddressKeyType": pix_key_type,
            "description": description,
            "externalReference": idempotency_key,
        }
        last = ""
        for attempt in range(POST_TRANSFER_TRIES):
            created_at = datetime.now(UTC)
            status, data, exc = self._send("POST", "/transfers", body=body)
            if exc is None and status is not None and status < 400 and isinstance(data, dict):
                self._note_authorization(data)
                return self._to_transfer(data, pix_key=pix_key, created_at=created_at)
            if status == 409 and exc is None:
                # Asaas refuses a transfer with the same amount and key as an earlier one, PENDING or DONE
                # (live 2026-10-28: blocked by a DONE transfer from the same day). Nothing was debited.
                raise RailError(
                    RAIL_DUPLICATE_TRANSFER,
                    "Asaas refused this transfer as a duplicate of an earlier transfer with the same amount "
                    f"and Pix key; no money moved. Do not retry. ({error_descriptions(data)})",
                )
            if exc is None and status is not None and status < 500:
                raise RailError(RAIL_ERROR, f"POST /transfers HTTP {status}: {error_descriptions(data)}")
            # 5xx or timeout: the transfer may exist. Look it up before any retry.
            last = type(exc).__name__ if exc else f"HTTP {status}: {error_descriptions(data)}"
            self._runlog.write(
                "rail_transfer_uncertain",
                rail=self.name,
                attempt=attempt + 1,
                external_reference=idempotency_key,
                reason=last,
            )
            found = self.find_transfer_by_external_reference(idempotency_key)
            if found is not None:
                self._runlog.write(
                    "rail_transfer_recovered",
                    rail=self.name,
                    transfer_id=found.get("id"),
                    external_reference=idempotency_key,
                )
                self._note_authorization(found)
                return self._to_transfer(found, pix_key=pix_key, created_at=created_at)
            if attempt < POST_TRANSFER_TRIES - 1:
                self._sleep(BACKOFF_S[0])
        raise RailError(RAIL_ERROR, f"POST /transfers failed and no transfer found: {last}")

    def get_transfer(self, transfer_id: str) -> RailTransfer:
        if not transfer_id or "/" in transfer_id:
            raise RailError(TRANSFER_NOT_FOUND, "invalid transfer id")
        status, data = self._get(f"/transfers/{transfer_id}")
        if status == 404:
            raise RailError(TRANSFER_NOT_FOUND, f"transfer {transfer_id} not found")
        if status >= 400 or not isinstance(data, dict):
            raise RailError(RAIL_ERROR, f"GET /transfers/{{id}} HTTP {status}: {error_descriptions(data)}")
        return self._to_transfer(data)

    # ------------------------------------------------------------------ helpers (not protocol)

    def transfer_authorized(self, transfer_id: str) -> bool:
        """True once the owner typed the critical-action token. Asaas keeps the status PENDING after that
        while the transfer waits to be processed, so the status alone cannot tell."""
        if not transfer_id or "/" in transfer_id:
            raise RailError(TRANSFER_NOT_FOUND, "invalid transfer id")
        return self._get_ok(f"/transfers/{transfer_id}").get("authorized") is True

    def find_transfer_by_external_reference(
        self, external_reference: str, since: date | None = None
    ) -> dict | None:
        """Find a transfer by externalReference.

        GET /transfers documents only dateCreated, transferDate and type filters. We pass
        externalReference anyway (see ASAAS_NOTES.md, L1 findings) plus a recent dateCreated window
        (default: yesterday UTC, to cover the Brazil local date), and always match externalReference
        client side, paging through results.
        """
        since = since or (datetime.now(UTC) - timedelta(days=1)).date()
        offset = 0
        for _ in range(LOOKUP_MAX_PAGES):
            params = {
                "externalReference": external_reference,
                "dateCreated[ge]": since.isoformat(),
                "limit": 100,
                "offset": offset,
            }
            data = self._get_ok("/transfers", params)
            items = (data.get("data") or []) if isinstance(data, dict) else []
            for item in items:
                if isinstance(item, dict) and item.get("externalReference") == external_reference:
                    return item
            if not (isinstance(data, dict) and data.get("hasMore")):
                return None
            offset += len(items) or 100
        return None

    def list_transfers(self, limit: int = 10, offset: int = 0, **filters: Any) -> dict:
        return self._get_ok("/transfers", {"limit": limit, "offset": offset, **filters})

    def list_all_transfers(self, max_pages: int = 20, **filters: Any) -> list[dict]:
        out: list[dict] = []
        offset = 0
        for _ in range(max_pages):
            data = self.list_transfers(limit=100, offset=offset, **filters)
            items = [i for i in (data.get("data") or []) if isinstance(i, dict)]
            out.extend(items)
            if not data.get("hasMore") or not items:
                break
            offset += len(items)
        return out

    def pending_atl_transfers(self, prefix: str = "dec_") -> list[dict]:
        """PENDING transfers whose externalReference starts with `prefix` (ATL execute_payment) and that the
        owner has not authorized. An authorized transfer stays PENDING while Asaas processes it (live
        2026-10-28: about 2 minutes) and must never be cancelled: it is the run's payment evidence."""
        return [
            t
            for t in self.list_all_transfers()
            if t.get("status") == "PENDING"
            and t.get("authorized") is not True
            and str(t.get("externalReference") or "").startswith(prefix)
        ]

    def cancel_transfer(self, transfer_id: str) -> dict:
        """DELETE /v3/transfers/{id}/cancel (docs: cancelar-uma-transferencia). Refunds the debit."""
        if not transfer_id or "/" in transfer_id:
            raise RailError(TRANSFER_NOT_FOUND, "invalid transfer id")
        path = f"/transfers/{transfer_id}/cancel"
        status, data, exc = self._send("DELETE", path)
        if exc is not None:
            raise RailError(RAIL_ERROR, f"DELETE {path} failed: {type(exc).__name__}")
        if status == 404:
            raise RailError(TRANSFER_NOT_FOUND, f"transfer {transfer_id} not found")
        if status is None or status >= 400:
            raise RailError(RAIL_ERROR, f"DELETE {path} HTTP {status}: {error_descriptions(data)}")
        return data if isinstance(data, dict) else {}

    def account_status(self) -> dict:
        return self._get_ok("/myAccount/status")

    def ensure_customer(self, external_reference: str, name: str, cpf_cnpj: str) -> str:
        data = self._get_ok("/customers", {"externalReference": external_reference})
        for c in (data or {}).get("data") or []:
            if c.get("externalReference") == external_reference and not c.get("deleted"):
                return str(c["id"])
        created = self._post_ok(
            "/customers", {"name": name, "cpfCnpj": cpf_cnpj, "externalReference": external_reference}
        )
        return str(created["id"])

    def create_pix_charge(
        self,
        customer_id: str,
        value_cents: int,
        due_date: date,
        external_reference: str,
        description: str,
    ) -> dict:
        return self._post_ok(
            "/payments",
            {
                "customer": customer_id,
                "billingType": "PIX",
                "value": cents_to_value(value_cents),
                "dueDate": due_date.isoformat(),
                "description": description,
                "externalReference": external_reference,
            },
        )

    def confirm_sandbox_payment(self, payment_id: str) -> dict:
        if self._production:
            raise RailError(RAIL_ERROR, "sandbox payment confirmation is sandbox only")
        return self._post_ok(f"/sandbox/payment/{payment_id}/confirm", {})

    def close(self) -> None:
        self._client.close()
