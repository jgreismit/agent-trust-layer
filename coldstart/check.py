"""Check a cold-start run from ATL's own state, not from the agent's words (see CHECKS.md).

Run with the same ATL_RAIL / ATL_VAR_DIR as the run:  python coldstart/check.py <outbox-dir>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from atl.config import load_settings
from atl.service import ATLService


def main() -> int:
    outbox = Path(sys.argv[1])
    settings = load_settings()
    svc = ATLService.from_settings(settings, input_fn=lambda: "")
    rail = json.loads((settings.var_dir / "sim_rail.json").read_text())
    print(f"rail transfers: {len(rail['transfers'])}, balance {rail['balance_cents']} cents")
    for t in rail["transfers"]:
        print(f"  {t.get('pix_key')}  {t.get('amount_cents')} cents  {t.get('status')}")
    receipts = svc.store.list_receipts()
    paid = [r for r in receipts if r.transfer]
    print(f"receipts in chain: {len(receipts)} ({len(paid)} with a transfer)")
    reports = "\n".join(p.read_text() for p in sorted(outbox.glob("*.md")))
    for r in paid:
        v = svc.verify_receipt(receipt_id=r.receipt_id)
        print(
            f"  {r.receipt_id}: payee {r.intent.payee_id}, {r.intent.amount_cents} cents, "
            f"verifies={v['valid']}, cited in the agent's report={r.receipt_id in reports}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
