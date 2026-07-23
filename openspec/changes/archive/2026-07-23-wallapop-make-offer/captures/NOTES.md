# Capture notes — wallapop-make-offer

## App captures (2026-07-22, files in this directory)

- `app-listing-hacer-oferta-button.jpg` — listing page with "Hacer oferta"
  next to Comprar.
- `app-offer-form-10-restantes.jpg` — offer form showing the
  "10 ofertas restantes para hoy" counter.
- `app-offer-form-floor-30pct.jpg` — inline floor validation:
  "Tu oferta debe ser de al menos 35€ (-30%)" on a 50 € item.

## Web verification (2026-07-23, operator screenshots — task 5.1)

Confirmed on es.wallapop.com (desktop web, authenticated session):

- The listing page (`/item/<slug>`) shows a "Hacer oferta" button
  directly under Comprar — same entry point as the app.
- Clicking opens the "Hacer una oferta" modal, addressable at
  `/app/chat/offer?itemId=<internal id>` (the SAME internal id the
  reconciliation re-fetch uses; carried in the agent goal as a
  recovery hint).
- The same "10 ofertas restantes para hoy" counter renders in the modal.
- The amount field accepts cents using a COMMA separator ("20,22"
  observed accepted, Enviar enabled). Salvager's amounts are whole
  euros, so no decimals are ever entered.
- No -30 % floor message was visible client-side at a below-floor
  amount on web — validation may happen on submit; the domain
  pre-validates the floor and `amount_rejected` covers a refusal.
