## Context

PR #7 partitions reserved Wallapop listings out of the buy path inside `run_poll_cycle` (`_split_reserved` → `buyable, reserved`). The reserved set is already in scope at dispatch time: the cycle logs `reserved_comps_observed` with the comp prices and records them as seen, but the prices never reach the operator's alert. The only place that turns reserved prices into a count/min/median/max summary is `cli/commands/test_search_cmd.py::_comp_summary_line`, which carries the even-length-median fix Devin flagged on PR #7.

The alert renderers (`domain/alert.py`) are pure functions producing `RenderedAlert`. Phase 2 already demonstrates the extension pattern this change follows: `render_phase2_listing_alert` takes `phase2_max_price_eur` as a render-time argument that is NOT stored on `AlertSnapshot`. The alert output is locked at v1 (FR22) and guarded by snapshot tests in `test_alert_renderer_snapshots.py` (Phase 1) and `test_phase2_renderer_snapshots.py` (Phase 2).

## Goals / Non-Goals

**Goals:**
- Surface an in-cycle reserved-comp summary line on buyable Phase 1 and Phase 2 listing alerts.
- Single source of truth for the comp arithmetic, shared by the alert renderers and the `test-search` footer.
- Zero persistence/schema/prompt impact — render-time signal only.

**Non-Goals:**
- Cross-cycle comp history / trend persistence (a separate later item).
- Feeding comps into the LLM evaluator or any scoring (FR17 prompt boundary stays intact).
- Changing how reserved listings are partitioned, logged, or recorded as seen.
- Comps for operational/receipt/failure alerts — listing alerts only.

## Decisions

**1. New `domain/comps.py` value object over reusing the CLI helper.**
`_comp_summary_line` returns an English one-line string built for the CLI table footer; the alert needs es-ES MarkdownV2 output with different wording. Sharing the *arithmetic* (not the string) is the right seam. Introduce `CompSummary` (frozen dataclass: `count`, `min_eur`, `median_eur`, `max_eur`, all `Decimal`) plus `summarize_comps(prices: Iterable[Decimal]) -> CompSummary | None` returning `None` for empty input. The CLI footer and the alert line both format from a `CompSummary`. _Alternative considered:_ leave the helper in the CLI and import it into `domain` — rejected, it inverts the dependency direction (domain importing from cli) and keeps the English/es-ES formatting tangled with the math.

**2. Comp line is a render-time arg, not an `AlertSnapshot` field.**
Mirror `phase2_max_price_eur`: `render_phase1_listing_alert(snapshot, *, comp_summary: CompSummary | None = None)` and `render_phase2_listing_alert(snapshot, phase2_max_price_eur, *, comp_summary=None)`. Callbacks edit the keyboard, never re-render the body, so the snapshot never needs the comp data. This keeps the change schema-free and keeps comps from ever influencing the buy/audit path. _Alternative considered:_ add `comp_summary` to `AlertSnapshot` — rejected as unused persistence and an FR-scope creep (snapshot is the buy-replay record).

**3. Build the summary once per entry in `run_poll_cycle`, thread through `_dispatch_alert`.**
At the existing `buyable, reserved = _split_reserved(candidates)` site, compute `comp_summary = summarize_comps(r.price_eur for r in reserved)` (already guarded by the `if reserved:` branch context). Pass it into each `_dispatch_alert(...)` call for that entry's buyable listings; `_dispatch_alert` forwards it to whichever renderer `_select_phase` chose. All buyable alerts in a cycle for one entry share the same comp summary — correct, since comps are an entry-level signal. _Alternative considered:_ recompute per listing — wasteful and identical output.

**4. Line position and format (operator-chosen).**
Append after the Confidence row: `💬 Comps (<n> reservados): <min> – <max> € · mediana <median> €`. Prices via the existing `_format_price_es`, then `escape_markdown_v2`. The em-dash range and `·` separator match the existing alert typography. The `💬` glyph is a new severity-adjacent token local to the line (not added to `SEVERITY_TOKENS`, which is reserved for headline prefixes).

**5. Singular wording for a single comp.**
With one comp, min=median=max. The format still reads `Comps (1 reservados)` which is grammatically off but the operator explicitly chose the compact-with-range format and did not request a singular special-case; keep one code path. (A singular variant was offered and not selected.) Document this so it is a known, intentional choice rather than an oversight.

## Risks / Trade-offs

- **In-cycle comps are sparse** → A reserved listing and a buyable one rarely surface for the same entry in the same cycle, so the line will often be absent. Mitigation: this is the accepted Layer-2 scope; cross-cycle persistence (which would make comps common) is a deliberately separate future item. The CLI `test-search` path still shows comps on demand.
- **Locked v1 output (FR22) changes** → snapshot tests will fail until updated. Mitigation: update the `test_alert_renderer_snapshots.py` / `test_phase2_renderer_snapshots.py` snapshots as part of the change; the new line only ever appears when comps exist, so all existing no-comp snapshots stay byte-identical and only new comp-present cases are added.
- **Median type drift** → averaging two `Decimal` central values can yield a half-cent (e.g. `(200 + 201)/2 = 200.5`); `_format_price_es` quantizes to 2dp so it renders cleanly. Mitigation: covered by an even-length builder unit test.
- **Reserved set only exists for Wallapop today** → eBay listings don't carry a reserved flag, so eBay alerts won't show comps. Acceptable; no special handling needed.
