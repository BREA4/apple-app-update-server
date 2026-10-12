# Breach updates

Public release automation and download hosting for Breach on Apple Silicon Macs.

[Downloads](https://brea4.github.io/apple-app-update-server/) ·
[All releases](https://github.com/BREA4/apple-app-update-server/releases) ·
[Workflows](https://github.com/BREA4/apple-app-update-server/actions)

New private `main` commits become beta builds through workflows in this public
repository. Stable releases promote an existing build without rebuilding it.
New builds are distributed only as DMGs for manual installation and Sparkle
updates. Previously published ZIP releases remain available unchanged.

Each push to private `main` dispatches the public beta workflow. It builds
unprocessed main commits in order and triggers another run while commits remain.
There is no scheduled polling. To recover a missed trigger:

```sh
gh workflow run build-beta.yml --repo BREA4/apple-app-update-server -f force=false
```

To promote build 123:

```sh
gh workflow run promote.yml --repo BREA4/apple-app-update-server -f build=123
```

For publishing, promotion, or repairs, read [AGENTS.md](AGENTS.md).
