# Release operations

Use е instead of ё in Russian output.

This public repository owns Breach's build counter, GitHub releases, and signed
GitHub Pages feed. The private app repository is `BREA4/apple-app`; its `main`
branch supplies source, version metadata, and release notes. CI runs here.

## Identity and channels

- A build number identifies one signed ZIP. Numbers increase across all app
  versions and channels; failed allocations leave gaps. The first GitHub build
  follows the previous Vercel build 5.
- Release titles use `0.2.1 Build #123`.
- Beta tags use `0.2.1-beta-build-123`; stable tags use
  `0.2.1-release-build-123`. Use numeric version prefixes exactly; `v` prefixes
  and spaces are invalid in release tags.
- Every successful build starts in beta. Promotion preserves its version, build
  number, source revision, ZIP bytes, SHA-256, and Ed25519 signature. Channel
  membership lives in the signed feed, rather than in the app bundle.
- GitHub releases are immutable. Keep published assets and tags intact. All
  builds remain downloadable even when old entries leave the appcast.

## Publish a beta

1. In the private app repository, update `release.json`'s display version when
   needed and write `updates/notes/VERSION-beta.md`. Merge the app changes into
   `main`. The hosted pipeline assigns the actual build number.
2. The scheduled `Build beta` workflow checks main every five minutes and builds
   unprocessed main commits in order. GitHub may delay schedules. For an immediate
   check, dispatch `build-beta.yml` with its default `force=false`. To request an
   additional build of unchanged main, set `force=true`.
3. Follow the run to completion. A beta is complete when its immutable GitHub
   release has the ZIP and `release.json`, the catalog marks it `beta`, and
   `Publish update feed` has deployed the signed feed referencing its asset.

## Promote a build

1. Read `releases.json` and select an existing successful beta build number.
2. Dispatch `promote.yml` with the `build` input. This creates the stable tag and
   release from the original beta ZIP, then signs and deploys the updated feed.
3. Verify the stable ZIP's SHA-256 matches the beta receipt and the feed's item
   has the same build number without `sparkle:channel`. Promotion is complete
   when the Pages deployment succeeds. Repeating promotion is safe.

Promoting an older build creates its stable release but does not downgrade
clients or replace a newer stable build as GitHub's latest release.

## Repair a failure

- Retry the failed workflow to retain its allocated build number. A retry can
  recover a release published before feed deployment failed; it keeps the
  published archive's original bytes. A fresh forced run allocates a new number.
- If release publication succeeded but Pages deployment failed, dispatch
  `pages.yml` to publish the current signed files.
- For signing, source access, scheduling, or workflow changes, read
  [pipeline maintenance](docs/pipeline.md) before editing. Tests and public
  download/signature verification must pass before calling a repair complete.
