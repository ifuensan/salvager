## 1. Shared comp value object

- [x] 1.1 Add `domain/comps.py` with a frozen `CompSummary` dataclass (`count: int`, `min_eur: Decimal`, `median_eur: Decimal`, `max_eur: Decimal`).
- [x] 1.2 Add `summarize_comps(prices: Iterable[Decimal]) -> CompSummary | None` returning `None` for empty input and computing an averaged median for even-length sets, central value for odd-length.
- [x] 1.3 Add unit tests covering empty (→ None), single comp (min=median=max), odd-length central median, and even-length averaged median (incl. a half-cent case).

## 2. Renderer integration

- [x] 2.1 In `domain/alert.py`, add a private `_comp_line(summary: CompSummary) -> str` that formats `💬 Comps (<n> reservados): <min> – <max> € · mediana <median> €` via `_format_price_es` + `escape_markdown_v2`.
- [x] 2.2 Add an optional keyword-only `comp_summary: CompSummary | None = None` to `render_phase1_listing_alert`; append the comp line after the Confidence row when present.
- [x] 2.3 Add the same optional `comp_summary` to `render_phase2_listing_alert`; append after the (Phase 2 max) Confidence row, leaving the Comprar keyboard untouched.
- [x] 2.4 Update the renderer snapshot tests (`test_alert_renderer_snapshots.py` + `test_phase2_renderer_snapshots.py`): keep all no-comp snapshots byte-identical, add comp-present cases for both phases (incl. single-comp and multi-comp).

## 3. Poll-cycle wiring

- [x] 3.1 In `orchestration/poll_loop.py`, build `comp_summary = summarize_comps(r.price_eur for r in reserved)` at the `_split_reserved` site (entry scope).
- [x] 3.2 Add a `comp_summary` parameter to `_dispatch_alert` and forward it to whichever renderer `_select_phase` selected.
- [x] 3.3 Pass the entry's `comp_summary` into each `_dispatch_alert` call for that entry's buyable listings.
- [x] 3.4 Add/extend a poll_loop unit test: a cycle with one reserved + one buyable listing for the same entry produces an alert whose rendered text contains the comp line; a cycle with no reserved comps produces no comp line.

## 4. CLI footer de-duplication

- [x] 4.1 Re-implement `cli/commands/test_search_cmd.py::_comp_summary_line` on top of `summarize_comps`, preserving its existing English footer wording and output.
- [x] 4.2 Confirm existing `test_search` tests still pass (footer string unchanged); add a test asserting CLI footer and alert line derive identical count/min/median/max from the same comp set.

## 5. Verification

- [x] 5.1 Run `ruff check` + `ruff format --check`, adapter-discipline, and payment-rail gates clean.
- [x] 5.2 Run `mypy src tests` clean on touched files.
- [x] 5.3 Run the full pytest suite (ignoring the two known sandbox-only `/app` PermissionError failures); confirm new tests pass.
- [x] 5.4 `openspec validate annotate-alerts-with-comps --strict` passes.
