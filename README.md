# Breach updates

Public release automation and download hosting for Breach on Apple Silicon Macs.

[Downloads](https://brea4.github.io/apple-app-update-server/) ·
[All releases](https://github.com/BREA4/apple-app-update-server/releases) ·
[Workflows](https://github.com/BREA4/apple-app-update-server/actions)

New private `main` commits become beta builds through workflows in this public
repository. Stable releases promote an existing build without rebuilding it.

The beta workflow polls private `main` every five minutes. To check for unbuilt
commits immediately:

```sh
gh workflow run build-beta.yml --repo BREA4/apple-app-update-server -f force=false
```

To promote build 123:

```sh
gh workflow run promote.yml --repo BREA4/apple-app-update-server -f build=123
```

For publishing, promotion, or repairs, read [AGENTS.md](AGENTS.md).
