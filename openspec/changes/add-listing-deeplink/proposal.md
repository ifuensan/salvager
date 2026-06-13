## Why

FR18 requires every Phase 1 listing alert to contain "a deep link to the listing", but it was never implemented: the renderer prints photo / title / price / location / take / confidence (+ the new comp line) and no URL, while the 👁 Ver inline button only records a "visto" audit row (the view callback does not open or surface the URL). The operator therefore cannot click through from an alert to validate the listing — confirmed painful in live burn-in. The listing URL is already on hand (`Listing.url`, required, persisted in the `AlertSnapshot` listing JSON), so closing the gap is a small renderer change.

## What Changes

- Phase 1 and Phase 2 listing alerts gain a clickable **deep-link row** in the body, rendered immediately after the `📍 <location> · <marketplace>` row: `🔗 Ver anuncio en <Marketplace>` as a MarkdownV2 inline link to `listing.url` (`<Marketplace>` = `listing.marketplace.capitalize()`, matching the location row).
- The link is rendered on **every** listing alert (the URL is required), so it is unconditional — unlike the opt-in comp line.
- A private `_md_v2_link(text, url)` helper centralises MarkdownV2 link assembly: escape the visible text via `escape_markdown_v2`, and escape only `\` and `)` in the URL destination (the two characters MarkdownV2 treats as special inside a link target).
- The 👁 Ver inline button, its `view` callback, and the `InlineButton` model are **unchanged** — the button stays a "visto" audit affordance; the new link is the click-through path. No Telegram URL button.
- The `📍` location row behaviour is unchanged (still `📍 — · Ebay` when location is empty).
- The locked v1 alert output (FR22) changes for every listing alert; renderer snapshot tests are regenerated.

## Capabilities

### New Capabilities
- `listing-alert-deeplink`: every Phase 1 / Phase 2 listing alert carries a clickable deep link to the originating listing URL, rendered as a MarkdownV2 inline-link row after the location row, on both renderers.

### Modified Capabilities
<!-- None. The base listing-alert anatomy (FR18/FR22) was specified in PRD stories, not in a promoted OpenSpec capability; this adds a new capability rather than editing an existing promoted spec. -->

## Impact

- **Code:**
  - `domain/alert.py`: new `_md_v2_link(text, url)` helper; `render_phase1_listing_alert` and `render_phase2_listing_alert` insert the deep-link row after the location row.
- **Tests:** all existing renderer snapshots in `test_alert_renderer_snapshots.py` + `test_phase2_renderer_snapshots.py` regenerate (the link row is unconditional); new assertions for link presence, correct URL/marketplace, and correct escaping of a URL containing `)` / reserved chars.
- **No change** to: `InlineButton`/keyboard/callback flow, the `view` verb, `AlertSnapshot`, DB schema, LLM prompt, the `📍` location row.
- **v1.0 gate:** another `domain/alert.py` renderer change → adds narrowly to the Story 5.17 rendering re-audit scope already flagged in ROADMAP. Likely shipped as v0.3.1 and redeployed to hermes001.
