"""Settings and environment guards. Owned by L0.

- Loads the sandbox key from `.env` (searched upward from the package, so git worktrees find it).
- Never reads `.env.production` unless every promotion precondition holds.
- Refuses any base URL that is not the Asaas sandbox host while ATL_ENV is sandbox.
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


@dataclass(frozen=True)
class Settings:
    env: Literal["sandbox", "production"]
    rail: Literal["asaas", "simulated"]
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


def load_settings(*, stdin_isatty: bool | None = None) -> Settings:
    env = os.environ.get("ATL_ENV", "sandbox").strip().lower() or "sandbox"
    if env not in ("sandbox", "production"):
        raise ATLError(PRODUCTION_BLOCKED, f"unknown ATL_ENV {env!r}")
    root = REPO_ROOT
    var_dir = Path(os.environ.get("ATL_VAR_DIR") or root / "var")
    runs_dir = Path(os.environ.get("ATL_RUNS_DIR") or root / "runs")
    rail: Literal["asaas", "simulated"] = (
        "simulated" if os.environ.get("ATL_RAIL", "").lower() == "simulated" else "asaas"
    )
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
    )
