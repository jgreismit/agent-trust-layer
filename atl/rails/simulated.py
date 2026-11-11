"""Simulated rail: same interface as AsaasRail, persistent JSON state, clearly labeled "simulated".

State file (default var/sim_rail.json): {"balance_cents": int, "transfers": [ {...}, ... ]}.
Every read-modify-write holds a thread lock and an fcntl file lock, and the file is rewritten atomically.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atl.contracts import RAIL_ERROR, TRANSFER_NOT_FOUND, RailName, RailTransfer
from atl.ids import new_id
from atl.rails.errors import RailError
from atl.runlog import RunLog

START_BALANCE_CENTS = 20000
_thread_lock = threading.Lock()


class SimulatedRail:
    name: RailName = "simulated"

    def __init__(self, state_path: Path | str, runlog: RunLog | None = None):
        self.state_path = Path(state_path)
        self._lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        self._runlog = runlog

    # ------------------------------------------------------------------ state

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with _thread_lock, self._lock_path.open("a") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {}
        missing = "balance_cents" not in state or "transfers" not in state
        state.setdefault("balance_cents", START_BALANCE_CENTS)
        state.setdefault("transfers", [])
        if missing:
            # Persist the initial state on first read so the verifier always finds the file.
            self._write(state)
        return state

    def _write(self, state: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.state_path.parent, prefix=".sim_rail.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def _log(self, method: str, **fields: Any) -> None:
        if self._runlog is not None:
            self._runlog.write("rail_call", rail=self.name, method=method, **fields)

    @staticmethod
    def _to_transfer(rec: dict[str, Any]) -> RailTransfer:
        return RailTransfer(
            rail="simulated",
            transfer_id=rec["transfer_id"],
            status=rec["status"],
            amount_cents=rec["amount_cents"],
            pix_key=rec["pix_key"],
            created_at=datetime.fromisoformat(rec["created_at"]),
            fee_cents=rec.get("fee_cents", 0),
        )

    # ------------------------------------------------------------------ Rail protocol

    def balance_cents(self) -> int:
        with self._locked():
            state = self._read()
        self._log("balance_cents", response={"balance_cents": state["balance_cents"]})
        return int(state["balance_cents"])

    def transfer_pix(
        self,
        *,
        pix_key: str,
        pix_key_type: str,
        amount_cents: int,
        idempotency_key: str,
        description: str,
    ) -> RailTransfer:
        if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
            raise RailError(RAIL_ERROR, "amount must be positive integer cents")
        if not idempotency_key:
            raise RailError(RAIL_ERROR, "idempotency_key is required")
        with self._locked():
            state = self._read()
            for rec in state["transfers"]:
                if rec.get("external_reference") == idempotency_key:
                    self._log("transfer_pix", replay=True, response={"transfer_id": rec["transfer_id"]})
                    return self._to_transfer(rec)
            if state["balance_cents"] < amount_cents:
                raise RailError(RAIL_ERROR, "insufficient simulated balance")
            rec = {
                "transfer_id": new_id("sim"),
                "status": "DONE",
                "amount_cents": amount_cents,
                "pix_key": pix_key,
                "pix_key_type": pix_key_type,
                "description": description,
                "external_reference": idempotency_key,
                "created_at": datetime.now(UTC).isoformat(),
                "fee_cents": 0,
            }
            state["transfers"].append(rec)
            state["balance_cents"] -= amount_cents
            self._write(state)
        self._log("transfer_pix", request={"amount_cents": amount_cents, "pix_key": pix_key},
                  response={"transfer_id": rec["transfer_id"], "status": "DONE"})
        return self._to_transfer(rec)

    def get_transfer(self, transfer_id: str) -> RailTransfer:
        with self._locked():
            state = self._read()
        for rec in state["transfers"]:
            if rec["transfer_id"] == transfer_id:
                return self._to_transfer(rec)
        raise RailError(TRANSFER_NOT_FOUND, f"transfer {transfer_id} not found")

    # ------------------------------------------------------------------ helpers

    def set_balance_cents(self, cents: int) -> None:
        if not isinstance(cents, int) or cents < 0:
            raise RailError(RAIL_ERROR, "balance must be non negative integer cents")
        with self._locked():
            state = self._read()
            state["balance_cents"] = cents
            self._write(state)
        self._log("set_balance_cents", request={"balance_cents": cents})
