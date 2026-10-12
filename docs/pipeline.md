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
it when the job ends. After rotating the App key, update both the organization
secret and the private trigger's repository secret described below.
Git receives the token through an ephemeral askpass helper, rather than a
credential in the remote URL or command arguments. Persistent Git credential
helpers are disabled for the fetch, and the askpass helper is deleted before
app code runs. Only private main commits are built; fork pull requests run public
pipeline tests without secrets.

## Push triggers and recovery

The private `Trigger beta build` workflow runs on pushes to `main`, including
merges. It creates a short-lived App token restricted to
`BREA4/apple-app-update-server` with Contents write, then sends an
`apple-app-main` repository dispatch. The App is installed on both repositories.
The private repository also stores `RELEASE_APP_CLIENT_ID` as an Actions variable
and `RELEASE_APP_CLIENT_KEY` as an Actions secret at repository level.
[GitHub Free does not expose organization credentials to private repositories](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets#creating-secrets-for-an-organization).
Keep both copies of the App key current when rotating it.
Contents write is required by [GitHub's repository dispatch endpoint](https://docs.github.com/en/rest/repos/repos#create-a-repository-dispatch-event).
The private job has no checkout and an empty `GITHUB_TOKEN` permission set.

The public `Build beta` workflow accepts that notification and reads private
main itself. Dispatch payloads never choose the source, version, or build number.
One run allocates the oldest unprocessed first-parent main commit. After an
allocated build finishes, even if it fails, the continuation job rechecks main
and dispatches another run only when commits remain. A push containing multiple
commits therefore drains in order. Duplicate notifications stop without a new
allocation once the cursor catches up. Retries retain their original allocation.
No workflow has a schedule, and idle runs make no heartbeat commits.

Continuation uses the public workflow's Contents-write `GITHUB_TOKEN`.
`repository_dispatch` is an [exception to GitHub's workflow recursion restriction](https://docs.github.com/en/actions/concepts/security/github_token#when-github_token-triggers-workflow-runs).
Builds use public standard runners; `xcode-27` supplies macOS 27 and Xcode 27
([runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)).

To check delivery, compare the private push run with the public dispatch run:

```sh
gh run list --repo BREA4/apple-app --workflow trigger-beta.yml --event push --limit 5
gh run list --repo BREA4/apple-app-update-server --workflow build-beta.yml --event repository_dispatch --limit 5
```

If the private trigger fails, retry that run after repairing its App access, or
manually dispatch `trigger-beta.yml` in the private repository. If notification
delivery or continuation is missed, dispatch public `build-beta.yml` with
`force=false` to resume the queue. Verify recovery with an actual private push
and public `repository_dispatch` run through release and Pages publication.
A manual public run alone does not verify cross-repository delivery.
An empty client ID or private key in a private trigger run means its repository
variable or secret is missing; organization visibility alone does not fix it.

For rollout, merge the public receiver before the private trigger. Pushes made
with a workflow's `GITHUB_TOKEN` do not produce new push workflow runs; app-writing
automation must use an App token or explicitly dispatch the private trigger.

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
failures report SHA-256 prefixes for failing test identifiers, fixed assertion
categories and error counts. Compare an identifier's first 16 SHA-256 hex digits
against the private local test log to locate a failure. The summary omits test
names, source paths, source code and assertion values. Tests and release
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
