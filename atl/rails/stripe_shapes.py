"""Every assumption ATL makes about Stripe tool and API shapes, in one place.

Nobody on the team had a Stripe sandbox key when this was written, so the argument and result shapes of the
Stripe MCP tools (setup_recipient, send_money, stripe_api_read) are inferred from docs.stripe.com/mcp and the
v2 money management API reference (outbound payments, financial accounts). Each assumption is tagged
[A1]..[A14] and listed in STRIPE_RAIL_REPORT.md and atl/rails/STRIPE_NOTES.md. The first real run of
`scripts/stripe_sandbox_smoke.py --list-tools` prints the real tool schemas; fix this module, nothing else.

Defenses that hold even if a guess is wrong:
- fit_to_schema drops arguments the live tool schema does not declare, so a wrong optional field name
  (description, metadata, idempotency_key) cannot make the call fail; required ones surface as tool errors.
- A send_money answer that is neither a recognizable payment nor a confirmation request is treated as an
  unknown outcome: the rail records it as uncertain and never sends again for that idempotency key.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

# ---------------------------------------------------------------------------------------------- tool names
# [A1] Tool names as listed on docs.stripe.com/mcp (Treasury tools are public preview).
TOOL_SETUP_RECIPIENT = "setup_recipient"
TOOL_SEND_MONEY = "send_money"
TOOL_API_READ = "stripe_api_read"
TOOL_BALANCE_SUMMARY = "get_balance_summary"
TOOL_ACCOUNT_INFO = "get_stripe_account_info"
REQUIRED_TOOLS = (TOOL_SETUP_RECIPIENT, TOOL_SEND_MONEY, TOOL_API_READ)

# ---------------------------------------------------------------------------------------------- API paths
OUTBOUND_PAYMENT_PATH = "/v2/money_management/outbound_payments/{id}"
OUTBOUND_PAYMENTS_PATH = "/v2/money_management/outbound_payments"
FINANCIAL_ACCOUNT_PATH = "/v2/money_management/financial_accounts/{id}"
BALANCE_PATH = "/v1/balance"
TRANSFERS_PATH = "/v1/transfers"
TRANSFER_PATH = "/v1/transfers/{id}"

# ---------------------------------------------------------------------------------------------- statuses
# [A9] v2 OutboundPayment.status values (API reference): processing, posted, failed, returned, canceled.
OUTBOUND_STATUS_MEANING = {
    "processing": "in flight",
    "posted": "paid",
    "failed": "failed, no money moved",
    "returned": "returned by the receiving bank",
    "canceled": "canceled before posting",
}
FAILED_STATUSES = frozenset({"failed", "returned", "canceled", "reversed"})
# [A13] v1 Transfer objects have no status field: a created transfer is complete. ATL reports "paid", or
# "reversed" when the transfer has been reversed.
REST_PAID = "paid"
REST_REVERSED = "reversed"

_ID_RE = {
    "acct": re.compile(r"\bacct_[A-Za-z0-9_]+"),
    "obp": re.compile(r"\bobp_[A-Za-z0-9_]+"),
}
_URL_RE = re.compile(r"https://[^\s\"'<>)\]]+")
_CONFIRM_WORDS = re.compile(r"confirm|approv", re.I)
_CONFIRM_URL_KEYS = ("confirmation_url", "approval_url", "confirm_url", "review_url", "url")
_APPROVAL_REF_KEYS = ("approval_token", "approval_request", "approval_request_id", "approval_id")
_ONBOARDING_URL_KEYS = ("onboarding_url", "account_link", "url", "link")


@dataclass
class ToolResult:
    """Transport-neutral view of an MCP CallToolResult."""

    is_error: bool
    data: Any = None  # structuredContent, else JSON parsed from the text blocks, else None
    text: str = ""  # all text blocks joined

    def __post_init__(self) -> None:
        if self.data is None:
            self.data = parse_text_json(self.text)


@dataclass
class RecipientSetup:
    recipient_id: str
    onboarding_url: str | None = None
    raw: Any = None


@dataclass
class SendOutcome:
    # sent: Stripe created the payment. confirmation_required: held for the account owner (human
    # confirmation). error: the tool refused, nothing moved. unknown: success-looking answer ATL cannot read.
    kind: Literal["sent", "confirmation_required", "error", "unknown"]
    payment_id: str | None = None
    status: str | None = None
    amount_cents: int | None = None
    currency: str | None = None
    confirmation_url: str | None = None
    approval_ref: dict[str, str] = field(default_factory=dict)
    message: str = ""


@dataclass
class PaymentView:
    payment_id: str
    status: str
    amount_cents: int
    currency: str
    destination: str
    created_at: datetime
    metadata: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------- helpers


def parse_text_json(text: str) -> Any:
    """Tool results often carry JSON as a text block. Return the parsed object, or None."""
    t = (text or "").strip()
    if not t or t[0] not in "{[":
        return None
    try:
        return json.loads(t)
    except ValueError:
        return None


def fit_to_schema(arguments: dict[str, Any], input_schema: dict[str, Any] | None) -> tuple[dict, list[str]]:
    """Keep only arguments the tool declares. Returns (arguments, dropped names). No schema: unchanged."""
    props = (input_schema or {}).get("properties")
    if not isinstance(props, dict) or not props:
        return dict(arguments), []
    kept = {k: v for k, v in arguments.items() if k in props}
    return kept, sorted(k for k in arguments if k not in props)


def _walk(obj: Any):
    """Yield every dict nested in obj (depth first), including obj."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _first_str(obj: Any, keys: tuple[str, ...], pred=lambda s: True) -> str | None:
    for d in _walk(obj):
        for k in keys:
            v = d.get(k)
            if isinstance(v, str) and v and pred(v):
                return v
    return None


def _amount_cents(value: Any) -> int | None:
    """[A8] v2 amounts are {"value": <minor units int>, "currency": "usd"}; v1 amounts are ints."""
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _currency(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("currency")
    return value.upper() if isinstance(value, str) and value else None


def _created(value: Any) -> datetime:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def _blob(result: ToolResult) -> str:
    return result.text + "\n" + (json.dumps(result.data, default=str) if result.data is not None else "")


# ---------------------------------------------------------------------------------------------- setup


def setup_recipient_args(
    *, display_name: str, email: str, country: str = "US", entity_type: str = "company"
) -> dict[str, Any]:
    """[A2] setup_recipient arguments: name, email, country, entity type of the payee."""
    return {"name": display_name, "email": email, "country": country, "entity_type": entity_type}


def parse_recipient(result: ToolResult) -> RecipientSetup | None:
    """[A3] The recipient is a v2 Account (acct_...) configured as a recipient; it may come with a hosted
    onboarding link where the payee enters bank details. Id from structured keys, else first acct_ in text."""
    rid = _first_str(
        result.data,
        ("recipient_id", "recipient", "account_id", "account", "id"),
        lambda s: s.startswith("acct_"),
    )
    if rid is None:
        m = _ID_RE["acct"].search(_blob(result))
        rid = m.group(0) if m else None
    if rid is None:
        return None
    url = _first_str(result.data, _ONBOARDING_URL_KEYS, lambda s: s.startswith("https://"))
    if url is None:
        m = _URL_RE.search(result.text)
        url = m.group(0) if m else None
    return RecipientSetup(recipient_id=rid, onboarding_url=url, raw=result.data)


# ---------------------------------------------------------------------------------------------- send money


def send_money_args(
    *,
    recipient_id: str,
    amount_cents: int,
    currency: str,
    description: str,
    idempotency_key: str,
    metadata: dict[str, str],
    financial_account: str | None = None,
    approval_ref: dict[str, str] | None = None,
) -> dict[str, Any]:
    """[A4] send_money arguments. amount in minor units (cents) [A5], lowercase currency, the recipient id,
    a description and metadata carrying ATL's idempotency key and intent id [A6], an idempotency_key field
    [A7], optional source financial account, and on retry the approval reference from Stripe [A11]."""
    args: dict[str, Any] = {
        "recipient": recipient_id,
        "amount": amount_cents,
        "currency": currency.lower(),
        "description": description,
        "metadata": dict(metadata),
        "idempotency_key": idempotency_key,
    }
    if financial_account:
        args["financial_account"] = financial_account
    for k, v in (approval_ref or {}).items():
        args[k] = v
    return args


def find_confirmation_url(result: ToolResult) -> str | None:
    """[A10] Human confirmation: Stripe answers with a Dashboard URL to approve instead of executing. Taken
    from a structured url field, or from the text when the text speaks of confirming or approving."""
    url = _first_str(result.data, _CONFIRM_URL_KEYS, lambda s: s.startswith("https://"))
    if url is not None and (_CONFIRM_WORDS.search(_blob(result)) or "dashboard.stripe.com" in url):
        return url
    if _CONFIRM_WORDS.search(_blob(result)):
        for m in _URL_RE.finditer(_blob(result)):
            if "stripe.com" in m.group(0):
                return m.group(0)
    return None


def parse_send_money(result: ToolResult) -> SendOutcome:
    url = find_confirmation_url(result)
    approval_ref = {k: v for k in _APPROVAL_REF_KEYS if (v := _first_str(result.data, (k,))) is not None}
    held = url is not None or "approval_required" in _blob(result)
    if held:
        return SendOutcome(
            kind="confirmation_required",
            confirmation_url=url,
            approval_ref=approval_ref,
            message=result.text[:300],
        )
    if result.is_error:
        return SendOutcome(kind="error", message=result.text[:300] or "tool error")
    pid = _first_str(
        result.data, ("id", "outbound_payment", "outbound_payment_id"), lambda s: s.startswith("obp_")
    )
    if pid is None:
        m = _ID_RE["obp"].search(_blob(result))
        pid = m.group(0) if m else None
    if pid is None:
        return SendOutcome(kind="unknown", message=result.text[:300] or "no payment id in send_money result")
    data = result.data if isinstance(result.data, dict) else {}
    return SendOutcome(
        kind="sent",
        payment_id=pid,
        status=str(data.get("status") or "processing"),
        amount_cents=_amount_cents(data.get("amount")),
        currency=_currency(data.get("amount")) or _currency(data.get("currency")),
    )


# ---------------------------------------------------------------------------------------------- reads


def read_args(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """[A12] stripe_api_read takes the GET path and query parameters."""
    out: dict[str, Any] = {"method": "GET", "path": path}
    if params:
        out["params"] = params
    return out


def read_data(result: ToolResult) -> Any:
    return result.data if result.data is not None else parse_text_json(result.text)


def parse_outbound_payment(data: Any, fallback_destination: str = "") -> PaymentView | None:
    if not isinstance(data, dict) or not str(data.get("id", "")).startswith("obp_"):
        return None
    to = data.get("to") if isinstance(data.get("to"), dict) else {}
    return PaymentView(
        payment_id=str(data["id"]),
        status=str(data.get("status") or "processing"),
        amount_cents=_amount_cents(data.get("amount")) or 0,
        currency=_currency(data.get("amount")) or "USD",
        destination=str(to.get("recipient") or fallback_destination),
        created_at=_created(data.get("created")),
        metadata={k: str(v) for k, v in (data.get("metadata") or {}).items()},
    )


def find_outbound_payment(data: Any, idempotency_key: str) -> PaymentView | None:
    """Recovery after an uncertain send: match metadata.atl_idempotency_key, else the key in description."""
    items = data.get("data") if isinstance(data, dict) else None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        if meta.get("atl_idempotency_key") == idempotency_key or idempotency_key in str(
            item.get("description") or ""
        ):
            return parse_outbound_payment(item)
    return None


def parse_balance(data: Any, currency: str) -> int | None:
    """v1 balance: {"available": [{"amount", "currency"}]}. v2 financial account (Treasury):
    {"balance": {"available": {"usd": {"value", "currency"}}}} [A14]."""
    cur = currency.lower()
    if not isinstance(data, dict):
        return None
    avail = data.get("available")
    if isinstance(avail, list):
        total = [a.get("amount") for a in avail if isinstance(a, dict) and a.get("currency") == cur]
        return sum(int(x) for x in total if isinstance(x, int)) if total else 0
    bal = data.get("balance")
    if isinstance(bal, dict) and isinstance(bal.get("available"), dict):
        return _amount_cents(bal["available"].get(cur)) or 0
    return None


# ---------------------------------------------------------------------------------------------- REST fallback


def rest_transfer_form(
    *,
    destination: str,
    amount_cents: int,
    currency: str,
    description: str,
    idempotency_key: str,
    metadata: dict[str, str],
) -> dict[str, str]:
    """POST /v1/transfers (Connect), form encoded. transfer_group carries the ATL idempotency key so
    GET /v1/transfers?transfer_group=... can find the transfer after an uncertain answer."""
    form = {
        "amount": str(amount_cents),
        "currency": currency.lower(),
        "destination": destination,
        "description": description,
        "transfer_group": idempotency_key,
    }
    for k, v in metadata.items():
        form[f"metadata[{k}]"] = str(v)
    return form


def parse_rest_transfer(data: Any) -> PaymentView | None:
    if not isinstance(data, dict) or not str(data.get("id", "")).startswith("tr_"):
        return None
    return PaymentView(
        payment_id=str(data["id"]),
        status=REST_REVERSED if data.get("reversed") else REST_PAID,
        amount_cents=_amount_cents(data.get("amount")) or 0,
        currency=_currency(data.get("currency")) or "USD",
        destination=str(data.get("destination") or ""),
        created_at=_created(data.get("created")),
        metadata={k: str(v) for k, v in (data.get("metadata") or {}).items()},
    )


def rest_error_message(data: Any) -> str:
    """Stripe error body: {"error": {"type", "code", "message"}}."""
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        parts = [str(err[k]) for k in ("type", "code", "message") if err.get(k)]
        if parts:
            return ": ".join(parts)
    if isinstance(data, str) and data:
        return data[:300]
    return "no error description"


def rest_error_code(data: Any) -> str:
    err = data.get("error") if isinstance(data, dict) else None
    return str(err.get("code") or err.get("type") or "") if isinstance(err, dict) else ""
