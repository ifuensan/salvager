# Price-parser regression fixtures

These fixtures back the daily Phase 2 smoke test (Story 5.6) and live
inside the project's test tree so they ship with the repo and are
version-controlled alongside the parsers they protect.

## Layout

```
tests/fixtures/price_parsers/
  active/                                 # fixtures that run every smoke test
    <name>.<ext>                          # recorded marketplace response
    <name>.expected.json                  # independent reference + parser kind
```

Each `expected.json` carries:

```json
{
  "kind": "wallapop_api | wallapop_html | ebay_api",
  "price_eur": "55.00",
  "notes": "<how the price was verified independently>"
}
```

`kind` selects which parser the smoke-test orchestrator dispatches the
response to — the composer wires the real adapter parsers under those
keys.

## Adding a fixture (NFR-M3)

When you hit a real-world parser surprise:

1. Capture the offending response (curl the API, save the HTML).
2. Verify the price independently — look at the page in a browser, or
   ask the seller — and write down what you saw.
3. Drop the capture into `active/` as `<descriptive-name>.<ext>`.
4. Add a sibling `<descriptive-name>.expected.json` with the verified
   price and the parser kind.
5. Run `salvager phase2 smoke-test` locally to confirm the
   fixture parses; commit the pair.

Every CI run now exercises that case.

## Canonical fixture set (v1.0)

The set required at v1.0 per Story 5.6:

- `wallapop_api_typical.json` — a normal Wallapop unofficial-API search
  result.
- `wallapop_html_typical.html` — a normal Wallapop listing page.
- `ebay_api_typical.json` — a normal eBay Browse API summary.
- `wallapop_html_comma_vs_dot.html` — the **Q9 regression** that drove
  this whole mechanism: a Spanish-locale page that a naïve parser
  reads as 0,53 € instead of 53,00 €. The smoke test must catch any
  parser drift here before a real listing gets auto-bought.
