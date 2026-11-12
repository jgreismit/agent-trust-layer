"""Payment rails (lane L1).

Public API (frozen):
    get_rail(settings: Settings) -> Rail
        settings.rail == "simulated" -> SimulatedRail (name "simulated")
        settings.env == "sandbox"     -> AsaasRail on the sandbox host (name "asaas_sandbox")
        settings.env == "production"  -> AsaasRail on production (name "asaas_production"), only after
                                          atl.config.production_guard passed
        settings.rail == "stripe"     -> StripeMCPRail (name "stripe_sandbox", USD), sandbox only, through
                                          the Stripe MCP (default) or REST (ATL_STRIPE_TRANSPORT=rest)
    rail_currency(settings) -> "BRL" | "USD"   (no rail is built, no I/O)
    RailError(ATLError) with code RAIL_ERROR or TRANSFER_NOT_FOUND.
Any attempt to reach api.asaas.com outside production raises ATLError(PRODUCTION_BLOCKED) before I/O.
Any live Stripe key, or a Stripe rail outside ATL_ENV=sandbox, raises ATLError(PRODUCTION_BLOCKED) before I/O.
"""

from __future__ import annotations

from atl.config import Settings
from atl.contracts import Rail
from atl.rails.asaas import AsaasRail
from atl.rails.errors import RailError
from atl.rails.simulated import SimulatedRail
from atl.runlog import RunLog

__all__ = ["AsaasRail", "RailError", "SimulatedRail", "StripeMCPRail", "get_rail", "rail_currency"]


def rail_currency(settings: Settings) -> str:
    return "USD" if settings.rail == "stripe" else "BRL"


def get_rail(settings: Settings, runlog: RunLog | None = None) -> Rail:
    if settings.rail == "simulated":
        return SimulatedRail(settings.var_dir / "sim_rail.json", runlog=runlog)
    if settings.rail == "stripe":
        from atl.rails.stripe_mcp import StripeMCPRail

        return StripeMCPRail(settings, runlog=runlog)
    return AsaasRail(settings, runlog=runlog)


def __getattr__(name: str):
    if name == "StripeMCPRail":
        from atl.rails.stripe_mcp import StripeMCPRail as cls

        return cls
    raise AttributeError(name)
