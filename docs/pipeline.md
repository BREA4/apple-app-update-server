# Pipeline maintenance

`release-config.json` defines the source repository, feed endpoint, pinned public
key, platform requirement, and archive budget. `releases.json` is the authoritative
counter and catalog; `public/` is generated from it. The app's manifest supplies
the version but its local development build number is replaced in the CI checkout.

## Source and workflow access

Source access uses the organization's installed GitHub App, BREA4 PREA4ER.
`RELEASE_APP_CLIENT_ID` and `RELEASE_APP_CLIENT_KEY` are existing organization
Actions credentials. Each source-fetch job creates a short-lived installation
token restricted to `BREA4/apple-app` with Contents read-only; the action revokes
it when the job ends. Renew the App key through organization secrets if rotated.
Git receives the token through an ephemeral askpass helper, rather than a
credential in the remote URL or command arguments. Persistent Git credential
helpers are disabled for the fetch, and the askpass helper is deleted before
app code runs. Only private main commits are built; fork pull requests run public
pipeline tests without secrets.

The private repository contains no Actions workflow. Public scheduled runs poll
main, including when private changes are pushed by an agent or merged in GitHub.
Both scheduled and manual builds use public standard runners; `xcode-27` supplies
macOS 27 and Xcode 27 ([runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)).
The schedule is best effort, not an immediate push webhook.
GitHub disables public schedules after 60 days without repository activity.
An idle poll records a heartbeat when the latest main commit is at least 30 days
old, keeping checks active without allocating or building another app version.

To check the scheduler itself, list runs with the schedule event:

```sh
gh run list --repo BREA4/apple-app-update-server --workflow build-beta.yml --event schedule --limit 5
gh api repos/BREA4/apple-app-update-server/actions/workflows/build-beta.yml --jq .state
```

Manual runs do not confirm that the scheduler works. If the workflow is disabled,
enable it with `gh workflow enable build-beta.yml --repo BREA4/apple-app-update-server`.
If it is active but no scheduled runs arrive, check the workflow on the default
branch and GitHub's Actions status. A commit changing the cron expression updates
the schedule and its associated actor. Verify recovery with an actual `schedule`
run; a successful manual dispatch alone does not establish recovery.

If the active beta workflow still has no scheduled runs, compare it with a
temporary workflow on `main` using `*/5 * * * *`, `permissions: {}`, and a single
Ubuntu step that prints the event name. Verify its manual dispatch, then inspect
its `schedule` events. If neither workflow receives a scheduled event, the
failure also occurs without the beta workflow's credentials, runner, conditions,
or concurrency group. An operational GitHub status page does not resolve that
repository-specific result. Record workflow IDs, cron expressions, commit SHAs,
and the observation window for GitHub Support, then remove the temporary probe.

Build and promotion workflows share a FIFO concurrency group with `queue: max`.
This is supported by [GitHub's concurrency syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#concurrency);
older workflow linters may not recognize it or the preview `xcode-27` label.
Only one mutates the catalog at a time. Catalog and signed feed are committed
together through a non-forced Git ref update. Concurrent manual changes cause a
failure rather than replacing a human's commit.

## Signing and publication

`SPARKLE_PRIVATE_KEY` is the existing Sparkle Ed25519 seed, exported from the
development Mac's Keychain and stored as a GitHub Actions secret. The signing
step derives its public key with CryptoKit and compares it with the pinned key
before signing. Temporary key files are removed after signing. Keep a secure
backup of the original Keychain; changing the key breaks existing installations.

SwiftPM resolves dependencies with `--disable-keychain` on the headless runner;
its default Keychain lookup can otherwise wait for an interactive prompt.
Swift resolves Sparkle 2.10.0's official tools. App code is tested and built before
the signing secret is made available to the signing step. Private build logs
remain in the ephemeral runner and are not printed or uploaded. Tests and release
compilation have separate time limits; a timed-out phase fails publication.
Published assets contain the app DMG and a public release receipt, never a
source checkout. New allocations use DMG only. Each image uses APFS and LZFSE
compression and contains `Breach.app` plus an `/Applications` shortcut. `ditto`
preserves bundle permissions and framework symlinks before image creation.
Existing ZIP records and immutable assets stay in their original format,
including during recovery and promotion. Older versions are not rebuilt or
repackaged for this transition.

Release publication creates a draft, uploads both assets, and publishes it as an
immutable beta or stable release. The publisher then downloads the public archive,
verifies its size, SHA-256, Ed25519 signature, and embedded updater metadata, and
only then publishes the signed appcast. DMG verification authenticates the image
before mounting it read-only, verifies the bundled code signature, reads bounded
bundle metadata, and detaches it even when verification fails. ZIP verification
remains available for historical builds. Stable archives are byte-for-byte
copies of their beta archives. Promotion requires no private source access.

Current builds use ad-hoc code signing and are not notarized. Developer ID signing
and notarization require configuring Apple's credentials and extending the build
step; Sparkle signatures authenticate downloaded releases independently.

The feed retains recent eligible items and the newest stable release. Binary
history stays in GitHub Releases. Stable clients select untagged feed items;
beta clients also select `sparkle:channel=beta`. Both choose newer integer builds,
so an older promotion cannot cause a downgrade.

## Feed hosting and migration

GitHub Pages is configured with Actions as its deployment source. The reusable
`pages.yml` workflow serves only `public/`. To republish a feed after a deployment
failure, dispatch that workflow and verify the live bytes against the signed
file in main.

Builds through Vercel build 5 contain the retired Vercel feed URL. Once that
project is deleted, those installations require a one-time manual installation
of a GitHub build. New builds embed the Pages feed URL and retain the original
Sparkle public key.

## Checks

Run `python3 -m unittest discover -s tests -v` before changing pipeline logic.
Release verification must exercise the actual public asset and live signed feed,
not only generated fixtures. Compare stable and beta asset hashes when testing
promotion. Use workflow retries to repair partially published builds; keep
immutable published releases intact.
