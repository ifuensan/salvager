## Context

`Listing.url` is a required field carried end-to-end into the `AlertSnapshot`. The alert renderers in `domain/alert.py` are pure functions; the listing-alert body is built as a list of rows joined by `\n`, with each user-supplied field individually escaped via `escape_markdown_v2` and interpolated into a template that carries intentional markup (`*bold*`, `_italic_`). The output is locked at v1 (FR22) and guarded by syrupy snapshots in `test_alert_renderer_snapshots.py` (Phase 1) and `test_phase2_renderer_snapshots.py` (Phase 2). FR18 requires a deep link in the alert; it was never rendered, and the `view` callback only records a "visto" audit row.

## Goals / Non-Goals

**Goals:**
- A clickable deep link to `listing.url` on every Phase 1 and Phase 2 listing alert.
- Zero change to the callback flow, keyboard, or persistence.
- MarkdownV2-safe link assembly reused across both renderers.

**Non-Goals:**
- A Telegram URL inline button (operator chose a body link; keeps `InlineButton`/keyboard untouched).
- Changing the `view` button semantics.
- Touching the `📍` location row (the empty-location `—` stays).
- Deep links on operational / receipt / failure alerts — listing alerts only.

## Decisions

**1. MarkdownV2 inline link in the body, not a URL button.**
Operator-chosen. A body link (`[text](url)`) renders clickable in both message text and photo captions, requires no `InlineButton` model change (today it only carries `callback_data` with a format validator), and keeps the `view` callback as the audit affordance. A URL button would need `InlineButton.url` support + keyboard-converter changes + a second tappable element — more surface for no operator-visible gain here.

**2. Dedicated `_md_v2_link(text, url)` helper.**
The link-destination escaping rules differ from body-text escaping: inside `(...)` MarkdownV2 only treats `\` and `)` as special, while `.`/`-`/`!` etc. (escaped in body text) must NOT be escaped in a URL or the link breaks. So a single helper assembles `[escape_markdown_v2(text)](escape_link_target(url))` where `escape_link_target` replaces `\`→`\\` then `)`→`\)`. Reused by both renderers so the rule lives in one place. _Alternative considered:_ escape the whole row like the comp line — rejected, it would corrupt the URL's `?`/`=`/`&` and is semantically wrong for a link target.

**3. Position: immediately after the location row.**
Matches the approved mockup and puts the validation affordance high in the message. For container listings the wrapper/extracted rows and the take follow the link. The link row is unconditional (URL is required), so it sits between row 2 (location) and the rest on every alert.

**4. Marketplace label reuses `listing.marketplace.capitalize()`.**
Same value already shown on the location row (`Wallapop` / `Ebay`), so the link reads consistently (`🔗 Ver anuncio en Ebay`) without introducing a second display mapping.

## Risks / Trade-offs

- **Every snapshot changes** → unlike the opt-in comp line, the link is unconditional, so all existing renderer snapshots churn. Mitigation: regenerate via syrupy and eyeball the diff (one new row per fixture, identical shape); add explicit link-presence/URL/escaping assertions so the behaviour is pinned beyond the opaque snapshot.
- **MarkdownV2 link-target escaping is easy to get subtly wrong** (over-escaping breaks the link; under-escaping breaks the markup). Mitigation: the helper escapes exactly `\` and `)`; covered by a unit/renderer test using a URL with a `)` and a query string.
- **Re-audit scope grows** → another `domain/alert.py` change re-arms ROADMAP criterion 3 (Story 5.17). Mitigation: already flagged for v1.0; the addition is a single well-formed link row, scope stays narrow.
- **eBay listings with empty location** still render `📍 — · Ebay` (unchanged by decision); the deep link now lets the operator validate regardless.
