-- 0004_offer_schema.sql — Wallapop offer-flow schema (wallapop-offer-flow).
--
-- Two pieces, mirroring the Phase 2 split (0002):
--   1. `offers` — append-only: one row per EXECUTED offer attempt (success
--      or failure). Application-layer append-only via `OfferAuditWriter`
--      (INSERT-only methods; the append-only lint test covers it). Offer
--      *taps* are not duplicated here — they ride the existing `callbacks`
--      audit path like every other verb.
--   2. `offer_state` — the single mutable lockout row (same class as
--      `phase2_state`, deliberately SEPARATE from it: offer failures must
--      never block real buys, and vice versa).
--
-- `status` is `'sent'` for every v1 row; a future acceptance-detection
-- change extends the value set without a schema break.
--
-- Idempotent: every statement is `IF NOT EXISTS` / `INSERT OR IGNORE`,
-- so re-running the migration after a partial application is safe.

CREATE TABLE IF NOT EXISTS offers (
    audit_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id            TEXT NOT NULL,        -- UUID, joins alert_snapshots.alert_id
    listing_id          TEXT NOT NULL,
    marketplace         TEXT NOT NULL,        -- listing_ids are only unique per marketplace
    entry_manufacturer  TEXT NOT NULL,
    entry_model         TEXT NOT NULL,
    entry_ref           TEXT NOT NULL,
    offered_eur         TEXT NOT NULL,        -- Decimal stringified (whole euros in v1)
    asking_eur          TEXT NOT NULL,        -- Decimal stringified; asking item price at tap time
    outcome             TEXT NOT NULL,        -- "success" | "failure" | "aborted"
    failure_reason      TEXT,                 -- OfferFailureReason value; NULL on success
    screenshot_path     TEXT,                 -- NULL when the agent produced none
    platform_remaining  INTEGER,              -- "N ofertas restantes" counter when the agent saw it
    status              TEXT NOT NULL DEFAULT 'sent',  -- v1: 'sent' (successful sends only meaning)
    attempted_at        TEXT NOT NULL         -- ISO 8601 UTC
);

CREATE INDEX IF NOT EXISTS idx_offers_listing
    ON offers (marketplace, listing_id);

CREATE INDEX IF NOT EXISTS idx_offers_attempted_at
    ON offers (attempted_at);

-- ─────────────────────────────────────────────────────────────────────
-- offer_state — the single-row offer lockout state. `CHECK (id = 1)`
-- plus the seed row guarantee exactly one row to UPDATE in place.
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS offer_state (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    globally_disabled     INTEGER NOT NULL DEFAULT 0,   -- 0 = active, 1 = locked out
    disabled_at           TEXT,                         -- ISO 8601 UTC, NULL when active
    disabled_reason       TEXT,                         -- NULL when active
    consecutive_failures  INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO offer_state (id, globally_disabled, consecutive_failures)
    VALUES (1, 0, 0);
