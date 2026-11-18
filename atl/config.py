"""Settings and environment guards. Owned by L0.

- Loads the sandbox key from `.env` (searched upward from the package, so git worktrees find it).
- Never reads `.env.production` unless every promotion precondition holds.
- Refuses any base URL that is not the Asaas sandbox host while ATL_ENV is sandbox.
- Stripe rail (ATL_RAIL=stripe_sandbox): sandbox only. Refuses ATL_ENV other than sandbox, any host other
  than mcp.stripe.com / api.stripe.com, and any Stripe key that is not a test-mode key, before any I/O.
  Keys come from STRIPE_AGENT_KEY (MCP transport) or STRIPE_SECRET_KEY (REST fallback), never logged.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from dotenv import dotenv_values

from atl.contracts import (
    CONFIG_INVALID,
    PRODUCTION_BLOCKED,
    PROMOTION_BLOCKED_ACCEPTANCE,
    PROMOTION_REQUIRES_HUMAN,
    ATLError,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX_BASE_URL = "https://api-sandbox.asaas.com/v3"
SANDBOX_HOST = "api-sandbox.asaas.com"
PRODUCTION_BASE_URL = "https://api.asaas.com/v3"
PRODUCTION_HOST = "api.asaas.com"
P0_ITEMS = [f"AC{i}" for i in range(1, 11)]
STRIPE_MCP_URL = "https://mcp.stripe.com"
STRIPE_API_BASE = "https://api.stripe.com"
STRIPE_HOSTS = ("mcp.stripe.com", "api.stripe.com")
RailKind = Literal["asaas", "simulated", "stripe"]
StripeTransportKind = Literal["mcp", "rest"]


@dataclass(frozen=True)
class Settings:
    env: Literal["sandbox", "production"]
    rail: RailKind
    base_url: str
    api_key: str | None = field(repr=False)
    root: Path
    var_dir: Path
    db_path: Path
    keys_dir: Path
    runs_dir: Path
    run_id: str
    prod_allowed_pix_key: str | None = field(default=None, repr=False)
    prod_allowed_pix_key_type: str | None = None
    stripe_transport: StripeTransportKind = "mcp"
    stripe_agent_key: str | None = field(default=None, repr=False)
    stripe_secret_key: str | None = field(default=None, repr=False)
    stripe_mcp_url: str = STRIPE_MCP_URL
    stripe_api_base: str = STRIPE_API_BASE
    # Treasury financial account to pay from (fa_...), when send_money needs one. Optional.
    stripe_financial_account: str | None = None


def find_dotenv(name: str = ".env", start: Path | None = None) -> Path | None:
    here = (start or REPO_ROOT).resolve()
    for d in [here, *here.parents]:
        candidate = d / name
        if candidate.is_file():
            return candidate
    return None


def acceptance_all_pass(report: Path) -> bool:
    if not report.is_file():
        return False
    text = report.read_text(encoding="utf-8")
    for item in P0_ITEMS:
        row = re.search(rf"^\|\s*{item}\s*\|(.*)$", text, re.M)
        if not row or "PASS" not in row.group(1) or "FAIL" in row.group(1) or "NOT RUN" in row.group(1):
            return False
    return True


def production_guard(root: Path = REPO_ROOT, stdin_isatty: bool | None = None) -> None:
    """Raise unless a human is promoting to production in an interactive terminal."""
    tty = sys.stdin.isatty() if stdin_isatty is None else stdin_isatty
    if not tty:
        raise ATLError(PROMOTION_REQUIRES_HUMAN, "production needs an interactive terminal")
    if os.environ.get("ATL_PROMOTION") != "1":
        raise ATLError(PROMOTION_REQUIRES_HUMAN, "set ATL_PROMOTION=1 to promote")
    if not acceptance_all_pass(root / "reports" / "ACCEPTANCE-REPORT.md"):
        raise ATLError(PROMOTION_BLOCKED_ACCEPTANCE, "acceptance report does not show every P0 item PASS")
    if not (root / ".env.production").is_file():
        raise ATLError(PROMOTION_REQUIRES_HUMAN, ".env.production is missing")


def assert_sandbox_url(url: str) -> None:
    host = urlparse(url).hostname or ""
    if host != SANDBOX_HOST:
        raise ATLError(PRODUCTION_BLOCKED, f"non-sandbox host refused: {host}")


def assert_stripe_url(url: str) -> None:
    host = urlparse(url).hostname or ""
    if urlparse(url).scheme != "https" or host not in STRIPE_HOSTS:
        raise ATLError(PRODUCTION_BLOCKED, f"non-Stripe host refused: {host}")


def assert_stripe_test_key(key: str | None, what: str = "Stripe key") -> None:
    """Refuse a live or unrecognized Stripe key. The key itself never appears in the message."""
    if not key:
        return
    if "_live_" in key:
        raise ATLError(PRODUCTION_BLOCKED, f"{what} is a live-mode key; the Stripe rail is sandbox only")
    if "_test_" not in key:
        raise ATLError(
            PRODUCTION_BLOCKED, f"{what} is not a recognizable test-mode key (expected *_test_*); refused"
        )


def _rail_kind(value: str) -> RailKind:
    """Fail safe (HW9 tester finding): no ATL_RAIL means the simulated rail, and an unknown value is an error.

    Before, anything that was not "simulated" or "stripe" (including a missing variable or a typo) selected
    the Asaas rail. A payment guard must never reach a real rail by default: Asaas now needs ATL_RAIL=asaas.
    """
    v = value.strip().lower()
    if v in ("", "simulated"):
        return "simulated"
    if v in ("asaas", "asaas_sandbox"):
        return "asaas"
    if v in ("stripe", "stripe_sandbox"):
        return "stripe"
    raise ATLError(CONFIG_INVALID, f"unknown ATL_RAIL {value!r}: use simulated, asaas or stripe_sandbox")


def load_settings(*, stdin_isatty: bool | None = None) -> Settings:
    env = os.environ.get("ATL_ENV", "sandbox").strip().lower() or "sandbox"
    if env not in ("sandbox", "production"):
        raise ATLError(PRODUCTION_BLOCKED, f"unknown ATL_ENV {env!r}")
    root = REPO_ROOT
    var_dir = Path(os.environ.get("ATL_VAR_DIR") or root / "var")
    runs_dir = Path(os.environ.get("ATL_RUNS_DIR") or root / "runs")
    rail = _rail_kind(os.environ.get("ATL_RAIL", ""))
    if rail == "stripe" and env != "sandbox":
        raise ATLError(PRODUCTION_BLOCKED, "the Stripe rail runs only with ATL_ENV=sandbox")
    prod_key = prod_key_type = None

    if env == "production":
        production_guard(root, stdin_isatty)
        values = dotenv_values(root / ".env.production")
        api_key = values.get("ASAAS_API_KEY")
        base_url = PRODUCTION_BASE_URL
        prod_key = values.get("ATL_PROD_ALLOWED_PIX_KEY")
        prod_key_type = values.get("ATL_PROD_ALLOWED_PIX_KEY_TYPE")
    else:
        dotenv = find_dotenv(".env")
        values = dotenv_values(dotenv) if dotenv else {}
        api_key = os.environ.get("ASAAS_API_KEY") or values.get("ASAAS_API_KEY") or None
        base_url = (
            os.environ.get("ASAAS_BASE_URL") or values.get("ASAAS_BASE_URL") or SANDBOX_BASE_URL
        ).rstrip("/")
        assert_sandbox_url(base_url)

    stripe: dict = {}
    if rail == "stripe":
        def pick(name: str) -> str | None:
            return os.environ.get(name) or values.get(name) or None

        transport = (pick("ATL_STRIPE_TRANSPORT") or "mcp").strip().lower()
        if transport not in ("mcp", "rest"):
            raise ATLError(PRODUCTION_BLOCKED, f"unknown ATL_STRIPE_TRANSPORT {transport!r}")
        stripe = dict(
            stripe_transport=transport,
            stripe_agent_key=pick("STRIPE_AGENT_KEY"),
            stripe_secret_key=pick("STRIPE_SECRET_KEY"),
            stripe_financial_account=pick("STRIPE_FINANCIAL_ACCOUNT"),
        )
        assert_stripe_test_key(stripe["stripe_agent_key"], "STRIPE_AGENT_KEY")
        assert_stripe_test_key(stripe["stripe_secret_key"], "STRIPE_SECRET_KEY")

    return Settings(
        env=env,  # type: ignore[arg-type]
        rail=rail,
        base_url=base_url,
        api_key=api_key,
        root=root,
        var_dir=var_dir,
        db_path=Path(os.environ.get("ATL_DB_PATH") or var_dir / "atl.db"),
        keys_dir=Path(os.environ.get("ATL_KEYS_DIR") or var_dir / "keys"),
        runs_dir=runs_dir,
        run_id=os.environ.get("ATL_RUN_ID") or "run_adhoc",
        prod_allowed_pix_key=prod_key,
        prod_allowed_pix_key_type=prod_key_type,
        **stripe,
    )
