## 1. Schema & store

- [x] 1.1 Migration `0003_alert_state_updates.sql`: nullable `telegram_message_id` on `alert_snapshots`; mutable `alert_watches` (alert_id PK, listing_id, entry key, telegram_message_id, last_price_eur, last_is_reserved, watch_until, last_edited_at) with index on (entry key, watch_until); append-only `alert_updates` (audit_id, alert_id, change_kind, old_value, new_value, edited_at, edit_ok, rendered_text) with index on alert_id
- [x] 1.2 `AlertSnapshot.telegram_message_id: int | None = None`; Store ABC + SQLite impl: persist it on `record_alert_snapshot`, plus `create_watch`, `active_watches(entry_key, now)`, `advance_watch`, `close_watch`, `record_alert_update`, lazy pruning of expired watches
- [x] 1.3 Store tests: id persisted, watch CRUD, expiry filter, append-only guarantees on `alert_updates`, migration idempotence on a v0.3.x database copy

## 2. Config

- [x] 2.1 New `alerts` section in `config_yaml.py`: `watch_days=7` (ge=1), `min_price_drop_pct=1` (ge=0), `min_price_drop_eur=Decimal("0.50")` (ge=0), `price_drop_ping_pct=10` (ge=0); document in `config.example.yaml` + sync bundled template
- [x] 2.2 Config tests: defaults, YAML overrides, rejection of negatives

## 3. Telegram surface

- [x] 3.1 `TelegramSurface.edit_alert(message_id: int, rendered: RenderedAlert, *, has_photo: bool) -> None`; `TelegramBotProtocol` grows `edit_message_caption` + `edit_message_text`; single attempt, "message is not modified" → no-op success, "message to edit not found" → raise a typed terminal error the caller maps to close_watch
- [x] 3.2 Surface tests: caption vs text branch, explicit reply_markup always sent, both BadRequest special cases, no retry loop

## 4. Rendering

- [x] 4.1 Edited-alert render variant: status banner line (`🔴 RESERVADO` / `🟢 Disponible de nuevo` / `📉 <new> € (antes <old> €)`) prepended to the re-rendered body (current values incl. buyer-total breakdown); banner replaces, never stacks
- [x] 4.2 Keyboard reconstruction helper from `callbacks` rows (none → phase row; view/skip/snooze → ack row; buy → sentinel "skip edit"); Phase 2 dead badge `🔴 Reservado` + flip-back restore
- [x] 4.3 Big-drop ping renderer (short reply message `📉 Bajada: X € → Y €`)
- [x] 4.4 Snapshot tests: new locked formats (banner variants, dead-badge keyboard, ping message); 1024-char caption budget asserted on the longest container-alert fixture

## 5. Poll-loop integration

- [x] 5.1 `_dispatch_alert`: stop discarding `message_id` — attach to snapshot pre-insert, create watch row
- [x] 5.2 Watch-diff hook in `run_poll_cycle` before `_filter_unseen`: join fetched listings against active watches; transitions per spec (reserved both directions; drop ≥ pct AND ≥ eur); increases/sub-threshold advance last-known silently
- [x] 5.3 Edit dispatch unit of work: re-render → reconstruct keyboard (skip if buy in-flight) → edit_alert → (ping if ≥ ping_pct) → advance watch ONLY on success → append `alert_updates` row (success and failure); failures never block the cycle
- [x] 5.4 Poll-loop tests: reserved edit within one cycle, flip-back restores, sub-threshold no-op, increase no-op, big-drop edit+ping, failed edit retried next cycle, deleted message closes watch, in-flight buy skips edit, dedup path untouched

## 6. Audit CLI

- [x] 6.1 `audit show <alert-id>`: render update history (change kind, values, outcome, rendered body) after the original snapshot
- [x] 6.2 CLI tests: alert with 0, 1 and 2 updates

## 7. Verification & docs

- [x] 7.1 Full gate: ruff + format + mypy + pytest green; `openspec validate edit-alerts-on-state-change --strict` passes
- [x] 7.2 CHANGELOG entry under [Unreleased]; README status blurb mention (live-updating alerts)
