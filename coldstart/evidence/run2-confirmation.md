# NF-1042: NOT PAID

- **Status:** Not paid. The payment is blocked.
- **Amount:** R$ 450,00 (45000 cents)
- **Payee:** Papelaria Central Ltda (`papelaria-central`), Pix key financeiro@papelariacentral.com.br
- **ATL decision:** `dec_01M7N7TVK0QT333Q21AH9DSDG8`. ATL approved it with no rule hits, so the invoice, payee, Pix key and category all matched the mandate.
- **ATL receipt id:** `rcp_01M7N7TVNKAJZEDDC205DGXKG6`. It was issued at approval, but no transfer exists (`transfer_id` is null).
- **Receipt verified:** No. I did not verify it, because no payment was made.

## What blocked it
`execute_payment` failed with `RAIL_ERROR: insufficient simulated balance`. The payment rail account does not have enough funds.

## Action needed (finance/owner)
Fund the payment account, then call `execute_payment` on the decision above. The invoice is due on 20/11/2026. I did not retry or change any values.
