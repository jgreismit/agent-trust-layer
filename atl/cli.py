"""`atl` command line (lane L4).

Frozen commands:
    atl approve <decision_id> | atl refuse <decision_id> | atl revoke <mandate_id>
    atl rail balance
    atl reset
    atl mandate register <file>
    atl demo [--sim] [--auto-approve-after SECONDS] [--production-single]
        delegates to demo.runner.run_demo(sim=..., auto_approve_after=...) owned by L5,
        except --production-single which L4 implements in atl.approvals.production.

Every command calls load_settings first; an ATLError prints "CODE: message" on stderr and exits 2.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from atl.config import Settings, load_settings
from atl.contracts import ATLError

app = typer.Typer(help="Agent Trust Layer", no_args_is_help=True)
rail_app = typer.Typer(help="Read-only rail commands", no_args_is_help=True)
mandate_app = typer.Typer(help="Mandate commands", no_args_is_help=True)
app.add_typer(rail_app, name="rail")
app.add_typer(mandate_app, name="mandate")

EXIT_ATL_ERROR = 2


def _run(fn: Callable[[Settings], Any]) -> Any:
    try:
        settings = load_settings()
        return fn(settings)
    except ATLError as exc:
        typer.echo(f"{exc.code}: {exc.message}" if exc.message else exc.code, err=True)
        raise typer.Exit(EXIT_ATL_ERROR) from None


def _service(settings: Settings):
    from atl.service import ATLService

    return ATLService.from_settings(settings)


@app.command()
def version() -> None:
    """Print the ATL version."""
    from atl import __version__

    typer.echo(__version__)


@app.command()
def approve(decision_id: str) -> None:
    """Approve an escalated decision (signed with the owner key)."""

    def go(settings: Settings) -> None:
        d = _service(settings).approve(decision_id)
        typer.echo(f"approved {d['decision_id']} (receipt {d['receipt_id']})")

    _run(go)


@app.command()
def refuse(decision_id: str) -> None:
    """Refuse an escalated decision (signed with the owner key)."""

    def go(settings: Settings) -> None:
        d = _service(settings).refuse(decision_id)
        typer.echo(f"refused {d['decision_id']} (receipt {d['receipt_id']})")

    _run(go)


@app.command()
def revoke(mandate_id: str) -> None:
    """Revoke a mandate; every later pay_check under it is refused."""

    def go(settings: Settings) -> None:
        _service(settings).revoke(mandate_id)
        typer.echo(f"revoked {mandate_id}")

    _run(go)


@app.command()
def reset() -> None:
    """Delete the database and var/current_run_id. Keys are kept."""

    def go(settings: Settings) -> None:
        from atl.store import reset_store

        reset_store(settings.db_path)
        (Path(settings.var_dir) / "current_run_id").unlink(missing_ok=True)
        typer.echo("reset done (keys kept)")

    _run(go)


@rail_app.command("balance")
def rail_balance() -> None:
    """Print the rail balance (read only)."""

    def go(settings: Settings) -> None:
        from atl.approvals import format_brl
        from atl.rails import get_rail

        rail = get_rail(settings)
        typer.echo(f"{rail.name}: {format_brl(rail.balance_cents())}")

    _run(go)


@mandate_app.command("register")
def mandate_register(file: Path) -> None:
    """Sign a mandate JSON file with the owner key and register it."""

    def go(settings: Settings) -> None:
        data = json.loads(file.read_text(encoding="utf-8"))
        svc = _service(settings)
        out = svc.register_mandate(svc.sign_mandate(data))
        typer.echo(f"registered {out['mandate_id']}")

    _run(go)


@app.command()
def demo(
    sim: bool = typer.Option(False, "--sim", help="Use the simulated rail."),
    auto_approve_after: float | None = typer.Option(
        None, "--auto-approve-after", help="Unattended test runs only: auto-approve after SECONDS."
    ),
    production_single: bool = typer.Option(
        False, "--production-single", help="One 100 cent payment to the allowlisted key (human only)."
    ),
    rail_auth_wait: float | None = typer.Option(
        None,
        "--rail-auth-wait",
        help="Seconds to wait for the owner to authorize PENDING transfers in Asaas (600; 0 with auto).",
    ),
) -> None:
    """Run the end-to-end demo, or the single production payment."""

    def go(settings: Settings) -> int:
        if production_single:
            from atl.approvals.production import run_production_single

            return run_production_single(settings)
        from demo.runner import run_demo

        return run_demo(sim=sim, auto_approve_after=auto_approve_after, rail_auth_wait=rail_auth_wait)

    code = _run(go)
    raise typer.Exit(int(code or 0))


if __name__ == "__main__":
    app()
