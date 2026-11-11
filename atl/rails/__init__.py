"""Payment rails (lane L1).

Public API (frozen):
    get_rail(settings: Settings) -> Rail
        settings.rail == "simulated" -> SimulatedRail (name "simulated")
        settings.env == "sandbox"     -> AsaasRail on the sandbox host (name "asaas_sandbox")
        settings.env == "production"  -> AsaasRail on production (name "asaas_production"), only after
                                          atl.config.production_guard passed
    RailError(ATLError) with code RAIL_ERROR or TRANSFER_NOT_FOUND.
Any attempt to reach api.asaas.com outside production raises ATLError(PRODUCTION_BLOCKED) before I/O.
"""

from __future__ import annotations

from atl.config import Settings
from atl.contracts import Rail
from atl.rails.asaas import AsaasRail
from atl.rails.errors import RailError
from atl.rails.simulated import SimulatedRail
from atl.runlog import RunLog

__all__ = ["AsaasRail", "RailError", "SimulatedRail", "get_rail"]


def get_rail(settings: Settings, runlog: RunLog | None = None) -> Rail:
    if settings.rail == "simulated":
        return SimulatedRail(settings.var_dir / "sim_rail.json", runlog=runlog)
    return AsaasRail(settings, runlog=runlog)
