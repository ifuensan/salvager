-- 0002_phase2_schema.sql — Phase 2 audit schema (AR8 / AR9 / AR13 / NFR-S4).
--
-- Adds the four Phase 2 tables on top of the Phase 1 base (migration
-- 0001). The three audit tables (`tap_events`, `transactions`,
-- `phase2_smoke_tests`) are append-only at the application layer:
-- `Phase2AuditWriter` exposes only INSERT-backed `record_*` methods and
-- a property test (`test_audit_writer_append_only.py`) mechanically
-- rejects any `update_*` / `delete_*` method. The DB layer is
-- intentionally not the gate — the writer API is.
--
-- `phase2_state` is the one mutable Phase 2 table: a single-row lockout
-- + circuit-breaker counter that must survive daemon restarts (AR13).
-- It is updated in place; that is by design and is why it is NOT one of
-- the append-only audit tables.
--
-- Idempotent: every statement is `IF NOT EXISTS` / `INSERT OR IGNORE`,
-- so re-running the migration after a partial application is safe.

-- ─────────────────────────────────────────────────────────────────────
-- tap_events — one row per Phase 2 inline-button tap (the `✅ Comprar`
-- confirmation). `alert_id` joins back to alert_snapshots; not a SQL FK
-- since PRAGMA foreign_keys is OFF (the application is the join authority).
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tap_events (
    audit_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id        TEXT NOT NULL,
    verb            TEXT NOT NULL,            -- "buy" | "skip" | "view"
    raw_payload     TEXT NOT NULL,            -- JSON: the full Telegram callback payload
    tapped_at       TEXT NOT NULL,            -- ISO 8601 UTC
    ip_or_chat_id   TEXT NOT NULL             -- chat id (or source identifier) of the tap
);

CREATE INDEX IF NOT EXISTS idx_tap_events_alert_id
    ON tap_events (alert_id);

CREATE INDEX IF NOT EXISTS idx_tap_events_tapped_at
    ON tap_events (tapped_at);

-- ─────────────────────────────────────────────────────────────────────
-- transactions — one row per completed autonomous purchase. DECIMAL
-- columns are stored as TEXT (stringified Decimal) so no float rounding
-- ever touches a price — same convention as alert_snapshots.phase2_max_price_eur.
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS transactions (
    audit_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id          TEXT NOT NULL,
    price_paid_eur    TEXT NOT NULL,          -- Decimal stringified
    payment_method    TEXT NOT NULL,          -- "wallapop_pay" | "ebay_checkout"
    receipt_id        TEXT NOT NULL,
    screenshot_path   TEXT NOT NULL,
    total_seconds     INTEGER NOT NULL,       -- wall-clock from tap to receipt
    committed_at      TEXT NOT NULL           -- ISO 8601 UTC
);

CREATE INDEX IF NOT EXISTS idx_transactions_alert_id
    ON transactions (alert_id);

CREATE INDEX IF NOT EXISTS idx_transactions_committed_at
    ON transactions (committed_at);

-- ─────────────────────────────────────────────────────────────────────
-- phase2_smoke_tests — one row per daily synthetic smoke-test run
-- (Story 5.6). Captures the parsed vs. independently-verified price so
-- a parser-drift regression (the Q9 scenario) is auditable after the fact.
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS phase2_smoke_tests (
    audit_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at              TEXT NOT NULL,        -- ISO 8601 UTC
    result              TEXT NOT NULL,        -- "pass" | "fail"
    parsed_price        TEXT NOT NULL,        -- Decimal stringified
    independent_price   TEXT NOT NULL,        -- Decimal stringified
    delta_eur           TEXT NOT NULL,        -- Decimal stringified
    delta_pct           TEXT NOT NULL         -- Decimal stringified
);

CREATE INDEX IF NOT EXISTS idx_phase2_smoke_tests_run_at
    ON phase2_smoke_tests (run_at);

-- ─────────────────────────────────────────────────────────────────────
-- phase2_state — the single-row Phase 2 lockout + circuit-breaker state.
-- The `CHECK (id = 1)` constraint plus the seed `INSERT OR IGNORE` below
-- guarantee there is always exactly one row to UPDATE in place (AR13).
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS phase2_state (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    globally_disabled     INTEGER NOT NULL DEFAULT 0,   -- 0 = active, 1 = locked out
    disabled_at           TEXT,                         -- ISO 8601 UTC, NULL when active
    disabled_reason       TEXT,                         -- NULL when active
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    last_smoke_result     TEXT,                         -- "pass" | "fail" | NULL (never run)
    last_smoke_at         TEXT                          -- ISO 8601 UTC, NULL when never run
);

INSERT OR IGNORE INTO phase2_state (id, globally_disabled, consecutive_failures)
    VALUES (1, 0, 0);
