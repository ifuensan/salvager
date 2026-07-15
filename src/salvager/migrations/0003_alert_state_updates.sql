-- 0003_alert_state_updates.sql — live-updating alerts (edit-alerts-on-state-change).
--
-- Three pieces:
--   1. `alert_snapshots.telegram_message_id` — nullable; populated for every
--      alert dispatched by this binary onward (the send returns the id before
--      the row is inserted, so append-only is preserved: rows are born
--      complete). Historical rows stay NULL and are never watched/edited.
--   2. `alert_watches` — MUTABLE per-alert watch state (same class as
--      `wishlist_runtime_state`, NOT audit data): last-known price/reserved
--      plus the watch window. One row per dispatched alert; pruned lazily
--      after `watch_until`.
--   3. `alert_updates` — append-only audit of every ATTEMPTED edit (NFR-S4:
--      inserts only). `rendered_text` stores the full body sent to Telegram
--      so `audit show` can replay exactly what the operator's screen said.

-- Partial-application note: unlike 0001/0002 this script is not fully
-- re-runnable — SQLite has no `ADD COLUMN IF NOT EXISTS`. The ALTER comes
-- first so a duplicate-column error on a re-attempt (possible only if the
-- `_meta` version write failed after a successful script run) fails loudly
-- before any other statement; resolution is the documented manual path
-- (set _meta schema_version = 3).
ALTER TABLE alert_snapshots ADD COLUMN telegram_message_id INTEGER;

CREATE TABLE IF NOT EXISTS alert_watches (
    alert_id             TEXT PRIMARY KEY,            -- UUID, joins alert_snapshots.alert_id
    listing_id           TEXT NOT NULL,
    marketplace          TEXT NOT NULL,           -- scopes the diff join: listing_ids are only unique per marketplace
    entry_manufacturer   TEXT NOT NULL,
    entry_model          TEXT NOT NULL,
    entry_ref            TEXT NOT NULL,
    telegram_message_id  INTEGER NOT NULL,            -- denormalised for edit dispatch
    last_price_eur       TEXT NOT NULL,               -- Decimal stringified
    last_is_reserved     INTEGER NOT NULL DEFAULT 0,
    watch_until          TEXT NOT NULL,               -- ISO 8601 UTC
    last_edited_at       TEXT                         -- ISO 8601 UTC; NULL until first edit
);

CREATE INDEX IF NOT EXISTS idx_alert_watches_entry_window
    ON alert_watches (entry_manufacturer, entry_model, entry_ref, watch_until);

CREATE TABLE IF NOT EXISTS alert_updates (
    audit_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id       TEXT NOT NULL,
    change_kind    TEXT NOT NULL,                     -- "reserved" | "available" | "price_drop"
    old_value      TEXT NOT NULL,
    new_value      TEXT NOT NULL,
    edited_at      TEXT NOT NULL,                     -- ISO 8601 UTC
    edit_ok        INTEGER NOT NULL,                  -- 1 = edit (and ping, if any) succeeded
    rendered_text  TEXT NOT NULL                      -- full body sent (or attempted)
);

CREATE INDEX IF NOT EXISTS idx_alert_updates_alert_id
    ON alert_updates (alert_id);
