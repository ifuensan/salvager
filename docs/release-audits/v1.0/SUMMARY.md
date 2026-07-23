# v1.0 release-audit summary

| Field | Value |
|---|---|
| Run date | 2026-05-16 |
| Build SHA | `85252bc` |
| Auditor | ifuensan |
| Result | **`RESULT: PASS`** — see §1, §2, §3 verdicts + documented limitations |
| Applies to | `v0.2.0` (release candidate, published from this audit) and `v1.0.0` (future, gated on production burn-in) provided no rendering / accessibility change lands between releases |

> **About the `v1.0/` folder name.** This audit lives under
> `docs/release-audits/v1.0/` because Story 5.17 framed it as the
> pre-v1.0 stability gate. The codebase has since been versioned as
> `v0.2.0` (an interim release on the path to v1.0; see
> [`CHANGELOG.md`](../../../CHANGELOG.md)). The audit is fully
> applicable to v0.2.0 — same code, same rendering, same audit
> verdict. When v1.0.0 ships after production burn-in, re-run only
> if `domain/alert.py`, `observability/styling.py`, or
> `cli/dev_alert_fixtures.py` have changed in the interim.

Procedure: [`docs/release-checklist.md`](../../release-checklist.md). Test-chat
setup: [`SETUP.md`](SETUP.md). Reference MarkdownV2 text per variant:
[`reference-text/`](reference-text/).

---

## Re-audit delta — v0.3.1 (2026-06-14)

The original `RESULT: PASS` above was captured on `85252bc` and applies
**only as long as `domain/alert.py` is unchanged**. Two listing-renderer
changes have since landed, so the **listing surface must be re-captured**
against the v0.3.1 candidate before v1.0 promotion (ROADMAP criterion 3):

- **v0.3.0** — added the in-cycle reserved-comp row
  `💬 Comps (<n> reservados): <min> – <max> € · mediana <med> €`
  (rendered only when reserved comps exist).
- **v0.3.1** — added the unconditional deep-link row
  `🔗 Ver anuncio en <Marketplace>` (MarkdownV2 inline link to the
  listing URL), on every Phase 1 / Phase 2 listing alert.

**Re-audit scope is narrow — listing surface only.** The Phase-2-buy
renderer (receipt + 8 failure variants) and the 22 operational-event
variants are **untouched**; their 2026-05-16 PASS carries forward
unchanged. Within the listing surface:

- `reference-text/phase1-listing/` and `reference-text/phase2-listing/`
  were **regenerated to v0.3.1** in this commit (every variant gained the
  `🔗` row), and two new comp variants were added:
  `snapshot_with_comps` and `snapshot_with_single_comp` (phase1),
  `snapshot_with_comps` (phase2).
- **Pending (operator, needs devices):** re-capture the §1 listing-surface
  cells (Android + Telegram Desktop) at v0.3.1, and re-run the §2
  color-blind + §3 VoiceOver checks for the two new rows — verifying the
  deep link renders as a single tappable line (no wrap, URL not shown as
  raw text) and the comp `–`/`·`/`€` glyphs survive. New §1 invariant to
  add: **deep link opens the correct listing**.

> **Superseded by the v0.4.3 delta below** — the v0.3.1 capture cells
> were never taken; the v0.4.3 delta subsumes them.

---

## Re-audit delta — v0.4.3 (2026-07-19)

`domain/alert.py` changed four more times after the v0.3.1 delta above,
so the re-audit scope was re-derived from `git diff v0.3.1..v0.4.3`:

- **v0.3.3** — `💶` buyer-total breakdown row
  (`item + envío [(est.)] [+ Protección] = total`), between the 📍 row
  and the 🔗 deep link. **Production passes `buyer_cost` on every
  dispatch** (`poll_loop`), so every live alert since v0.3.3 carries
  this row — the pre-existing no-cost variants remain reachable only
  via the `audit show` / `explain` CLI replay (documented divergence).
- **v0.3.4** — `+ importación (est.)` term inside the 💶 row for
  non-EU listings (the Protección term is Wallapop-only).
- **v0.4.0** — the edit surface: a replaceable banner line prepended
  to a re-rendered body (`🔴 RESERVADO` · `🟢 Disponible de nuevo` ·
  `📉 <new> (antes <old>)`), the standalone `📉 Bajada:` ping message,
  and the non-tappable `🔴 Reservado` Phase 2 keyboard badge.
- **v0.4.1** — new buy-failure variant `listing_gone`
  ("El anuncio ya no está disponible (vendido o retirado)").
- **v0.4.3** — post-outcome keyboard repaint (orchestration layer, not
  `alert.py`): terminal `✅ Comprado` noop badge on success; the
  original `✅ Comprar` row restored on failure/abort. New *button*
  surfaces, no new message text.

**Code-level audit (2026-07-19): PASS.** Every new row follows the
locked single-escape-pass pattern (`escape_markdown_v2` over assembled
prose; no raw user content reaches the markup); the 49 pre-existing
reference-text files regenerated **byte-identical** (zero drift in the
previously audited surface). Anomalies, none blocking:

- **`Ebay` mis-branding (cosmetic, pre-existing).** The 📍 row and the
  🔗 deep link label render `listing.marketplace.capitalize()` →
  `Ebay`, not the brand form `eBay`. Present on every live eBay alert
  since Epic 3; FR22-locked format, so a fix is a deliberate follow-up
  release (marketplace label map), not an audit patch.
- **Stale LLM take after a price-drop edit (by design).** The edited
  body re-renders with current prices, but the take was written at
  evaluation time and may quote the old price. The 📉 banner carries
  the correction; the take is not re-evaluated on edit.

Tooling refreshed in this delta: `snapshot_with_cost` /
`snapshot_with_import` (phase1), `snapshot_with_cost` (phase2), the
five `reference-text/alert-updates/` shapes, `listing_gone` (phase2-buy)
— all snapshot-locked; `salvager dev emit-alert` registry grew 38 → 45
so every new shape is capturable on a real client.

**Pending (operator, needs devices)** — capture at v0.4.3 on Android +
Telegram Desktop, per the checklist invariants:

1. The 6 original listing cells (§1 table below — pending since the
   v0.3.1 delta) **plus** the 7 new variants: `phase1_listing_with_cost`,
   `phase1_listing_with_import`, `phase2_listing_with_cost`,
   `phase1_listing_edited_reserved`, `phase1_listing_edited_price_drop`,
   `phase2_listing_edited_reserved`, `price_drop_ping`.
2. New §1 invariants: the 💶 row stays on one line on a phone-width
   screen; the banner line renders above the headline after a real
   in-place edit (not just as a fresh message); the `🔴 Reservado` and
   `✅ Comprado` noop badges drop stray taps silently.
3. §2 color-blind pass over the new glyphs (💶 · 📉 · the 🔴/🟢 banner
   pair — banner text carries the signal, per UX-DR22).
4. One live keyboard-lifecycle eyeball: `🟡 Comprando…` →
   (`✅ Comprado` | restored `✅ Comprar`) after a real tap outcome
   (v0.4.3 repaint).

Until those cells are captured the listing-surface §1/§2/§3 verdicts
are **PENDING at v0.4.3** (the tables below still reflect the
2026-05-16 run on the pre-deep-link renderer).

### Capture results — 2026-07-20, Telegram Desktop (v0.4.4, `6cb1c7a`)

Emitted the 13 listing-surface variants from the live daemon on
hermes001 (`dev emit-alert`, message_ids 297–309) and compared each
Desktop render against its `reference-text/…` file. **All 12 captured
render byte-faithful** — every MarkdownV2 escape resolved, bold/italic
applied, the 💶 row on one line, the deep link a single tappable line:

| Variant | Desktop | Note |
|---|:-:|---|
| `phase1_listing_with_cost`          | ✓ | `💶 55,00 + 3,50 envío (est.) + 4,82 Protección = 63,32 €` |
| `phase1_listing_with_import`        | ✓ | `+ importación (est.)`, **no** Protección, `envío` not est. (eBay parses it); `Ebay` branding as documented |
| `phase2_listing_with_cost`          | ✓ | 💶 row + `Comprar · Saltar · Ver` intact |
| `phase1_listing_edited_reserved`    | ✓ | `🔴 RESERVADO` banner above headline |
| `phase1_listing_edited_price_drop`  | ✓ | `📉 48,00 € (antes 55,00 €)`; body + 💶 reflect the new price |
| `phase2_listing_edited_reserved`    | ✓ | keyboard `🔴 Reservado · 👁 Ver` |
| `phase1_listing_direct/container/missing_photo`  | ✓ | no-cost anatomy, matches refs |
| `phase2_listing_direct/container/missing_photo`  | ✓ | no-cost anatomy, matches refs |
| `price_drop_ping`                   | — | not in this batch; captured on Android instead (below) |

**Bonus — live production evidence.** The same capture session included
two **real** dispatched Corsair alerts (Badajoz, 65,00 €, real photos,
`3:29`): `💶 65,00 + 3,50 envío (est.) + 5,57 Protección = 74,07 €` on a
genuine armed 🟢 Phase 2 alert with real Gemini takes — the 💶 row is
confirmed on the live poll path, not just via `dev emit-alert`.

### Capture results — 2026-07-20, Telegram Android (v0.4.4, `6cb1c7a`)

All **13** listing variants captured on Android (11 screenshots),
including `price_drop_ping` (the Desktop gap) and the two real Corsair
production alerts. **Content byte-faithful across every variant** —
escapes resolved, bold/italic applied, emoji intact (📦🟢🔴📍💶🔗🔍📉),
edit banners above the headline, keyboards correct
(`🔴 Reservado · 👁 Ver` on a reserved Phase 2, `✅ Comprar · ❌ Saltar ·
👁 Ver` otherwise, `👁 Ver · ❌ Saltar · 😴 Posponer 24h` on Phase 1),
`price_drop_ping` plain text with no buttons/photo.

**Anomaly `!` (cosmetic, non-blocking) — the 💶 row soft-wraps to two
lines on Android** at the operator's font size, on every cost-bearing
variant:

- `💶 55,00 + 3,50 envío (est.) + 4,82` ⏎ `Protección = 63,32 €`
- `💶 55,00 + 16,82 envío + 3,63 importación` ⏎ `(est.) = 75,45 €`
- (live) `💶 65,00 + 3,50 envío (est.) + 5,57` ⏎ `Protección = 74,07 €`

The headline price (`— 55,00 €` → `€` wraps) and the
`Confidence … Phase 2 max: 60,00 €` row wrap the same way — this is
Telegram's ordinary long-line reflow, **not** a markup break: the text
is character-identical to the reference, no escape leaked, no emoji
corrupted, fully legible. It fails the *literal* "💶 on one line"
invariant but does **not** meet any blocking criterion (emoji collapse /
command unnavigable / severity-emoji corruption). **Verdict: documented
limitation, not BLOCKED.** If one-line is later deemed worth it, the fix
is a design change (shorten the row — drop the `envío`/`Protección`
words or symbol them), tracked as a post-v1.0 ticket, not an audit
patch (the row format is FR22-locked).

**§2 (color-blind) for the new glyphs:** 💶 and 📉 are informational,
not signal-bearing (the adjacent text carries the meaning); the
`🔴`/`🟢` banner circles are the already-audited severity tokens
(2026-05-16) and the banner **word** (`RESERVADO` / `Disponible de
nuevo` / `Bajada`) carries the state. No new colour-only dependency is
introduced, so the UX-DR22 contract holds by inspection; a formal Coblis
run is optional confirmation, not a gate.

**Still open (both clients now captured):** the §3 live
keyboard-lifecycle eyeball (`🟡 Comprando…` → `✅ Comprado` / restored
`✅ Comprar` after a real tap), and the "banner above headline after a
*real* in-place edit" invariant — the three edit variants were captured
as fresh emits, not true edits (a live reserved-flip / price drop on a
watched listing exercises the real edit path). Both need a live event,
not an emit.

**Side observation (not an audit item):** one screenshot's status bar
shows a `Wallapop API degradada` operational alert fired during the
session — a transient burn-in event on the already-audited operational
surface (self-recovers); noted here only so it isn't mistaken for a
capture-pass variant.

Mark each cell **`✓`** (clean), **`!`** (anomaly — drop a note + a PNG
into the per-section folder), or leave **blank** if not yet captured.
Critical anomalies (per the blocking-criteria section of the
checklist) flip the run to `BLOCKED`.

---

## §1 — Telegram client variance (UX-DR32)

Capture every (variant, context) cell as a PNG under
`telegram/<context>/<variant>.png`. Compare each capture against
`reference-text/<section>/<variant>.txt`. Verify the 4 invariants the
checklist names (emoji fidelity · MarkdownV2 fidelity · button-row
single-line · receipt photo inline).

> **¹ Scope note.** UX-DR32 names 4 contexts (iOS / Android / Desktop /
> Web). At v1.0 release time the operator (single-user per the project's
> scope contract) uses **Android + Telegram Desktop** exclusively. The
> three other context columns are marked **N/A — deferred** and tracked
> as a post-v1.0 audit item in ROADMAP: forkers running on iOS or via
> Telegram Web are encouraged to audit and open an issue if anything
> drifts; v1.0.x patch releases can address. This does not flip the run
> to BLOCKED because the columns are documented gaps, not unverified
> claims.

### Listing surface

| Variant | Android | Desktop | iOS¹ | Web Chrome¹ | Web Firefox¹ |
|---|:-:|:-:|:-:|:-:|:-:|
| `phase1_listing_direct`         | ✓ | ✓ | N/A | N/A | N/A |
| `phase1_listing_container`      | ✓ | ✓ | N/A | N/A | N/A |
| `phase1_listing_missing_photo`  | ✓ | ✓ | N/A | N/A | N/A |
| `phase2_listing_direct`         | ✓ | ✓ | N/A | N/A | N/A |
| `phase2_listing_container`      | ✓ | ✓ | N/A | N/A | N/A |
| `phase2_listing_missing_photo`  | ✓ | ✓ | N/A | N/A | N/A |

### Phase 2 buy surface (receipt + 8 failure variants)

| Variant | Android | Desktop | iOS¹ | Web Chrome¹ | Web Firefox¹ |
|---|:-:|:-:|:-:|:-:|:-:|
| `buy_success`                       | ✓ | ✓ | N/A | N/A | N/A |
| `failure_reconciliation_tripped`    | ✓ | ✓ | N/A | N/A | N/A |
| `failure_ui_check_failed`           | ✓ | ✓ | N/A | N/A | N/A |
| `failure_circuit_open`              | ✓ | ✓ | N/A | N/A | N/A |
| `failure_missing_element`           | ✓ | ✓ | N/A | N/A | N/A |
| `failure_marketplace_error`         | ✓ | ✓ | N/A | N/A | N/A |
| `failure_timeout`                   | ✓ | ✓ | N/A | N/A | N/A |
| `failure_screenshot_missing`        | ✓ | ✓ | N/A | N/A | N/A |
| `failure_payment_rail_unavailable`  | ✓ | ✓ | N/A | N/A | N/A |

### Operational surface (22 EventName variants)

| Variant | Android | Desktop | iOS¹ | Web Chrome¹ | Web Firefox¹ |
|---|:-:|:-:|:-:|:-:|:-:|
| `daemon_started`                    | ✓ | ✓ | N/A | N/A | N/A |
| `daemon_stopped`                    | ✓ | ✓ | N/A | N/A | N/A |
| `wallapop_session_expired`          | ✓ | ✓ | N/A | N/A | N/A |
| `wallapop_session_renewed`          | ✓ | ✓ | N/A | N/A | N/A |
| `wallapop_api_degraded`             | ✓ | ✓ | N/A | N/A | N/A |
| `wallapop_both_paths_down`          | ✓ | ✓ | N/A | N/A | N/A |
| `tinyfish_fallback_active`          | ✓ | ✓ | N/A | N/A | N/A |
| `tinyfish_fallback_recovered`       | ✓ | ✓ | N/A | N/A | N/A |
| `ebay_token_refresh_failed`         | ✓ | ✓ | N/A | N/A | N/A |
| `ebay_quota_breach`                 | ✓ | ✓ | N/A | N/A | N/A |
| `llm_provider_rate_limited`         | ✓ | ✓ | N/A | N/A | N/A |
| `entry_snoozed`                     | ✓ | ✓ | N/A | N/A | N/A |
| `poll_cycle_error`                  | ✓ | ✓ | N/A | N/A | N/A |
| `circuit_open`                      | ✓ | ✓ | N/A | N/A | N/A |
| `smoke_test_failed`                 | ✓ | ✓ | N/A | N/A | N/A |
| `smoke_test_recovered`              | ✓ | ✓ | N/A | N/A | N/A |
| `phase2_disabled`                   | ✓ | ✓ | N/A | N/A | N/A |
| `phase2_re_enabled`                 | ✓ | ✓ | N/A | N/A | N/A |
| `phase2_buy_callback_received`      | ✓ | ✓ | N/A | N/A | N/A |
| `phase2_screenshot_missing`         | ✓ | ✓ | N/A | N/A | N/A |
| `phase2_buy_completion_slow`        | ✓ | ✓ | N/A | N/A | N/A |
| `buy_orchestrator_error`            | ✓ | ✓ | N/A | N/A | N/A |

### §1 anomaly log

_Empty when clean. Drop one bullet per anomaly with the cell coords,
the symptom, and the captured PNG path._

- _(none)_

---

## §2 — Color-blind audit (UX-DR22)

For each simulator, view the **Android** captures (highest-saturation
context available given iOS is deferred — see §1 scope note) and check
that severity emoji + button labels remain distinguishable by
**shape + text**, never colour alone.

| Simulator | Severity emoji pass? | Button labels pass? | Anomaly PNGs |
|---|:-:|:-:|---|
| Deuteranopia (Coblis) | ✓ | ✓ | _(none — see anomaly log for documented colour shifts)_ |
| Protanopia (Coblis)   | ✓ | ✓ | _(none — see anomaly log for documented colour shifts)_ |
| Tritanopia (Coblis)   | ✓ | ✓ | _(none — see anomaly log for documented colour shifts)_ |

### §2 anomaly log

Documented cosmetic colour shifts under simulation. None affect the
UX-DR22 contract (distinguishability via shape + text holds across all
three simulators). Logged here so future auditors don't re-flag them.

- **Tritanopia (blue-blind) — `⚠️` warn glyph shifts yellow → pink.**
  Telegram's Noto Color Emoji yellow triangle re-maps under tritanopic
  simulation. **Distinguishability preserved**: shape (triangle vs
  square `ℹ️`) + bold headline ("Wallapop sin servicio", "Compra
  abortada", etc.) carry the signal. Auditor visual check: PASS.
- **Tritanopia — `🟢` Phase 2 listing emoji shifts green → light blue.**
  Cosmetic only; the `📦` Phase 1 emoji remains brown, so the
  Phase 1 vs Phase 2 distinction holds by shape + colour-family
  difference even after the green-to-blue shift.
- **Deuteranopia + Protanopia — `✅`/`❌` button glyphs converge toward
  amber.** The green ✅ Comprar and red ❌ Saltar buttons sit
  side-by-side in the Phase 2 listing keyboard; under red-blind and
  green-blind simulation their fills shift toward the same
  copper/amber hue. **Distinguishability preserved**: the checkmark vs
  cross glyph + the Spanish word labels ("Comprar" vs "Saltar")
  remain unambiguous on visual inspection. Auditor explicitly
  verified on capture `telegram/android/signal-2026-05-16-124416_013.jpeg`
  (Phase 2 direct listing alert with keyboard) — confirmed pass.

---

## §3 — VoiceOver on Terminal (UX-DR23 / UX-DR33)

Drive each command on macOS Terminal with VoiceOver running. Score the
readout end-to-end.

| Command | Reads in logical order? | Box-drawing interference? | Notes |
|---|:-:|:-:|---|
| `salvager health`              | ✗ (see below) | ✓ — but not reached: VO silent | Visual output correct; JSON workaround verified |
| `salvager audit show --last 5` | ✗ (see below) | n/a — single-line text       | Visual output correct; JSON workaround verified |
| `salvager phase2 status`       | ✗ (see below) | ✓ — but not reached: VO silent | Visual output correct; JSON workaround verified |

**Verdict: PASS with documented limitation** (not BLOCKED). Per UX-DR23
escape clause: "either patch the renderer or document the limitation
in `docs/accessibility.md`" — the v1.0 candidate exercises the second
branch.

### §3 anomaly log

- **Apple Terminal + VoiceOver does not announce Rich-rendered tabular
  output.** Verified during the 2026-05-16 audit: the three primary
  commands produce correct populated tables visually, but VoiceOver
  (`Cmd+F5` + `VO+A`) emits only whitespace / prompt sounds when asked
  to read the output. Root cause is a known limitation of Apple
  Terminal's accessibility hook with Unicode box-drawing characters +
  ANSI colour codes; not introduced by salvager. **Workaround**:
  every audited command supports `--format json`, which produces
  plain-text single-line JSON that VoiceOver reads cleanly. The
  workaround is documented in
  [`docs/accessibility.md`](../../accessibility.md) with `jq` recipes
  for each command. **Why not a v1.0 blocker**: the single-operator
  release target (per the c3 scope contract) does not depend on a
  screen reader; the JSON workflow gives any future screen-reader user
  the same information in an accessible form. Tracked in
  [`ROADMAP.md`](../../../ROADMAP.md) as a post-v1.0 nice-to-have
  (native `--plain` mode) if forker demand surfaces.

---

## Sign-off

When every section is clean (or every anomaly is patched / documented):

1. Flip the `RESULT:` field above to `PASS`.
2. Commit this file + the `telegram/`, `colorblind/`, `reference-text/` folders.
3. Proceed to Story 5.18 (tag `v1.0.0`).

If any **critical** anomaly per the checklist (emoji collapse under
simulator · primary command unnavigable in VoiceOver · severity emoji
corruption on a Telegram client), flip to `BLOCKED — <one-line reason>`
and open a release-gating bug.

---

## Pending delta — wallapop-make-offer (2026-07-23)

The offer flow (OpenSpec change `wallapop-make-offer`) adds 21 rendering
variants to the catalog (registry 45 → 66): 2 negotiable listing shapes,
2 with-offer listing shapes, `offer_sent`, 12 `offer_failure_*`, and 4
operational events. When the change ships in a release:

- the code-level audit extends over the new surfaces (golden snapshots
  already pin all 34 text+keyboard variants in
  `tests/unit/__snapshots__/test_offer_renderer_snapshots.ambr`);
- the on-device capture pass gains the emit-able newcomers
  (`dev emit-alert negotiable_listing_direct`, `offer_sent`, the 12
  failures) plus a colorblind glance at the `💰` token (informational —
  the banner word/keyboard carries state, per the §2 rule);
- the live-event items mirror the buy path: one real Ofertar tap
  eyeballed through `🟡 Ofertando…` → `💰 Oferta enviada` / restored row.
