## MODIFIED Requirements

### Requirement: Phase 2 Keyboards Are Reconstructed Safely On Edit

An edit SHALL send the keyboard the message currently deserves, reconstructed from the `callbacks` table: original phase row when no callback fired, the ack row after view/skip/snooze, and — when the last verb is `buy` and the tap is younger than a bounded suppression window (callbacks are append-only, so the marker MUST age out or a completed buy would suppress edits forever) — the edit SHALL be SKIPPED entirely (never repaint under a running buy; the diff re-fires next cycle). On reserved, a Phase 2 alert's `✅ Comprar` row SHALL be replaced with a non-tappable `🔴 Reservado` badge; on flip-back the row SHALL be restored. Phase 2 price drops SHALL receive no special keyboard treatment (preflight and reconciliation re-validate price at tap time).

Reconstruction SHALL additionally account for offer state (see `wallapop-offer-flow`): when the last verb is `offer` and the tap is younger than the same bounded suppression window, the edit SHALL be SKIPPED entirely (never repaint under a running offer); an alert with a recorded successful offer SHALL keep its terminal `💰 Oferta enviada` badge across every edit; an un-offered Ofertar row SHALL go dead on reserved and be restored on flip-back, like the Comprar row; and price-change edits SHALL re-derive Ofertar eligibility and the offer amount from the listing's current values (a drop into the ceiling re-renders the alert with its standard body and keyboard for the entry).

#### Scenario: Comprar goes dead on reserved

- **WHEN** a watched Phase 2 alert's listing flips to reserved
- **THEN** the edited message carries the `🔴 Reservado` badge row instead of `✅ Comprar`

#### Scenario: Flip-back restores the buy row

- **WHEN** that listing later returns to available within the watch window
- **THEN** the edited message shows the `🟢 Disponible de nuevo` banner and the original Phase 2 button row again

#### Scenario: Never repaint under an in-flight buy

- **WHEN** a state change is detected while the alert's last callback verb is `buy`
- **THEN** no edit is attempted that cycle

#### Scenario: Never repaint under an in-flight offer

- **WHEN** a state change is detected while the alert's last callback verb is `offer` and the tap is within the suppression window
- **THEN** no edit is attempted that cycle

#### Scenario: Oferta enviada badge survives reconstruction

- **WHEN** a watched alert with a recorded successful offer is edited for any state change
- **THEN** the reconstructed keyboard still carries the `💰 Oferta enviada` badge

#### Scenario: Price drop re-derives the offer surface

- **WHEN** a watched negotiable alert's listing drops in price
- **THEN** the edited message's offer line and keyboard reflect the recomputed amount — or the standard alert surface when the buyer total is now at or under the ceiling
