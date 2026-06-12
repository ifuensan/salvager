## Why

PR #7 routes reserved Wallapop listings out of the buy path but keeps their prices as a "comp" signal — what someone was recently willing to pay for the same gear. Today that signal only reaches the operator in structured logs and the `test-search` CLI footer. When a real buyable alert lands in Telegram, the operator has no in-context reference price and must eyeball whether the asking price is good. PR #7 explicitly listed "annotate Telegram alerts with comp summary line" as its next-PR follow-up; this change closes that Layer 2.

## What Changes

- Phase 1 and Phase 2 listing alerts gain an optional **comp summary line** rendered after the Confidence row, e.g. `💬 Comps (3 reservados): 180,00 – 240,00 € · mediana 200,00 €` (es-ES prices, MarkdownV2-escaped at render). The line appears only when at least one reserved comp was observed for that entry in the same poll cycle.
- The count/min/median/max comp arithmetic — currently living only in `cli/commands/test_search_cmd.py::_comp_summary_line`, including the even-length-median fix Devin caught on PR #7 — is extracted into a single shared domain value object so the CLI footer and the alert line cannot drift.
- `poll_loop` builds the comp summary from the `reserved` set it already partitions per entry and threads it through `_dispatch_alert` into the renderers.
- **Scope boundary:** in-cycle comps only. Comps reflect reserved listings observed in the *same* poll cycle for the *same* entry. Cross-cycle comp persistence (a history table for trend signal) remains a separate, later item and is explicitly out of scope here.
- No `AlertSnapshot`, DB schema, or LLM-prompt change. The comp summary is a render-time argument (same pattern as `phase2_max_price_eur`) and remains deterministic operator signal, never evaluator input — the FR17 prompt boundary is untouched.
- The locked v1 alert output (FR22) changes intentionally; renderer snapshot tests are updated to the new anatomy.

## Capabilities

### New Capabilities
- `listing-alert-comps`: when a buyable listing alert fires, the rendered Telegram message carries a comp summary line derived from reserved listings observed for the same entry in the same poll cycle, shown only when comps exist, formatted consistently with the `test-search` comp footer.

### Modified Capabilities
<!-- None. No promoted spec under openspec/specs/ owns the alert renderer yet; the
     phase1/phase2 listing-alert behaviour was specified in PRD stories (FR22/UX-DR4/UX-DR7),
     not in a promoted OpenSpec capability. This change adds a new capability rather than
     editing requirements of an existing promoted spec. -->

## Impact

- **Code:**
  - `domain/comps.py` (new): shared `CompSummary` value object + builder over a list of reserved prices (count, min, median, max), with the even-length-median handling.
  - `domain/alert.py`: `render_phase1_listing_alert` and `render_phase2_listing_alert` take an optional `comp_summary` and append the formatted line after the Confidence row.
  - `orchestration/poll_loop.py`: build the `CompSummary` from the existing `reserved` split and pass it through `_dispatch_alert`.
  - `cli/commands/test_search_cmd.py`: `_comp_summary_line` re-implemented on top of the shared builder (behaviour preserved).
- **Tests:** renderer snapshot tests (`test_alert_renderer.py`) updated for the new line; new unit tests for the `CompSummary` builder (incl. even-length median, single comp, empty); poll_loop test asserting the comp line reaches the rendered alert when reserved comps coexist with a buyable listing in one cycle.
- **No change** to: DB schema/migrations, `AlertSnapshot`, callback flow, LLM prompt / `FORBIDDEN_PROMPT_TERMS`, eBay/Wallapop adapters.
