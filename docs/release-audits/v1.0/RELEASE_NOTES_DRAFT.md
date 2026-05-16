# v0.2.0 release execution — draft

Prepared during the Story 5.17 audit run (2026-05-16) for executing
the v0.2.0 release. v0.2.0 is the **Phase 1 + Phase 2 feature-complete
preview**; v1.0.0 is a future milestone gated on production burn-in
(see [CHANGELOG.md](../../../CHANGELOG.md) `[1.0.0] — future`).

**Trigger gate**: `docs/release-audits/v1.0/SUMMARY.md` flipped to
`RESULT: PASS` ✓ (completed 2026-05-16).

---

## Steps already applied in this commit batch

The version bump + README + ROADMAP + CHANGELOG + SUMMARY edits are
all part of the `release: v0.2.0` commit. Do not re-apply.

- `pyproject.toml` version `0.1.0` → `0.2.0`.
- `CHANGELOG.md` introduced with `[0.2.0]` + `[1.0.0] — future` +
  retroactive `[0.1.0]` entries (Keep-a-Changelog format).
- `README.md` status block reframed to v0.2.0; Hermes paragraph removed
  from "Architecture"; Quick start mentions `:0.2.0` as recommended
  pinned tag.
- `ROADMAP.md` "Where we are" flipped to v0.2.0 shipped; "Near-term"
  replaced with "Path to v1.0" (promotion criteria).
- `docs/release-audits/v1.0/SUMMARY.md` annotated with the v0.2.0 vs
  v1.0.0 framing note.

---

## Step 1 — Tag + push

After CI confirms green on the `release: v0.2.0` commit:

```bash
git tag v0.2.0
git push origin v0.2.0
```

The `v0.2.0` tag push triggers `.github/workflows/release.yml` which
builds + pushes the Docker image to GHCR with the semver tags
`0.2.0` / `0.2` / `latest`.

---

## Step 2 — Verify the release

Once the release workflow completes (~5 minutes):

```bash
# 1. CI gates pass on the tag.
gh run list --workflow Release --limit 1

# 2. Image pulls cleanly without auth.
docker pull ghcr.io/ifuensan/hardware-hunter:0.2.0
docker run --rm ghcr.io/ifuensan/hardware-hunter:0.2.0 version
#    → expected: hardware-hunter 0.2.0 (commit <sha>)

# 3. `:latest` tracks the new release.
docker pull ghcr.io/ifuensan/hardware-hunter:latest
docker inspect ghcr.io/ifuensan/hardware-hunter:latest \
  --format '{{.RepoTags}}'
#    → should include both 0.2.0 and latest

# 4. The GHCR package page shows the v0.2.0 tag publicly.
xdg-open 'https://github.com/ifuensan/hardware-hunter/pkgs/container/hardware-hunter'
```

---

## Step 3 — Burn-in window starts

After GHCR is happy, the **v1.0.0 promotion gate opens**:

1. **Operator's own deploy**: pull the new image
   (`docker-compose pull && docker-compose up -d`) on the production
   homelab host. Pin to `:0.2.0` explicitly in `docker-compose.yml`
   (don't follow `:latest` for the burn-in window — you want
   reproducibility while diagnosing any issues).
2. **Sanity check**: `docker-compose logs hardware-hunter | head -50`
   to confirm `daemon_started` lands and the version line matches.
3. **Smoke test the safety stack**: `hardware-hunter phase2 smoke-test`
   should return `RESULT: pass` against the bundled fixture set.
4. **Phase 1 only first**: leave Phase 2 disabled (the default) until
   Phase 1 polling has been clean for a few days. Phase 1 cadence is
   every 15 min Wallapop / 30 min eBay, so within a day you'll see
   the system operate against real listings.
5. **Phase 2 enablement**: pick ONE wishlist entry, `hardware-hunter
   phase2 enable <entry>`, set a conservative `max_price_eur`, watch
   for the first Phase 2 alert. Do NOT tap Comprar until you've
   reviewed the listing visually — this is your first end-to-end
   exercise of the safety stack.

Burn-in success criteria (from CHANGELOG / ROADMAP):

- [ ] ≥ 2 weeks continuous v0.2.0 operation without unhandled crashes.
- [ ] ≥ 1 Phase 2 purchase end-to-end (or 1 verified abort with safety
      stack engaging).
- [ ] No critical rendering regression surfaced (re-audit if
      `domain/alert.py` or styling changes).
- [ ] OQ3 — per-purchase TinyFish cost ≤ €1.00 confirmed empirically.
- [ ] OQ6 — language-register quick check on first batch of real
      Telegram alerts.

When the criteria hold, follow up with the `v1.0.0` tag procedure
(same shape as this one, with a fresh `release: v1.0.0` commit
flipping README / CHANGELOG / ROADMAP to "stable").

---

## Rollback plan

If Step 2 surfaces a regression before Step 3 is done:

```bash
# Delete the tag (local + remote) — the release workflow's image is
# still in GHCR but no operator follows ":latest" to it because §3
# has not run yet.
git tag -d v0.2.0
git push origin :refs/tags/v0.2.0
# Optionally delete the image tag from GHCR via the package UI.
```

If Step 3 has already run and a regression surfaces in production:

```bash
# Pin back to v0.1.0 — the last published foundation tag.
sed -i 's/:0\.2\.0/:0.1.0/' docker-compose.yml
sed -i 's/:latest/:0.1.0/' docker-compose.yml
docker-compose pull && docker-compose up -d
```

…and open a v0.2.1 hotfix immediately. **Do NOT delete the v0.2.0 tag
once it's been pulled** by anyone outside the maintainer; semver
contract says a published tag is permanent (you can de-list, but
existing pullers keep their digest).

---

## After v0.2.0 is live

- [ ] Update [CHANGELOG.md](../../../CHANGELOG.md): move the
  `## [0.2.0] — _pending publish_` heading to a real date and remove
  the "pending publish" line.
- [ ] Confirm `docs/release-audits/v1.0/SUMMARY.md` is part of the
  release commit (the audit artefact ships with the release).
- [ ] Optionally close any GitHub issues filed against Phase 2 stories
  that v0.2.0 fully addresses.

When v1.0.0 promotion is ready (after Step 3 criteria are met), the
follow-up release commit will:

- Bump `pyproject.toml` `0.2.x` → `1.0.0`.
- Flip the `[1.0.0] — future` placeholder in CHANGELOG to a real
  entry with the burn-in summary.
- Update README + ROADMAP to "stable".
- Tag + push.
