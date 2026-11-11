"""SQLiteStore: implements Store and StoreView from atl.contracts."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atl.contracts import (
    ApprovalRecord,
    Decision,
    DecisionStatus,
    Mandate,
    PaymentIntent,
    RailTransfer,
    Receipt,
)

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _ts(value: datetime) -> str:
    """Fixed-width UTC string so lexicographic order equals time order."""
    return value.astimezone(UTC).strftime(_TS_FORMAT)


class SQLiteStore:
    def __init__(self, db_path: Path, schema_sql: str):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(db_path), timeout=5.0, check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.executescript(schema_sql)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # helpers
    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    @staticmethod
    def _pairs(rows: list[tuple[Any, ...]]) -> list[tuple[PaymentIntent, Decision]]:
        return [
            (PaymentIntent.model_validate_json(r[0]), Decision.model_validate_json(r[1])) for r in rows
        ]

    # mandates
    def save_mandate(self, mandate: Mandate) -> None:
        self._exec(
            "INSERT INTO mandates (mandate_id, agent_id, revoked, body) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(mandate_id) DO UPDATE SET agent_id=excluded.agent_id, "
            "revoked=excluded.revoked, body=excluded.body",
            (mandate.mandate_id, mandate.agent_id, int(mandate.revoked), mandate.model_dump_json()),
        )

    def get_mandate(self, mandate_id: str) -> Mandate | None:
        row = self._one("SELECT body, revoked FROM mandates WHERE mandate_id = ?", (mandate_id,))
        if row is None:
            return None
        mandate = Mandate.model_validate_json(row[0])
        if row[1]:
            mandate = mandate.model_copy(update={"revoked": True})
        return mandate

    def revoke_mandate(self, mandate_id: str) -> None:
        self._exec("UPDATE mandates SET revoked = 1 WHERE mandate_id = ?", (mandate_id,))

    # intents
    def save_intent(self, intent: PaymentIntent) -> None:
        self._exec(
            "INSERT INTO intents (intent_id, mandate_id, payee_id, invoice_ref, idempotency_key, "
            "amount_cents, body) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(intent_id) DO UPDATE SET mandate_id=excluded.mandate_id, "
            "payee_id=excluded.payee_id, invoice_ref=excluded.invoice_ref, "
            "idempotency_key=excluded.idempotency_key, amount_cents=excluded.amount_cents, "
            "body=excluded.body",
            (
                intent.intent_id,
                intent.mandate_id,
                intent.payee_id,
                intent.invoice_ref,
                intent.idempotency_key,
                intent.amount_cents,
                intent.model_dump_json(),
            ),
        )

    def get_intent(self, intent_id: str) -> PaymentIntent | None:
        row = self._one("SELECT body FROM intents WHERE intent_id = ?", (intent_id,))
        return PaymentIntent.model_validate_json(row[0]) if row else None

    # decisions
    def save_decision(self, decision: Decision) -> None:
        self._exec(
            "INSERT INTO decisions (decision_id, intent_id, verdict, status, created_at, body) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(decision_id) DO UPDATE SET intent_id=excluded.intent_id, "
            "verdict=excluded.verdict, status=excluded.status, created_at=excluded.created_at, "
            "body=excluded.body",
            (
                decision.decision_id,
                decision.intent_id,
                decision.verdict,
                decision.status,
                _ts(decision.created_at),
                decision.model_dump_json(),
            ),
        )

    def get_decision(self, decision_id: str) -> Decision | None:
        row = self._one("SELECT body FROM decisions WHERE decision_id = ?", (decision_id,))
        return Decision.model_validate_json(row[0]) if row else None

    def update_decision_status(self, decision_id: str, status: DecisionStatus) -> None:
        with self._lock:
            decision = self.get_decision(decision_id)
            if decision is None:
                return
            self.save_decision(decision.model_copy(update={"status": status}))

    # approvals
    def save_approval(self, approval: ApprovalRecord) -> None:
        self._exec(
            "INSERT INTO approvals (decision_id, action, created_at, body) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(decision_id) DO UPDATE SET action=excluded.action, "
            "created_at=excluded.created_at, body=excluded.body",
            (approval.decision_id, approval.action, _ts(approval.created_at), approval.model_dump_json()),
        )

    def get_approval(self, decision_id: str) -> ApprovalRecord | None:
        row = self._one("SELECT body FROM approvals WHERE decision_id = ?", (decision_id,))
        return ApprovalRecord.model_validate_json(row[0]) if row else None

    # transfers
    def save_transfer(self, decision_id: str, transfer: RailTransfer) -> None:
        self._exec(
            "INSERT INTO transfers (decision_id, transfer_id, rail, body) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(decision_id) DO UPDATE SET transfer_id=excluded.transfer_id, "
            "rail=excluded.rail, body=excluded.body",
            (decision_id, transfer.transfer_id, transfer.rail, transfer.model_dump_json()),
        )

    def get_transfer_for_decision(self, decision_id: str) -> RailTransfer | None:
        row = self._one("SELECT body FROM transfers WHERE decision_id = ?", (decision_id,))
        return RailTransfer.model_validate_json(row[0]) if row else None

    # receipts
    def append_receipt(self, receipt: Receipt) -> None:
        """Raises sqlite3.IntegrityError on a duplicate seq or receipt_id."""
        self._exec(
            "INSERT INTO receipts (seq, receipt_id, decision_id, hash, prev_hash, body) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                receipt.seq,
                receipt.receipt_id,
                receipt.decision.decision_id,
                receipt.hash,
                receipt.prev_hash,
                receipt.model_dump_json(),
            ),
        )

    def last_receipt(self) -> Receipt | None:
        row = self._one("SELECT body FROM receipts ORDER BY seq DESC LIMIT 1")
        return Receipt.model_validate_json(row[0]) if row else None

    def get_receipt(self, receipt_id: str) -> Receipt | None:
        row = self._one("SELECT body FROM receipts WHERE receipt_id = ?", (receipt_id,))
        return Receipt.model_validate_json(row[0]) if row else None

    def list_receipts(self) -> list[Receipt]:
        rows = self._all("SELECT body FROM receipts ORDER BY seq ASC")
        return [Receipt.model_validate_json(r[0]) for r in rows]

    def receipts_for_decision(self, decision_id: str) -> list[Receipt]:
        rows = self._all("SELECT body FROM receipts WHERE decision_id = ? ORDER BY seq ASC", (decision_id,))
        return [Receipt.model_validate_json(r[0]) for r in rows]

    # StoreView
    def find_by_idempotency_key(self, idempotency_key: str) -> tuple[PaymentIntent, Decision] | None:
        rows = self._all(
            "SELECT i.body, d.body FROM intents i JOIN decisions d ON d.intent_id = i.intent_id "
            "WHERE i.idempotency_key = ? ORDER BY i.rowid ASC, d.created_at ASC, d.rowid ASC LIMIT 1",
            (idempotency_key,),
        )
        pairs = self._pairs(rows)
        return pairs[0] if pairs else None

    def find_by_invoice(self, payee_id: str, invoice_ref: str) -> list[tuple[PaymentIntent, Decision]]:
        return self._pairs(
            self._all(
                "SELECT i.body, d.body FROM intents i JOIN decisions d ON d.intent_id = i.intent_id "
                "WHERE i.payee_id = ? AND i.invoice_ref = ? ORDER BY i.rowid ASC, d.rowid ASC",
                (payee_id, invoice_ref),
            )
        )

    def decisions_since(self, mandate_id: str, since: datetime) -> list[tuple[PaymentIntent, Decision]]:
        if since.tzinfo is None or since.utcoffset() is None:
            raise ValueError("since must be timezone aware")
        return self._pairs(
            self._all(
                "SELECT i.body, d.body FROM intents i JOIN decisions d ON d.intent_id = i.intent_id "
                "WHERE i.mandate_id = ? AND d.created_at >= ? ORDER BY d.created_at ASC, d.rowid ASC",
                (mandate_id, _ts(since)),
            )
        )
