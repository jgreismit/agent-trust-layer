"""`atl` command line (lane L4).

Frozen commands:
    atl approve <decision_id> | atl refuse <decision_id> | atl revoke <mandate_id>
    atl rail balance
    atl reset
    atl mandate register <file>
    atl demo [--sim] [--auto-approve-after SECONDS] [--production-single]
        delegates to demo.runner.run_demo(sim=..., auto_approve_after=...) owned by L5,
        except --production-single which L4 implements in atl.approvals.production.

Stripe rail (ATL_RAIL=stripe_sandbox), owner side only:
    atl stripe setup-recipient PAYEE_ID --name NAME --email EMAIL [--mandate FILE]
    atl stripe pending
    atl stripe confirm DECISION_ID [--token APPROVAL_TOKEN]
    atl stripe abandon DECISION_ID

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
stripe_app = typer.Typer(help="Stripe rail owner commands (ATL_RAIL=stripe_sandbox)", no_args_is_help=True)
app.add_typer(rail_app, name="rail")
app.add_typer(mandate_app, name="mandate")
app.add_typer(stripe_app, name="stripe")

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
        from atl.rails import get_rail, rail_currency

        rail = get_rail(settings)
        cents = rail.balance_cents()
        if rail_currency(settings) == "USD":
            typer.echo(f"{rail.name}: US$ {cents // 100}.{cents % 100:02d}")
        else:
            typer.echo(f"{rail.name}: {format_brl(cents)}")

    _run(go)


@mandate_app.command("register")
def mandate_register(file: Path) -> None:
    """Sign a mandate JSON file with the owner key and register it."""

    def go(settings: Settings) -> None:
        data = json.loads(file.read_text(encoding="utf-8"))
        svc = _service(settings)
        out = svc.register_mandate(svc.sign_mandate(data))
        typer.echo(f"registered {out['mandate_id']}")
        if settings.rail == "stripe" and settings.stripe_transport == "mcp":
            known = {r.get("recipient_id") for r in svc.rail.recipients().values()}
            for p in data.get("payees") or []:
                rid = p.get("stripe_recipient_id")
                if rid and rid not in known:
                    typer.echo(
                        f"note: {p.get('payee_id')} recipient {rid} was not set up through ATL "
                        "(atl stripe setup-recipient); make sure it exists in the Stripe sandbox",
                        err=True,
                    )

    _run(go)


# ---------------------------------------------------------------------------------------------- stripe


@stripe_app.command("setup-recipient")
def stripe_setup_recipient(
    payee_id: str,
    name: str = typer.Option(..., "--name", help="Payee legal or display name."),
    email: str = typer.Option(..., "--email", help="Payee contact email (Stripe may send it onboarding)."),
    country: str = typer.Option("US", "--country"),
    mandate: Path | None = typer.Option(
        None, "--mandate", help="Mandate JSON file: write the recipient id into this payee's entry."
    ),
) -> None:
    """Owner: set up a Stripe Treasury recipient (setup_recipient) and record its id in ATL's state."""

    def go(settings: Settings) -> None:
        rec = _service(settings).setup_stripe_recipient(payee_id, name, email, country)
        typer.echo(f"recipient {rec['recipient_id']} set up for {payee_id}")
        if rec.get("onboarding_url"):
            typer.echo(f"payee onboarding link (send it to the payee): {rec['onboarding_url']}")
        if mandate is not None:
            data = json.loads(mandate.read_text(encoding="utf-8"))
            hits = [p for p in data.get("payees") or [] if p.get("payee_id") == payee_id]
            if not hits:
                raise ATLError("INVALID_ARGUMENT", f"payee {payee_id} not in {mandate}")
            hits[0]["stripe_recipient_id"] = rec["recipient_id"]
            mandate.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            typer.echo(f"wrote stripe_recipient_id into {mandate}; register it with atl mandate register")

    _run(go)


@stripe_app.command("pending")
def stripe_pending() -> None:
    """Owner: list payments Stripe holds for human confirmation, with their Dashboard URLs."""

    def go(settings: Settings) -> None:
        items = _service(settings).pending_rail_payments()
        if not items:
            typer.echo("no payment waits for Stripe confirmation")
        for r in items:
            typer.echo(
                f"{r['idempotency_key']}  {r['amount_cents']} {r['currency']} cents -> {r['destination']}  "
                f"approve: {r.get('confirmation_url') or '(no URL)'}"
            )

    _run(go)


@stripe_app.command("confirm")
def stripe_confirm(
    decision_id: str,
    token: str | None = typer.Option(None, "--token", help="Approval token Stripe showed after Approve."),
) -> None:
    """Owner: after approving in the Stripe Dashboard, retry the held payment. ATL never approves itself."""

    def go(settings: Settings) -> None:
        out = _service(settings).confirm_rail_payment(decision_id, token)
        if out["status"] == "executed":
            typer.echo(f"executed {decision_id}: {out['transfer_id']} (receipt {out['receipt_id']})")
        else:
            typer.echo(
                f"still pending Stripe confirmation: approve at {out.get('confirmation_url') or '(no URL)'}"
            )

    _run(go)


@stripe_app.command("abandon")
def stripe_abandon(decision_id: str) -> None:
    """Owner: give up a payment Stripe still holds (decision becomes expired; never sent)."""

    def go(settings: Settings) -> None:
        out = _service(settings).abandon_rail_payment(decision_id)
        typer.echo(f"abandoned {decision_id} (receipt {out['receipt_id']})")

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
