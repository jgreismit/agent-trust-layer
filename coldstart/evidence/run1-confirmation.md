# NF-1042 – Payment NOT made

- **Status:** Not paid (refused by ATL)
- **Amount:** R$ 450,00 (45000 cents)
- **Payee:** Papelaria Central Ltda (Pix: financeiro@papelariacentral.com.br)
- **Mandate / agent:** mdt_loja_ap / ap-agent-01
- **ATL decision:** dec_01M7N7PN14D3361TGVZ11RFC0E, verdict `refuse`, rule R2_UNKNOWN_PAYEE
- **Reason:** "Payee papelaria_central is not in the mandate"
- **ATL receipt id (refusal record):** rcp_01M7N7PN3VMKYY9KXC3F4PW3DK. It records the refusal and was not verified, because no money moved.

## What blocked it
The payee is not registered in the mandate. I did not know the registered payee id and used `papelaria_central`. The mandate may list this supplier under another id, or the supplier may not be registered at all. I did not retry or change any values, as ATL instructs.

## Action needed from the owner
Confirm whether Papelaria Central Ltda should be a payee under `mdt_loja_ap`, and give the payee id and Pix key to register. The invoice is due 20/11/2026.
