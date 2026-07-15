## ADDED Requirements

### Requirement: Dispatched Alerts Persist Their Telegram Message Id

`AlertSnapshot` SHALL carry `telegram_message_id: int | None` (`None` only for pre-feature rows), populated from the `TelegramSurface.send` return value before the snapshot row is inserted, so the append-only audit contract is preserved (the row is born complete, never updated). Migration `0003` SHALL add the nullable column to `alert_snapshots`.

#### Scenario: Message id stored at dispatch

- **WHEN** `_dispatch_alert` sends a listing alert and Telegram returns message id 4711
- **THEN** the persisted `alert_snapshots` row carries `telegram_message_id = 4711`

#### Scenario: Historical rows remain valid

- **WHEN** the migration runs on a database with pre-feature alert rows
- **THEN** those rows carry `telegram_message_id = NULL` and are never watched or edited

---

### Requirement: Alerted Listings Are Watched For State Changes

Every dispatched listing alert SHALL create a watch row (mutable `alert_watches` table: alert id, listing id, entry key, message id, last-known `price_eur`, last-known `is_reserved`, `watch_until`, `last_edited_at`). The watch SHALL expire at `rendered_at + alerts.watch_days` (config, default 7 days). Terminal states SHALL be exactly: the edit target message no longer exists, and window expiry — a reserved transition SHALL NOT close the watch (flip-backs stay observable). There SHALL be no global cap on concurrent watches and no backfill of pre-feature alerts.

#### Scenario: Watch created on dispatch

- **WHEN** an alert is dispatched for listing L
- **THEN** an `alert_watches` row exists for L with the listing's current price and reserved state and `watch_until` 7 days out (default config)

#### Scenario: Expired watch is inert

- **WHEN** a watched listing changes state after `watch_until`
- **THEN** no edit is dispatched and the watch row is (lazily) pruned

#### Scenario: Deleted message closes the watch silently

- **WHEN** an edit attempt returns Telegram's "message to edit not found"
- **THEN** the watch is closed, a structured log records it, and no replacement message is ever sent

---

### Requirement: State Changes Are Detected In The Poll Cycle At Zero Marketplace Cost

The poll cycle SHALL diff freshly fetched listings against the entry's active watches BEFORE the seen-listing dedup filter discards them, using only data already present in the search response (no additional marketplace API calls). Detected transitions SHALL be: `is_reserved` false→true, `is_reserved` true→false (flip-back), and price drops meeting the threshold. A price drop SHALL trigger an edit only when it is ≥ `alerts.min_price_drop_pct` (default 1 %) AND ≥ `alerts.min_price_drop_eur` (default 0,50 €) relative to the watch's last-known price. Price increases and sub-threshold drops SHALL advance the watch's last-known price without editing.

#### Scenario: Reserved flip detected within one cadence

- **WHEN** a watched listing appears in the next poll cycle's results with `is_reserved = true`
- **THEN** an edit is dispatched in that same cycle

#### Scenario: Sub-threshold drop does not edit

- **WHEN** a watched listing's price drops 0,30 € (below the 0,50 € floor)
- **THEN** no edit is dispatched and the watch's last-known price becomes the new price

#### Scenario: Price increase never edits

- **WHEN** a watched listing's price rises
- **THEN** no edit is dispatched and the last-known price advances

#### Scenario: Dedup contract untouched

- **WHEN** a watched listing is diffed (edited or not)
- **THEN** it still never re-enters the new-alert path (`_filter_unseen` semantics unchanged)

---

### Requirement: Edits Re-Render The Full Body With A Replaceable Status Banner

On a detected change the alert body SHALL be re-rendered from the stored snapshot with the listing's CURRENT values (price line, buyer-total breakdown, Phase 2 max line all re-computed) plus a single status banner line prepended (`🔴 RESERVADO`, `🟢 Disponible de nuevo`, or `📉 <new> € (antes <last-displayed> €)`). Subsequent updates SHALL REPLACE the banner, never stack a history. The edit SHALL use `editMessageCaption` when the original alert carried a photo and `editMessageText` otherwise, branch derived from the stored snapshot's `photo_urls`, via a new `TelegramSurface.edit_alert(message_id: int, rendered: RenderedAlert, *, has_photo: bool) -> None`. `reply_markup` SHALL always be sent explicitly with the edit. The rendered edit variant SHALL fit Telegram's 1024-char photo-caption cap.

#### Scenario: Reserved banner on a photo alert

- **WHEN** a watched photo alert's listing flips to reserved
- **THEN** `editMessageCaption` is called and the caption starts with the `🔴 RESERVADO` banner over an otherwise re-rendered body

#### Scenario: Second update replaces the banner

- **WHEN** a listing already edited to `📉` later flips to reserved
- **THEN** the message shows only the `🔴 RESERVADO` banner (no stacked `📉` line); the prior update remains in `alert_updates`

#### Scenario: Identical re-render is a no-op success

- **WHEN** the edit returns Telegram's "message is not modified"
- **THEN** it is treated as success (watch state advances, `edit_ok` recorded true)

---

### Requirement: Edits Are Best-Effort, Single-Attempt, With State Advanced Only On Success

Each detected change SHALL produce at most ONE edit attempt per poll cycle; there SHALL be no in-cycle retry (the next cycle's re-diff is the retry mechanism). The watch row's last-known state SHALL advance only after a successful edit (including "not modified"); a failed attempt SHALL leave the watch unchanged so the diff re-fires next cycle. Edit failures SHALL never block or delay the alert pipeline.

#### Scenario: Transient failure retries next cycle

- **WHEN** an edit attempt fails with a network error
- **THEN** the cycle continues unaffected, the watch keeps its previous state, and the next cycle re-detects the same change and retries

---

### Requirement: Phase 2 Keyboards Are Reconstructed Safely On Edit

An edit SHALL send the keyboard the message currently deserves, reconstructed from the `callbacks` table: original phase row when no callback fired, the ack row after view/skip/snooze, and — when the last verb is `buy` and the tap is younger than a bounded suppression window (callbacks are append-only, so the marker MUST age out or a completed buy would suppress edits forever) — the edit SHALL be SKIPPED entirely (never repaint under a running buy; the diff re-fires next cycle). On reserved, a Phase 2 alert's `✅ Comprar` row SHALL be replaced with a non-tappable `🔴 Reservado` badge; on flip-back the row SHALL be restored. Phase 2 price drops SHALL receive no special keyboard treatment (preflight and reconciliation re-validate price at tap time).

#### Scenario: Comprar goes dead on reserved

- **WHEN** a watched Phase 2 alert's listing flips to reserved
- **THEN** the edited message carries the `🔴 Reservado` badge row instead of `✅ Comprar`

#### Scenario: Flip-back restores the buy row

- **WHEN** that listing later returns to available within the watch window
- **THEN** the edited message shows the `🟢 Disponible de nuevo` banner and the original Phase 2 button row again

#### Scenario: Never repaint under an in-flight buy

- **WHEN** a state change is detected while the alert's last callback verb is `buy`
- **THEN** no edit is attempted that cycle

---

### Requirement: Large Price Drops Additionally Ping

A price drop ≥ `alerts.price_drop_ping_pct` (default 10 %) SHALL, in addition to the silent edit, send a short NEW message as a Telegram reply to the original alert (e.g. `📉 Bajada: 95,00 € → 80,00 €`). Ping and edit SHALL be recorded in the same `alert_updates` row and share the success/retry semantics (state advances only when the unit of work succeeds).

#### Scenario: Big drop pings

- **WHEN** a watched listing drops from 100 € to 85 € (15 %)
- **THEN** the original message is edited AND a reply message announces the drop

#### Scenario: Ordinary drop stays silent

- **WHEN** a watched listing drops from 100 € to 95 € (5 %, above edit threshold, below ping threshold)
- **THEN** the original message is edited and no new message is sent

---

### Requirement: Every Edit Attempt Is Auditable

Every attempted edit SHALL append a row to the append-only `alert_updates` table (`audit_id`, `alert_id`, `change_kind`, `old_value`, `new_value`, `edited_at`, `edit_ok`, `rendered_text` — the full body sent to Telegram). `audit show --id <audit-id>` SHALL render an alert's update history alongside the original snapshot so the operator-visible message can be replayed at any point in time. Rows SHALL never be updated or deleted (NFR-S4).

#### Scenario: Audit row on success and failure alike

- **WHEN** an edit attempt completes (success or failure)
- **THEN** an `alert_updates` row records the change kind, values, outcome, and the rendered text that was (or would have been) sent

#### Scenario: audit show replays the history

- **WHEN** the operator runs `audit show --id <audit-id>` for an alert edited twice
- **THEN** the output includes both updates in order with their rendered bodies
