# edit-alerts-on-state-change

## Why

Once an alert fires, it is frozen: the daemon marks the `(listing, entry)` pair as seen and never looks at that listing again, even though every subsequent poll cycle still fetches it in the search results and silently discards it at the dedup filter (`poll_loop._filter_unseen`). When the listing is later reserved or drops its price, the Telegram alert the operator acts on is stale — worst case the operator (or Phase 2's armed `✅ Comprar` button) chases inventory that is already gone, or misses that a watched item just became cheaper. (Sold-state detection is explicitly out of scope for v1 — search results carry no sold signal and inferring it is unreliable; see design.md Resolved Question 2.)

Editing the original message is impossible today for two reasons, both verified in code:

1. **The Telegram `message_id` is discarded.** `TelegramSurface.send` returns it (`adapters/telegram_bot/surface.py:188`), but `_dispatch_alert` binds it to a throwaway (`_message_id`, `orchestration/poll_loop.py:620`) and `alert_snapshots` has no column for it. Only tapped alerts leave a recoverable id (in the `callbacks` table).
2. **The surface has no body-edit method.** `TelegramSurface` exposes `send`, `edit_keyboard` (= `editMessageReplyMarkup`, keyboard only) and `listen_callbacks` — no `editMessageCaption`/`editMessageText`.

Telegram bots can edit their own messages with no age limit, so the API is not the blocker — the missing id and the missing method are. This was parked on 2026-06-14 as a forward-looking feature (historical backfill is explicitly out: pre-feature alerts have no persisted id and never will).

## What Changes

- **Persist the `message_id`.** `AlertSnapshot` gains `telegram_message_id: int | None`; `_dispatch_alert` stops discarding the send result and stores it on the snapshot row. New migration `0003` adds a nullable `telegram_message_id` column to `alert_snapshots` (nullable so historical rows stay valid; the value is known before the row is inserted, so the append-only audit contract is untouched).
- **Add a body-edit method to the Telegram port.** `TelegramSurface.edit_alert(message_id, rendered)` implemented in `TelegramBotSurface` with the same retry/config-error classification as `send`. It calls `editMessageCaption` when the original alert carried a photo (listing alerts are sent via `send_photo` whenever `listing.photo_urls` is non-empty) and `editMessageText` otherwise; the photo/text branch is re-derived deterministically from the stored `listing_json`.
- **Watch alerted listings for state changes.** A new small mutable table (working name `alert_watches`, same class of state as `wishlist_runtime_state`, NOT audit data) tracks each dispatched alert's last-known state: `alert_id`, `listing_id`, entry key, `telegram_message_id`, last-known `price_eur`, last-known `is_reserved`, `watch_until`, `last_edited_at`. The poll cycle, before the dedup filter drops seen listings, intersects the fetched results with the entry's active watch set and diffs state (`is_reserved` flip, `price_eur` change). Zero extra marketplace calls — the data is already in the search response we currently throw away.
- **On a detected change** (reserved flip in either direction; price drop ≥ 1 % and ≥ 0,50 €): re-render the full alert body from the stored snapshot (renderers are pure) with current listing values, prepend a single replaceable status banner (`🔴 RESERVADO` / `🟢 Disponible de nuevo` / `📉 80,00 € (antes 95,00 €)`), edit the original message, advance the watch row **only on success**, and append a row (including the rendered text) to a new append-only `alert_updates` audit table so `audit show` can replay what the operator saw. Edits are best-effort, single-attempt: a failure never blocks the poll cycle — the next cycle re-diffs and retries.
- **Big-drop ping:** a price drop ≥ 10 % (`alerts.price_drop_ping_pct`) additionally sends a short new message replying to the original alert — Telegram edits are silent, and a large drop is the one transition worth a notification.
- **Phase 2 interplay:** an edit on a Phase 2 alert must pass `reply_markup` explicitly or Telegram drops the button row. On reserved the `✅ Comprar` row is replaced with a non-tappable `🔴 Reservado` badge (restored on flip-back); an alert whose buy is in-flight (`🟡 Comprando…`) is never edited.
- **Bounded watching:** listings are watched for `alerts.watch_days` (default 7); terminal states are message-deleted and window expiry only.

## Capabilities

### New Capabilities

- `listing-alert-state-updates`: persisting the Telegram message id, detecting state changes on already-alerted listings within the existing poll cycle, editing the original alert message, and auditing those edits.

### Modified Capabilities

- None at the spec level. The Comprar-row treatment on edits (dead badge on reserved, restore on flip-back, never repaint under an in-flight buy) is owned by the new `listing-alert-state-updates` capability; `phase2-purchase-flow`'s own requirements (preflight, buy gates, reconciliation) are unchanged — decided with the operator 2026-07-12 (design.md Resolved Questions 5/6).

## Impact

- **Code:** `domain/alert.py` (snapshot field + state-banner render variant), `interfaces/telegram_surface.py` + `adapters/telegram_bot/surface.py` (new `edit_alert`, protocol grows `edit_message_caption`/`edit_message_text`), `interfaces/store.py` + `adapters/sqlite_store/store.py` (persist message_id; watch-row CRUD; `alert_updates` append), `migrations/0003_*.sql`, `orchestration/poll_loop.py` (watch-diff hook before `_filter_unseen`, edit dispatch), `config/config_yaml.py` (new `alerts` section: `watch_days=7`, `min_price_drop_pct=1`, `min_price_drop_eur=0.50`, `price_drop_ping_pct=10`), `cli/commands/audit*` (`audit show` update-history replay), snapshot tests for the new locked edited-alert format.
- **DB:** one migration — nullable column on `alert_snapshots`, new mutable `alert_watches` table, new append-only `alert_updates` table. Existing rows untouched; pre-feature alerts are simply never editable.
- **Quota:** zero extra marketplace API calls (diff uses data already fetched). Telegram edits are bounded by the watch window and per-change deduping; the existing retry/backoff policy (`RetryAfter` is retryable) already covers edit rate-limit responses.
- **Ops:** normal tag-driven release to hermes001 (image bump + quadlet restart); migration runs automatically via the tracked runner (AR10). Only alerts sent after the release gain live updates.
