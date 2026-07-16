# edit-alerts-on-state-change — Design

## Context

Ground truth from the code (verified 2026-07-11):

- **Send path:** `poll_loop._dispatch_alert` renders → `telegram.send(rendered)` → persists `record_alert_snapshot` + `record_seen(match_fired=True)`. The send happens *before* the snapshot insert, so the returned `message_id` is available at insert time — it is currently bound to a throwaway (`_message_id = await telegram.send(rendered)`, `orchestration/poll_loop.py:620`) and lost.
- **What `send` does:** `TelegramBotSurface._invoke_send` (`adapters/telegram_bot/surface.py:430-450`) calls `send_photo(photo=…, caption=text)` when `rendered.photo_url` is set, else `send_message(text=…)`. Listing renderers set `photo_url = listing.photo_urls[0] if listing.photo_urls else None` — so listing alerts are *usually* photo messages (caption, 1024-char cap) but not always (text, 4096-char cap). Editing therefore needs **both** `editMessageCaption` and `editMessageText`, branch chosen per message. The branch is deterministic from the persisted `listing_json` (`photo_urls` non-empty ⇒ photo), so no extra column is strictly required — but see Decision 4.
- **Existing edit machinery:** the bot protocol already carries `edit_message_reply_markup` and the surface exposes `edit_keyboard(message_id, keyboard)` with retry classification (`surface.py:92-98, 190-212`). The callback dispatcher already mutates keyboards after send: ack rows `✓ visto / ✓ saltado / ✓ pospuesto 24h` and the Phase 2 in-flight `🟡 Comprando…` badge (`orchestration/callback_handler.py`). **Any body edit must re-send `reply_markup` or Telegram deletes the current keyboard** — and the current keyboard may no longer be the one sent (it may be an ack row or the in-flight badge).
- **Store:** `alert_snapshots` (migration `0001`, lines 62-74) has no `telegram_message_id`; `callbacks` does (`0001:91`) but only for tapped alerts. `seen_listings` stores url + timestamps + `match_fired` but **no price and no reserved flag** — there is nothing to diff against today. The Store ABC is append-only for audit rows (NFR-S4) but explicitly allows mutable non-audit state (`seen_listings`, `wishlist_runtime_state`, `phase2_state` precedent).
- **Detection points:** `Listing.is_reserved` (`domain/listing.py:66`) is populated by the fetchers at search time; `_split_reserved` (`poll_loop.py:433-444`) partitions on it — but only for *unseen* listings. `Listing.price_eur` likewise arrives fresh every cycle. Already-alerted listings still appear in every search response and are discarded by `_filter_unseen` (`poll_loop.py:418-430`) **before any comparison happens** — the state-change signal is fetched every cycle and thrown away for free.
- **Sold** has no field: on Wallapop a sold listing typically *disappears* from search results entirely; distinguishing "sold" from "fell off page 1 / keyword drift / marketplace hiccup" from absence alone is unreliable without a per-listing detail fetch (which costs quota).
- **Renderers are pure** (`render_phase1_listing_alert` / `render_phase2_listing_alert` take an `AlertSnapshot`), so re-rendering an edited body from the stored snapshot + updated listing fields is mechanical. The alert format is locked by snapshot tests (FR22) — an edited variant becomes a new locked format.
- **Telegram constraints:** bots may edit their own messages indefinitely; edits do **not** trigger a notification ping; editing a photo message can change caption but not remove the photo; editing with identical content returns a "message is not modified" 400 (must be treated as no-op success); a deleted message returns "message to edit not found" 400 (non-retryable). Rate limits are the same class as sends (~1 msg/s per chat sustained) and `RetryAfter` is already classified retryable.

## Goals / Non-Goals

**Goals:**

- An alert whose listing later flips `is_reserved` or changes `price_eur` gets its original Telegram message edited in place, within one poll cadence of the marketplace showing the change.
- Zero additional marketplace API calls: detection uses only data already present in the search responses.
- `message_id` persistence for every alert dispatched after the release, whether or not it is ever edited (also unlocks any future re-render tooling).
- Every edit is auditable (`audit show` can reconstruct what the operator's screen said and when).
- Edits are strictly best-effort: no edit failure may block or delay the alert pipeline.

**Non-Goals:**

- Backfilling/re-rendering historical alerts (no persisted `message_id`; explicitly ruled out when this was parked on 2026-06-14 and re-confirmed by the operator 2026-07-12 — Resolved Question 8).
- **Sold detection, in any form** (operator decision 2026-07-12, Resolved Question 2): no sold flag exists in search results, absence-inference is unreliable, and detail-fetches cost quota. v1 detects only positively observable changes (`is_reserved`, `price_eur`). Revisit post-burn-in.
- Re-alerting as a *new* message for ordinary state changes — editing in place is the point. The single exception is the big-drop ping (Decision 11): a price drop ≥ `alerts.price_drop_ping_pct` additionally sends a short new message, because Telegram edits are silent and a large drop is the most actionable signal the daemon produces.
- Editing operational alerts, buy receipts, or failure messages — listing alerts only.
- Any change to the dedup contract: a listing that alerted once never alerts again as "new".

## Decisions

(All Open Questions were resolved by the operator on 2026-07-12 — see the Resolved Questions section. CodeRabbit's review of the draft [PR #38 first push] is folded in: Decisions 4 and 12 answer the signature-consistency and send-then-insert findings, Decision 8 the audit-replay finding, Decision 10 the edit-failure commit-semantics finding, and the proposal no longer promises sold-state edits.)

1. **Persist `telegram_message_id` on `alert_snapshots`, set at insert time.** Migration `0003` adds a nullable `INTEGER` column; `AlertSnapshot` gains `telegram_message_id: int | None = None`. `_dispatch_alert` builds the snapshot as today, sends, then attaches the returned id (`model_copy(update=…)` or constructing the snapshot post-send) before `record_alert_snapshot`. Append-only is preserved — the row is never updated, it is simply born complete. Nullable keeps historical rows and the existing failure path (persist-after-send already tolerates partial state) valid.

2. **A dedicated mutable `alert_watches` table, not columns on `seen_listings`.** The watch needs last-known `price_eur`, last-known `is_reserved`, `watch_until`, `last_edited_at`, and the join keys (`alert_id`, `listing_id`, entry key, `telegram_message_id` denormalised for cheap lookup). `seen_listings` rows exist for *every* sighting (dropped, over-ceiling, reserved-comp) — polluting it with alert-only state and a price column would blur its dedup-index purpose and bloat the hot `is_seen` path. A separate table keyed on `alert_id` is written once per dispatched alert and read once per cycle per entry. Same mutability class as `wishlist_runtime_state` (explicitly not audit data).

3. **Hook the diff in `run_poll_cycle` before `_filter_unseen`.** After the per-entry fetch/union produces `listings_by_id`, load the entry's active watches (`watch_until > now`, small set) and inner-join on `listing_id`. For each hit, diff fetched state against the watch row; on change, dispatch an edit and update the watch row. Then proceed into the existing pipeline unchanged (`_filter_unseen` still discards seen listings from the *new-alert* path). This placement costs one indexed SELECT per entry per cycle and touches nothing downstream.

4. **`edit_alert(message_id: int, rendered: RenderedAlert, *, has_photo: bool) -> None`** — the ONE canonical signature, used verbatim everywhere this document and the specs mention it (CodeRabbit flagged draft inconsistency). New abstract method on `TelegramSurface` beside `edit_keyboard`; `TelegramBotProtocol` grows `edit_message_caption` and `edit_message_text`. The caller passes `has_photo` derived from the stored snapshot's `listing_json.photo_urls` (deterministic re-derivation; avoids a schema column *and* avoids guessing inside the adapter). `reply_markup` travels inside `rendered.inline_keyboard`, always explicitly set (Decision 6). Error handling: **single attempt per cycle** (Resolved Question 13 — the next cycle re-diffs and retries naturally; edits are non-critical); two BadRequest variants special-cased — "message is not modified" ⇒ silent no-op success, "message to edit not found" ⇒ terminal: the operator deleted the alert, the watch closes silently, no replacement is ever sent (Resolved Question 7).

5. **Re-render, don't patch.** On a state change, rebuild the full body from the stored `AlertSnapshot` (with `listing` fields updated to the freshly fetched values) through the existing renderer, then prepend a **single status banner line that is REPLACED (never stacked) by subsequent updates** (Resolved Question 3): `🔴 RESERVADO` / `🟢 Disponible de nuevo` / `📉 80,00 € (antes 95,00 €)` — "antes" is the price the operator last saw on screen. **Every derived row is re-computed from current values** (Resolved Question 10): price line, 💶 buyer-total breakdown (shipping/importación buffers included), Phase 2 max line. The message never lies; the pre-change price survives in the banner. This keeps one source of truth for alert anatomy and keeps snapshot tests meaningful.

6. **Keyboard reconstruction on edit.** The current keyboard is derivable: no callback rows for the alert ⇒ original phase row; last callback verb `view/skip/snooze` ⇒ ack row; verb `buy` ⇒ in-flight badge ⇒ **skip the edit entirely** (never repaint under a running buy). On `is_reserved` false→true on a Phase 2 alert the `✅ Comprar` row is **replaced with a non-tappable dead badge** `🔴 Reservado` (Resolved Question 5; `listing:noop:<id>` pattern already exists); on the flip-back (true→false) the original row is restored via the same reconstruction. Phase 2 price drops need **no keyboard treatment** (Resolved Question 6): preflight + reconciliation re-validate price at tap time, and a listing whose drop brings it inside a ceiling it previously exceeded never alerted, so there is no message to edit — it enters as a new alert through the normal path.

7. **Bounded watch window via config `alerts.watch_days` (default 7)** (Resolved Question 4). `watch_until = rendered_at + watch_days`; expired watches are ignored by the join and lazily pruned. **No global cap** on concurrently watched listings — alert volume already bounds the set.

8. **Append-only `alert_updates` audit table, in scope for v1** (Resolved Question 9), written for every ATTEMPTED edit: `audit_id`, `alert_id`, `change_kind`, `old_value`, `new_value`, `edited_at`, `edit_ok`, **and `rendered_text` — the full body that was sent to Telegram** (CodeRabbit: persist enough to replay the edited message; old/new values alone can't reproduce what the operator saw). `audit show <alert_id>` renders the original snapshot plus the update history. NFR-S4-compatible: inserts only.

9. **Watch lifecycle** (Resolved Question 1): the watch tracks `is_reserved` false→true (banner + dead badge), true→false flip-back (banner swapped to `🟢 Disponible de nuevo`, Comprar row restored — the watch RE-OPENS rather than closing at the first transition), and price drops. **Price increases never edit** but DO advance the watch's last-known price. Terminal states are exactly: "message to edit not found" (operator deleted it) and `watch_until` expiry. Reserved is deliberately NOT terminal — flip-backs are common on Wallapop and a re-available bargain is actionable.

10. **Edit-failure commit semantics** (CodeRabbit finding): the watch row's last-known state advances **only after a successful edit** ("message is not modified" counts as success). On a failed attempt the state is NOT advanced, so the next cycle re-detects the same diff and retries naturally — this is what makes the single-attempt budget (Decision 4) safe. The big-drop ping (Decision 11) shares the same rule: ping and edit succeed or retry together as one unit of work, recorded in the same `alert_updates` row.

11. **Price-drop threshold and big-drop ping** (Resolved Questions 11, 12, 12b). A drop edits only when it is **≥ 1 % AND ≥ 0,50 €** relative to the last-known price (config `alerts.min_price_drop_pct` / `alerts.min_price_drop_eur`, global). Sub-threshold changes advance the last-known price without editing (anti-churn against repricing bots — successive micro-drops do not accumulate into an edit). A drop **≥ 10 %** (config `alerts.price_drop_ping_pct`) additionally sends a short NEW message referencing the original alert (Telegram reply), because edits are silent and a big drop is the one transition worth a notification.

12. **The send→insert crash window is accepted** (CodeRabbit finding on send-then-insert): if the process dies between `telegram.send` and `record_alert_snapshot`, the message exists but no snapshot/watch row does — that alert is simply never watched (and, pre-existing behaviour, its callbacks already dangle). The window is milliseconds wide, the failure mode is a stale-but-visible alert (exactly today's status quo), and closing it would need an outbox/two-phase pattern that is disproportionate for a one-operator bot. Documented, not engineered away.

## Risks / Trade-offs

- [Absence ≠ sold] Inferring "sold" from a listing vanishing from results confuses pagination drift and marketplace hiccups with real sales; a wrong `🔴 Vendido` edit on a live bargain is worse than a stale alert → sold is OUT of v1 by operator decision (Resolved Question 2); only positively observable changes (`is_reserved`, `price_eur`) are detected.
- [Silent edits can hide good news] Telegram edits don't ping; a price *drop* — the most actionable transition — may go unnoticed at the bottom of the chat → mitigated by the ≥ 10 % big-drop ping (Decision 11); ordinary drops stay silent by operator choice (Resolved Question 12).
- [Sub-threshold drift] Advancing the last-known price on sub-threshold changes means many consecutive micro-drops never edit and the on-screen price can drift stale by design → accepted anti-churn trade-off (Resolved Question 11); the ≥ 1 % / ≥ 0,50 € floor keeps the drift small relative to purchase decisions.
- [Caption cap] Photo captions cap at 1024 chars; today's alerts fit, but banner + `precio anterior` lines eat margin; a long container-alert body could overflow and 400 → renderer must budget for the edit variant; snapshot tests enforce it.
- [Keyboard races] An operator tap landing between diff and edit could repaint an ack row with a stale keyboard; the callbacks-table read narrows but can't eliminate the window → acceptable for a one-operator bot; the audit trail stays correct either way.
- [Watch-table growth] One row per alert is negligible at observed alert volume; lazy pruning on expiry keeps the join cheap.
- [Format lock churn] The edited-alert variant is a new locked format (FR22); every future renderer tweak now maintains two variants → contained by reusing the base renderer and appending the state treatment (Decision 5).
- [Migration on live deploy] `0003` runs via the tracked runner on hermes001 restart; nullable column + two new tables = no rewrite of existing data; rollback = repin previous tag (older binary ignores the new tables; `SchemaDriftError` is not triggered because version only moves forward — a genuine downgrade needs the documented manual path).

## Migration Plan

Normal tag-driven release. Migration `0003_alert_state_updates.sql`: `ALTER TABLE alert_snapshots ADD COLUMN telegram_message_id INTEGER` (nullable), `CREATE TABLE alert_watches …`, `CREATE TABLE alert_updates …`, indexes on `alert_watches (entry_manufacturer, entry_model, entry_ref, watch_until)` and `alert_updates (alert_id)`. Deploy to hermes001 = image bump + quadlet restart; the runner applies `0003` idempotently. No behaviour change for anything already in the DB — watching starts with the first alert dispatched by the new binary.

## Resolved Questions (operator, 2026-07-12)

1. **Transitions:** reserved (false→true), price drops, AND reserved→available flip-back (watch re-opens, banner swaps to available, Comprar restored). Price increases never edit (they advance last-known price silently).
2. **Sold:** out of scope for v1 entirely. Revisit post-burn-in if stale sold listings prove annoying.
3. **Presentation:** single status banner line prepended; subsequent updates REPLACE the banner (no stacked history — history lives in `alert_updates`). Body stays intact and re-rendered.
4. **Watch window:** `alerts.watch_days = 7`, configurable. No global cap on watched listings.
5. **Phase 2 on reserved:** `✅ Comprar` row replaced with a non-tappable `🔴 Reservado` dead badge (restored on flip-back).
6. **Phase 2 on price drop:** no extra treatment — preflight/reconciliation re-validate at tap time; never-alerted listings that drop into range enter as new alerts (confirmed fine).
7. **Deleted message:** close the watch silently; never send a replacement.
8. **Historical alerts:** never watched, no backfill of any kind.
9. **Audit:** `alert_updates` table + `audit show` integration in v1.
10. **Re-render scope:** full body re-rendered to current values (buyer total, Phase 2 max, price line); the pre-change price survives in the banner.
11. **Drop threshold:** edit only on drops ≥ 1 % AND ≥ 0,50 € (global config `alerts.min_price_drop_pct` / `alerts.min_price_drop_eur`); sub-threshold changes advance last-known price without editing.
12. **Notification for drops:** silent edit normally; a drop ≥ 10 % (`alerts.price_drop_ping_pct`) ALSO sends a short new message replying to the original alert.
13. **Retry budget:** single attempt per cycle; the next cycle's re-diff is the retry mechanism.
