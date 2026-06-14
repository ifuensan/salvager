## 1. Renderer helper + rows

- [x] 1.1 In `domain/alert.py`, add `_md_v2_link(text: str, url: str) -> str`: returns `[<escaped text>](<escaped url>)`, where text is escaped via `escape_markdown_v2` and the URL target escapes only `\` (→ `\\`) then `)` (→ `\)`).
- [x] 1.2 Add a `_deeplink_row(listing) -> str` (or inline) producing `🔗 ` + `_md_v2_link(f"Ver anuncio en {listing.marketplace.capitalize()}", listing.url)`.
- [x] 1.3 In `render_phase1_listing_alert`, insert the deep-link row immediately after the `📍` location row (before container rows and the take).
- [x] 1.4 In `render_phase2_listing_alert`, insert the same row in the same position; leave the `🟢` prefix, `Phase 2 max:` suffix, and Comprar keyboard untouched.

## 2. Tests

- [x] 2.1 Regenerate the syrupy snapshots in `test_alert_renderer_snapshots.py` and `test_phase2_renderer_snapshots.py`; review the diff to confirm each fixture gained exactly one `🔗 Ver anuncio en …` row in the expected position.
- [x] 2.2 Add a unit/renderer test asserting the deep-link row is present, links to `listing.url`, and names the marketplace correctly (Phase 1 + Phase 2).
- [x] 2.3 Add a test with a listing URL containing a `)` (and a query string `?a=1&b=2`): the `)` is `\)`-escaped, the query chars are preserved, and the row is a well-formed `[..](..)` link.
- [x] 2.4 Add a test confirming the `view` callback / keyboard is unchanged (no URL surfaced; "✓ visto" still rendered on tap) — extend existing callback test if cheaper.

## 3. Verification

- [x] 3.1 `ruff check` + `ruff format --check`, adapter-discipline, payment-rail clean.
- [x] 3.2 `mypy src tests` clean.
- [x] 3.3 Full pytest green (ignoring the 2 known sandbox `/app` failures).
- [x] 3.4 `openspec validate add-listing-deeplink --strict` passes.
