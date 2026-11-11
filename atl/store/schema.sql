-- ATL SQLite schema. Owned by L0. Models are stored as canonical JSON in `body`;
-- other columns exist only for lookups. Times are UTC ISO 8601 strings.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS mandates (
    mandate_id   TEXT PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    revoked      INTEGER NOT NULL DEFAULT 0,
    body         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intents (
    intent_id        TEXT PRIMARY KEY,
    mandate_id       TEXT NOT NULL,
    payee_id         TEXT NOT NULL,
    invoice_ref      TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL,
    amount_cents     INTEGER NOT NULL,
    body             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_intents_idem ON intents (idempotency_key);
CREATE INDEX IF NOT EXISTS ix_intents_invoice ON intents (payee_id, invoice_ref);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id  TEXT PRIMARY KEY,
    intent_id    TEXT NOT NULL REFERENCES intents (intent_id),
    verdict      TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    body         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_intent ON decisions (intent_id);
CREATE INDEX IF NOT EXISTS ix_decisions_created ON decisions (created_at);

CREATE TABLE IF NOT EXISTS approvals (
    decision_id  TEXT PRIMARY KEY REFERENCES decisions (decision_id),
    action       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    body         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transfers (
    decision_id  TEXT PRIMARY KEY REFERENCES decisions (decision_id),
    transfer_id  TEXT NOT NULL,
    rail         TEXT NOT NULL,
    body         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipts (
    seq          INTEGER PRIMARY KEY,
    receipt_id   TEXT NOT NULL UNIQUE,
    decision_id  TEXT NOT NULL,
    hash         TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    body         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_receipts_decision ON receipts (decision_id);

CREATE TABLE IF NOT EXISTS payee_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    mandate_id   TEXT NOT NULL,
    payee_id     TEXT NOT NULL,
    pix_key      TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    decision_id  TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_payee_history ON payee_history (mandate_id, payee_id, created_at);
