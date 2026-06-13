## ADDED Requirements

### Requirement: Listing Alerts Carry A Clickable Deep Link

Every Phase 1 and Phase 2 listing alert SHALL include a clickable deep link to the originating listing, rendered as a MarkdownV2 inline-link row in the alert body immediately after the `📍 <location> · <marketplace>` row. The link text SHALL read `🔗 Ver anuncio en <Marketplace>` (where `<Marketplace>` is the listing's marketplace capitalized, matching the location row) and SHALL target `listing.url`. The link SHALL be present on every listing alert (the listing URL is a required field), independent of comps, container status, or phase.

The 👁 Ver inline button and its `view` callback SHALL remain unchanged: the button stays a "visto" acknowledgement that records an audit row and does not surface the URL — the body link is the click-through path.

#### Scenario: Phase 1 alert renders the deep link after the location row

- **WHEN** `render_phase1_listing_alert` renders a listing alert
- **THEN** the text contains a `🔗 Ver anuncio en <Marketplace>` row positioned immediately after the `📍` location row and before any container wrapper/extracted rows and the take
- **AND** the row is a MarkdownV2 inline link whose target is `listing.url`

#### Scenario: Phase 2 alert renders the same deep link

- **WHEN** `render_phase2_listing_alert` renders a listing alert
- **THEN** the deep-link row is present in the same position as Phase 1
- **AND** the `🟢` severity prefix, the `· Phase 2 max:` confidence suffix, and the Comprar keyboard are unchanged

#### Scenario: Ver button still only acknowledges

- **WHEN** the operator taps the 👁 Ver button
- **THEN** the `view` callback records its audit row and edits the keyboard to "✓ visto" as before
- **AND** the URL is not surfaced via the callback (the body link is the click-through)

---

### Requirement: Deep-Link URL Is MarkdownV2-Safe

The deep-link row SHALL be assembled so that neither the visible text nor the URL can break the MarkdownV2 markup or open an injection vector. The visible link text SHALL be escaped via the standard MarkdownV2 escaper. Within the link target, the characters `\` and `)` — the two MarkdownV2 treats as special inside a link destination — SHALL be backslash-escaped; other URL characters (`?`, `=`, `&`, `#`, `|`, etc.) SHALL be left intact so the link remains valid.

#### Scenario: URL containing a reserved character is escaped

- **WHEN** a listing URL contains a `)` or a `\`
- **THEN** those characters are backslash-escaped in the rendered link target
- **AND** the resulting row is a well-formed MarkdownV2 inline link

#### Scenario: Ordinary query-string URL is left functional

- **WHEN** a listing URL contains query characters such as `?`, `=`, or `&`
- **THEN** those characters are preserved unescaped in the link target so the link resolves to the real listing
