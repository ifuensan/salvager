# listing-alert-comps Specification

## Purpose
TBD - created by archiving change annotate-alerts-with-comps. Update Purpose after archive.
## Requirements
### Requirement: Buyable Listing Alerts Carry An In-Cycle Comp Summary Line

When the poll cycle dispatches a Phase 1 or Phase 2 listing alert for a buyable listing, and one or more reserved listings were observed for that same wishlist entry in the same poll cycle, the rendered Telegram alert SHALL include a comp summary line placed immediately after the Confidence row. The line SHALL report the count of reserved comps, the minimum and maximum comp price as a range, and the median comp price, using es-ES price formatting and MarkdownV2 escaping. When no reserved comp was observed for the entry in that cycle, the alert SHALL render exactly as before, with NO comp line.

The comp summary SHALL be derived only from reserved listings observed in the same poll cycle for the same entry (in-cycle scope). The comp summary SHALL NOT be persisted to `AlertSnapshot`, the database, or fed to the LLM evaluator.

#### Scenario: Buyable listing alerts alongside reserved comps in one cycle

- **WHEN** a poll cycle for an entry yields a buyable listing that passes the confidence threshold AND at least one reserved listing was partitioned out for the same entry in that cycle
- **THEN** the rendered alert text includes a comp line after the Confidence row reading `💬 Comps (<n> reservados): <min> – <max> € · mediana <median> €`
- **AND** the prices are formatted es-ES (e.g. `1.234,56 €`) and MarkdownV2-escaped
- **AND** the `AlertSnapshot` persisted for the alert contains no comp fields

#### Scenario: Buyable listing alerts with no reserved comps

- **WHEN** a poll cycle yields a buyable listing that passes the confidence threshold AND no reserved listing was observed for the same entry in that cycle
- **THEN** the rendered alert text contains no comp line and is byte-identical to the pre-change anatomy

#### Scenario: Comp line appears on both Phase 1 and Phase 2 alerts

- **WHEN** the dispatched alert is rendered by either `render_phase1_listing_alert` or `render_phase2_listing_alert` and a comp summary is present
- **THEN** both renderers place the comp line in the same position (after the Confidence row)
- **AND** the Phase 2 alert still renders its Confidence row's `· Phase 2 max:` suffix and its Comprar button row unchanged

---

### Requirement: Comp Summary Arithmetic Is Shared And Median-Correct

The count/min/median/max arithmetic over reserved comp prices SHALL be computed by a single shared domain helper consumed by both the alert renderers and the `test-search` CLI footer, so the two surfaces cannot drift. For an even-length set of comp prices, the median SHALL be the average of the two central values, not the upper-middle element.

#### Scenario: Even-length comp set yields an averaged median

- **WHEN** the shared builder summarizes an even-length list of reserved comp prices
- **THEN** the reported median is the arithmetic mean of the two central values

#### Scenario: Odd-length comp set yields the central value

- **WHEN** the shared builder summarizes an odd-length list of reserved comp prices
- **THEN** the reported median is the single central value

#### Scenario: Empty comp set yields no summary

- **WHEN** the shared builder is given an empty list of reserved comp prices
- **THEN** it returns no summary and neither the alert line nor the CLI footer is rendered

#### Scenario: test-search footer and alert line agree

- **WHEN** the same set of reserved comp prices is summarized for the `test-search` footer and for an alert line
- **THEN** the count, min, median, and max values reported by both surfaces are identical

