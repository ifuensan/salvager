# On-device capture pass — `dev emit-alert` commands (v0.4.4)

Run each against the **live daemon on hermes001** so the message lands in
the configured audit chat, then screenshot it on **Android + Telegram
Desktop**. Compare each capture against
`reference-text/<section>/<variant>.txt`. Mark the §1 cell `✓` / `!` in
`SUMMARY.md`.

Prereq: hermes001 must be running **`:0.4.4`** (v0.4.3 has only 38
variants — the 7 new ones below error as "Unknown variant" on it).
Verify first:

```bash
ssh centos@192.168.1.173 'podman exec salvager salvager dev list-variants | tail -1'
# → "45 variants total."
```

## The 13 listing-surface variants

```bash
# ── The 7 that match live production anatomy (💶 row / edit surface) ──
for v in \
  phase1_listing_with_cost \
  phase1_listing_with_import \
  phase2_listing_with_cost \
  phase1_listing_edited_reserved \
  phase1_listing_edited_price_drop \
  phase2_listing_edited_reserved \
  price_drop_ping \
; do ssh centos@192.168.1.173 "podman exec salvager salvager dev emit-alert $v"; done

# ── The 6 original cells (no-cost anatomy; pending capture since v0.3.1) ──
for v in \
  phase1_listing_direct \
  phase1_listing_container \
  phase1_listing_missing_photo \
  phase2_listing_direct \
  phase2_listing_container \
  phase2_listing_missing_photo \
; do ssh centos@192.168.1.173 "podman exec salvager salvager dev emit-alert $v"; done
```

Emit one at a time if you want to keep the chat readable — each prints
`OK sent <variant> as message_id=<n>`.

## What to verify on each capture (checklist invariants)

- **💶 row on one line** on a phone-width screen (`with_cost`,
  `with_import`, and every edit variant carry it).
- **`with_import`**: shows `+ importación (est.)`, **no** `Protección`
  term, and the label reads `Ebay` (documented cosmetic anomaly — not a
  fail).
- **Edit banners** render above the headline: `🔴 RESERVADO`,
  `📉 <new> (antes <old>)`. NB: `dev emit-alert` sends these as a *fresh*
  message; also eyeball one **real** in-place edit in the wild (a
  watched listing flipping reserved / dropping price) to confirm the
  banner sits above the body after a true edit, not just on first send.
- **`phase2_listing_edited_reserved`**: keyboard is `🔴 Reservado · 👁 Ver`
  and a tap on the badge does nothing (noop verb).
- **`price_drop_ping`**: plain text, no photo, no buttons.

## §2 color-blind (Coblis, on the Android captures)

New glyphs to eyeball across Deuteranopia / Protanopia / Tritanopia:
`💶` · `📉` · the `🔴`/`🟢` banner pair. Signal must survive on shape +
text, never colour alone (UX-DR22).

## §3 keyboard-lifecycle eyeball (v0.4.3 repaint)

Not an emit — needs a real tap. On the next armed 🟢 buy alert, confirm
`🟡 Comprando…` repaints to either `✅ Comprado` (success) or the
restored `✅ Comprar` row (failure/abort).
