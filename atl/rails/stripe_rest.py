"""Stripe REST fallback (ATL_STRIPE_TRANSPORT=rest): Connect transfers to a test connected account.

Exists because the Stripe MCP Treasury tools are public preview and may not be enabled on a new sandbox.
POST /v1/transfers (form encoded, Idempotency-Key = ATL decision id), GET /v1/transfers/{id},
GET /v1/transfers?transfer_group=..., GET /v1/balance. Test-mode secret or restricted keys only
(STRIPE_SECRET_KEY); the host and key guards run before every request. The key is never logged.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from atl.config import assert_stripe_test_key, assert_stripe_url
from atl.contracts import RAIL_DUPLICATE_TRANSFER, RAIL_ERROR, TRANSFER_NOT_FOUND, PayeeRule
from atl.rails import stripe_shapes as shapes
from atl.rails.errors import RailError, RailOutcomeUnknown
from atl.runlog import RunLog

TIMEOUT_S = 20.0
POST_TRIES = 2  # safe: Stripe replays the same result for the same Idempotency-Key
GET_TRIES = 3
BACKOFF_S = (0.5, 1.5)
USER_AGENT = "atl/0.1"


class StripeRestBackend:
    kind = "rest"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        runlog: RunLog | None,
        rail_name: str,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._base = base_url.rstrip("/")
        assert_stripe_url(self._base)
        assert_stripe_test_key(api_key, "STRIPE_SECRET_KEY")
        self._key = api_key
        self._runlog = runlog
        self._rail = rail_name
        self._client = client or httpx.Client(timeout=TIMEOUT_S)
        self._sleep = sleep

    # ------------------------------------------------------------------ plumbing

    def _headers(self, idempotency_key: str | None) -> dict[str, str]:
        if not self._key:
            raise RailError(RAIL_ERROR, "no STRIPE_SECRET_KEY configured for the Stripe REST fallback")
        assert_stripe_test_key(self._key, "STRIPE_SECRET_KEY")
        h = {"Authorization": f"Bearer {self._key}", "User-Agent": USER_AGENT, "Accept": "application/json"}
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        return h

    def _send(
        self,
        method: str,
        path: str,
        *,
        form: dict | None = None,
        params: dict | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[int | None, Any, Exception | None]:
        url = self._base + path
        assert_stripe_url(url)  # before any I/O
        headers = self._headers(idempotency_key)
        t0 = time.perf_counter()
        status: int | None = None
        data: Any = None
        exc: Exception | None = None
        try:
            resp = self._client.request(method, url, data=form, params=params, headers=headers)
            status = resp.status_code
            try:
                data = resp.json() if resp.content else None
            except ValueError:
                data = resp.text[:500]
        except httpx.HTTPError as e:
            exc = e
        if self._runlog is not None:
            fields: dict[str, Any] = {
                "rail": self._rail,
                "transport": "rest",
                "method": method,
                "path": path,
                "status": status,
                "ms": int((time.perf_counter() - t0) * 1000),
                "request": form,
                "response": {k: data.get(k) for k in ("id", "object", "amount", "currency", "error")}
                if isinstance(data, dict)
                else data,
            }
            if params:
                fields["params"] = params
            if exc is not None:
                fields["error"] = type(exc).__name__
            self._runlog.write("rail_call", **fields)
        return status, data, exc

    def _get_ok(self, path: str, params: dict | None = None) -> Any:
        last = ""
        for attempt in range(GET_TRIES):
            status, data, exc = self._send("GET", path, params=params)
            if exc is None and status is not None and status < 500:
                if status == 401:
                    raise RailError(RAIL_ERROR, "Stripe answered 401: STRIPE_SECRET_KEY is not accepted")
                if status == 404:
                    raise RailError(TRANSFER_NOT_FOUND, f"GET {path}: not found")
                if status >= 400:
                    raise RailError(
                        RAIL_ERROR, f"GET {path} HTTP {status}: {shapes.rest_error_message(data)}"
                    )
                return data
            last = type(exc).__name__ if exc else f"HTTP {status}"
            if attempt < GET_TRIES - 1:
                self._sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
        raise RailError(RAIL_ERROR, f"GET {path} failed after {GET_TRIES} tries: {last}")

    # ------------------------------------------------------------------ backend interface

    def destination_for(self, payee: PayeeRule) -> str:
        if not payee.stripe_account_id:
            raise RailError(
                RAIL_ERROR,
                f"payee {payee.payee_id} has no stripe_account_id; the REST fallback pays Connect accounts",
            )
        return payee.stripe_account_id

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
        form = shapes.rest_transfer_form(
            destination=destination,
            amount_cents=amount_cents,
            currency=currency,
            description=description,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )
        last = ""
        for attempt in range(POST_TRIES):
            status, data, exc = self._send(
                "POST", shapes.TRANSFERS_PATH, form=form, idempotency_key=idempotency_key
            )
            if exc is None and status is not None and status < 400:
                view = shapes.parse_rest_transfer(data)
                if view is None:
                    raise RailOutcomeUnknown(RAIL_ERROR, "POST /v1/transfers answered without a transfer id")
                return shapes.SendOutcome(
                    kind="sent",
                    payment_id=view.payment_id,
                    status=view.status,
                    amount_cents=view.amount_cents,
                    currency=view.currency,
                )
            if exc is None and status == 401:
                raise RailError(RAIL_ERROR, "Stripe answered 401: STRIPE_SECRET_KEY is not accepted")
            if exc is None and status == 409:
                # Same Idempotency-Key reused with different parameters, or still in flight.
                raise RailError(
                    RAIL_DUPLICATE_TRANSFER,
                    f"Stripe refused a reused idempotency key; no second transfer was made "
                    f"({shapes.rest_error_message(data)})",
                )
            if exc is None and status is not None and status < 500:
                raise RailError(
                    RAIL_ERROR, f"POST /v1/transfers HTTP {status}: {shapes.rest_error_message(data)}"
                )
            last = type(exc).__name__ if exc else f"HTTP {status}"
            if attempt < POST_TRIES - 1:
                self._sleep(BACKOFF_S[0])
        raise RailOutcomeUnknown(
            RAIL_ERROR, f"POST /v1/transfers outcome unknown after {POST_TRIES} tries: {last}"
        )

    def find_payment(self, idempotency_key: str) -> shapes.PaymentView | None:
        data = self._get_ok(shapes.TRANSFERS_PATH, {"transfer_group": idempotency_key, "limit": 10})
        for item in (data or {}).get("data") or []:
            view = shapes.parse_rest_transfer(item)
            if view is not None:
                return view
        return None

    def get_payment(self, payment_id: str) -> shapes.PaymentView:
        if not payment_id.startswith("tr_") or "/" in payment_id:
            raise RailError(TRANSFER_NOT_FOUND, "invalid transfer id")
        view = shapes.parse_rest_transfer(self._get_ok(shapes.TRANSFER_PATH.format(id=payment_id)))
        if view is None:
            raise RailError(RAIL_ERROR, "GET /v1/transfers/{id}: unexpected body")
        return view

    def balance_cents(self, currency: str) -> int:
        cents = shapes.parse_balance(self._get_ok(shapes.BALANCE_PATH), currency)
        if cents is None:
            raise RailError(RAIL_ERROR, "balance missing in response")
        return cents

    def setup_recipient(self, **_: Any) -> shapes.RecipientSetup:
        raise RailError(
            RAIL_ERROR,
            "the REST fallback does not set up Treasury recipients; create a test connected account in the "
            "Stripe Dashboard and put its acct_ id in the payee's stripe_account_id",
        )

    def close(self) -> None:
        self._client.close()
