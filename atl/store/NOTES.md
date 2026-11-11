# L3 plan

1. `atl/store/sqlite_store.py`: SQLiteStore, one autocommit connection (WAL, busy_timeout 5000, check_same_thread=False) guarded by a threading lock.
2. Models live in `body` as model_dump_json; lookup columns filled from the model; decision created_at stored in a fixed UTC format so string comparison equals time comparison.
3. Upserts keep rowid stable so "earliest intent" for an idempotency key stays the original.
4. `atl/receipts/__init__.py`: key PEM (0600), ReceiptChain.append with seq conflict retry (5), verify_receipt, verify_chain.
5. Tests in tests/store and tests/receipts (unique file names, no __init__ files); run ruff + pytest, then commit.

# Notes for L0

- `payee_history` table in schema.sql is not used by the store (no contract method needs it).
