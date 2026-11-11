"""Redacting JSONL run logger. Owned by L0.

Every external call and every tool call is one JSON line in runs/<run_id>.jsonl.
Anything that looks like an Asaas key, and the value of any secret-named field, is masked.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED]"
_KEY_RE = re.compile(r"\$?aact_[A-Za-z0-9_\-.:=+/]+")
_SECRET_FIELD_RE = re.compile(
    r"(access_token|api_key|apikey|authorization|secret|password|private_key)", re.I
)
_lock = threading.Lock()


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return _KEY_RE.sub(REDACTED, value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_FIELD_RE.search(k) and v not in (None, ""):
                out[k] = REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


def _default_runs_dir() -> Path:
    env = os.environ.get("ATL_RUNS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "runs"


def current_run_id() -> str:
    """ATL_RUN_ID, else the id the demo runner wrote to var/current_run_id, else run_adhoc.

    The file lets an owner's manual `atl approve` in another terminal log into the demo run.
    """
    env = os.environ.get("ATL_RUN_ID")
    if env:
        return env
    var_dir = Path(os.environ.get("ATL_VAR_DIR") or Path(__file__).resolve().parent.parent / "var")
    marker = var_dir / "current_run_id"
    try:
        value = marker.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    return value or "run_adhoc"


class RunLog:
    def __init__(self, run_id: str | None = None, runs_dir: Path | None = None):
        self.run_id = run_id or current_run_id()
        self.runs_dir = runs_dir or _default_runs_dir()
        self.path = self.runs_dir / f"{self.run_id}.jsonl"

    def write(self, event: str, **fields: Any) -> dict:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "event": event,
            **fields,
        }
        record = redact(record)
        line = json.dumps(record, default=str, ensure_ascii=False)
        with _lock:
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return record


class Timer:
    """Usage: with Timer() as t: ...; t.ms"""

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        self.ms = 0
        return self

    def __exit__(self, *exc: object) -> None:
        self.ms = int((time.perf_counter() - self._start) * 1000)


def get_runlog() -> RunLog:
    return RunLog()
