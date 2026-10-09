#!/usr/bin/env python3
"""Allocate builds, publish signed beta archives, and promote their exact bytes."""
import argparse
import base64
import contextlib
import datetime
import hashlib
import html
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "release-config.json").read_text())
NS = "http://www.andymatuschak.org/xml-namespaces/sparkle"
ET.register_namespace("sparkle", NS)
VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))?"


def run(*args, cwd=ROOT, env=None, input=None, check=True):
    # ASVS 1.2.5: validated arguments are never evaluated by a shell.
    result = subprocess.run([str(x) for x in args], cwd=cwd, env=env, input=input,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        # Keep private source diagnostics and credentials out of public logs.
        raise RuntimeError(f"{Path(str(args[0])).name} failed with exit code {result.returncode}")
    return result


def number(value):
    text = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,11}", text):
        raise ValueError("Expected a positive build number")
    return int(text)


def valid_version(value):
    # ASVS 2.2.1, 5.3.2: version/build patterns also bound tags and filenames.
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(VERSION, value):
        raise ValueError("Use major.minor or major.minor.patch, without a prefix")
    return value


def tag(version, build, channel):
    if channel not in ("beta", "release"):
        raise ValueError("Expected beta or release")
    return f"{valid_version(version)}-{channel}-build-{number(build)}"


def title(record):
    return f"{valid_version(record['version'])} Build #{number(record['build'])}"


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")


def api(path, data=None, method=None, missing=False):
    args = ["gh", "api", "--method", method or ("POST" if data is not None else "GET"), path]
    if data is not None:
        args += ["--input", "-"]
    result = run(*args, input=json.dumps(data) if data is not None else None, check=False)
    if result.returncode:
        if missing and "HTTP 404" in result.stderr:
            return None
        raise RuntimeError(f"GitHub API request failed for {path.split('?')[0]}: {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def endpoint(path):
    return f"repos/{CONFIG['repository']}/{path}"


def validate_state(state):
    if state.get("schema") != 1 or not isinstance(state.get("builds"), list):
        raise ValueError("Unsupported release catalog")
    number(state["nextBuild"])
    if not re.fullmatch(r"[0-9a-f]{40}", state["lastSourceSHA"]):
        raise ValueError("Invalid source cursor")
    seen = set()
    for record in state["builds"]:
        build = number(record["build"])
        valid_version(record["version"])
        if build in seen or record["status"] not in ("building", "failed", "beta", "stable"):
            raise ValueError("Duplicate build or invalid build status")
        if not re.fullmatch(r"[0-9a-f]{40}", record["sourceSHA"]):
            raise ValueError("Invalid source revision")
        seen.add(build)
    if seen and state["nextBuild"] <= max(seen):
        raise ValueError("Build counter must exceed every allocated build")
    return state


def read_state():
    head = api(endpoint("git/ref/heads/main"))["object"]["sha"]
    file = api(endpoint(f"contents/releases.json?ref={head}"))
    state = validate_state(json.loads(base64.b64decode(file["content"], validate=False)))
    return head, state


def update_state(change, message, signer=None):
    # ASVS 2.3.3, 15.4.2: catalog and signed feed become visible in one Git commit.
    # Workflows share a FIFO concurrency group. A non-forced ref update also
    # rejects a concurrent human edit instead of replacing it.
    head, state = read_state()
    if not change(state):
        return state
    validate_state(state)
    files = {"releases.json": json.dumps(state, indent=2) + "\n"}
    if signer:
        files.update(render_site(state, signer))
    tree = api(endpoint(f"git/commits/{head}"))["tree"]["sha"]
    new_tree = api(endpoint("git/trees"), {"base_tree": tree, "tree": [
        {"path": path, "mode": "100644", "type": "blob", "content": content}
        for path, content in files.items()]})
    commit = api(endpoint("git/commits"), {"message": message, "tree": new_tree["sha"], "parents": [head]})
    api(endpoint("git/refs/heads/main"), {"sha": commit["sha"], "force": False}, method="PATCH")
    return state


def find_build(state, build):
    matches = [record for record in state["builds"] if record["build"] == number(build)]
    if len(matches) != 1:
        raise ValueError(f"Build #{build} does not exist")
    return matches[0]


def reserve(state, version, source_sha, run_id):
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("Invalid source revision")
    valid_version(version)
    existing = next((record for record in state["builds"] if record["runID"] == run_id), None)
    if existing:
        if (existing["version"], existing["sourceSHA"]) != (version, source_sha):
            raise ValueError("A workflow retry cannot change its source")
        if existing["status"] in ("building", "failed"):
            existing["status"] = "building"
        return existing
    build = state["nextBuild"]
    record = {"version": version, "build": build, "sourceSHA": source_sha, "runID": run_id,
              "status": "building", "betaTag": tag(version, build, "beta"),
              "filename": f"Breach-{version}-build-{build}-arm64.zip"}
    state["nextBuild"] += 1
    state["lastSourceSHA"] = source_sha
    state["builds"].append(record)
    return record


@contextlib.contextmanager
def source_checkout(destination):
    # ASVS 13.3.1, 13.3.2: the short-lived App token is read-only and
    # its askpass helper is removed before app code is executed. Disable Git's
    # persistent credential helpers so the token never enters the runner Keychain.
    token = os.environ.get("SOURCE_READ_TOKEN", "")
    if not token:
        raise ValueError("Provide the BREA4 PREA4ER source-read token as SOURCE_READ_TOKEN")
    with tempfile.TemporaryDirectory(prefix="breach-source-token-") as directory:
        askpass = Path(directory) / "askpass"
        askpass.write_text('#!/bin/sh\ncase "$1" in\n*Username*) printf "%s\\n" x-access-token;;\n*) printf "%s\\n" "$SOURCE_READ_TOKEN";;\nesac\n')
        askpass.chmod(0o700)
        environment = {**os.environ, "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0"}
        origin = f"https://github.com/{CONFIG['sourceRepository']}.git"
        run("git", "init", "--quiet", destination)
        run("git", "remote", "add", "origin", origin, cwd=destination)
        run("git", "-c", "credential.helper=", "fetch", "--quiet", "origin", CONFIG["sourceBranch"], cwd=destination, env=environment)
        yield environment


def choose_source(source, state, force):
    head = run("git", "rev-parse", "FETCH_HEAD", cwd=source).stdout.strip()
    if force:
        return head
    cursor = state["lastSourceSHA"]
    run("git", "merge-base", "--is-ancestor", cursor, head, cwd=source)
    commits = run("git", "rev-list", "--first-parent", "--reverse", f"{cursor}..{head}", cwd=source).stdout.splitlines()
    return commits[0] if commits else None


def output(**values):
    text = "".join(f"{key}={value}\n" for key, value in values.items())
    if path := os.environ.get("GITHUB_OUTPUT"):
        with Path(path).open("a") as file:
            file.write(text)
    else:
        print(text, end="")


def keep_schedule_active(head):
    # An idle public repository loses scheduled workflows after 60 days.
    commit = api(endpoint(f"git/commits/{head}"))
    committed = datetime.datetime.fromisoformat(commit["committer"]["date"].replace("Z", "+00:00"))
    now = datetime.datetime.now(datetime.timezone.utc)
    if now - committed < datetime.timedelta(days=30):
        return
    def heartbeat(state):
        state["lastHeartbeatAt"] = now.isoformat()
        return True
    update_state(heartbeat, "Keep automatic release checks active")


def plan(force):
    head, state = read_state()
    run_id = os.environ["GITHUB_RUN_ID"]
    retry = next((record for record in state["builds"] if record["runID"] == run_id), None)
    with tempfile.TemporaryDirectory(prefix="breach-source-") as directory:
        source = Path(directory) / "source"
        with source_checkout(source):
            sha = retry["sourceSHA"] if retry else choose_source(source, state, force)
            if not sha:
                keep_schedule_active(head)
                output(needed="false")
                return
            manifest = json.loads(run("git", "show", f"{sha}:release.json", cwd=source).stdout)
            version = valid_version(manifest["version"])
    record = None
    def allocate(state):
        nonlocal record
        record = reserve(state, version, sha, run_id)
        return True
    update_state(allocate, f"Allocate {version} beta build for workflow {run_id}")
    output(needed="true", build=record["build"], source_sha=sha)


def checkout(build, destination):
    _, state = read_state()
    record = find_build(state, build)
    with source_checkout(destination) as environment:
        run("git", "merge-base", "--is-ancestor", record["sourceSHA"], "FETCH_HEAD", cwd=destination)
        run("git", "checkout", "--quiet", "--detach", record["sourceSHA"], cwd=destination, env=environment)
    manifest_path = destination / "release.json"
    manifest = json.loads(manifest_path.read_text())
    if valid_version(manifest["version"]) != record["version"]:
        raise ValueError("Source version differs from the reserved build")
    manifest.update(build=record["build"], feedURL=CONFIG["feedURL"], publicKey=CONFIG["publicKey"])
    manifest.pop("channel", None)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


class Signer:
    def __init__(self, tools, key):
        self.tool = tools / "sign_update"
        self.key = key
        if run("swift", ROOT / "scripts/public-key.swift", key).stdout.strip() != CONFIG["publicKey"]:
            raise ValueError("Signing key does not match the app's public key")

    def sign(self, path, archive=False):
        options = ["-p"] if archive else []
        return run(self.tool, "--ed-key-file", self.key, *options, path).stdout.strip()

    def verify(self, path, signature=None):
        run(self.tool, "--ed-key-file", self.key, "--verify", path, *([signature] if signature else []))


@contextlib.contextmanager
def signing(tools):
    # ASVS 13.3.1: neither key material nor the private source enters published assets.
    with tempfile.TemporaryDirectory(prefix="breach-signing-") as directory:
        key = Path(directory) / "key"
        key.write_text(os.environ["SPARKLE_PRIVATE_KEY"].strip())
        key.chmod(0o600)
        yield Signer(tools, key)


def sha256(path):
    # ASVS 11.4.1: SHA-256 checks transport identity; Ed25519 authenticates the bytes.
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def asset_url(record, channel="beta", filename=None):
    release_tag = tag(record["version"], record["build"], channel)
    return f"https://github.com/{CONFIG['repository']}/releases/download/{release_tag}/{filename or record['filename']}"


def download(url, destination, maximum):
    # ASVS 1.2.2, 12.1.1: fixed repository HTTPS URLs and verified HTTPS redirects.
    if not url.startswith(f"https://github.com/{CONFIG['repository']}/releases/download/"):
        raise ValueError("Unexpected release asset URL")
    with urllib.request.urlopen(url, timeout=120) as response, destination.open("wb") as file:
        if urllib.parse.urlparse(response.url).scheme != "https":
            raise ValueError("Insecure asset redirect")
        length = 0
        while chunk := response.read(1024 * 1024):
            length += len(chunk)
            if length > maximum:
                raise ValueError("Archive exceeds its allowed size")
            file.write(chunk)


def verify_archive(archive, record, signer):
    if archive.stat().st_size != record["bytes"] or sha256(archive) != record["sha256"]:
        raise ValueError("Release archive size or SHA-256 differs")
    signer.verify(archive, record["signature"])
    with zipfile.ZipFile(archive) as file:
        entry = file.getinfo("Breach.app/Contents/Info.plist")
        if entry.file_size > 100_000:
            raise ValueError("Oversized bundle metadata")
        info = plistlib.loads(file.read(entry))
    expected = {"CFBundleShortVersionString": record["version"], "CFBundleVersion": str(record["build"]),
                "SUFeedURL": CONFIG["feedURL"], "SUPublicEDKey": CONFIG["publicKey"],
                "SURequireSignedFeed": True, "SUVerifyUpdateBeforeExtraction": True}
    if any(info.get(key) != value for key, value in expected.items()) or "BreachReleaseChannel" in info:
        raise ValueError("Bundle identity or updater configuration differs")


def render_feed(state):
    published = sorted((record for record in state["builds"] if record["status"] in ("beta", "stable")),
                       key=lambda record: record["build"], reverse=True)
    retained = published[:CONFIG["feedItems"]]
    stable = next((record for record in published if record["status"] == "stable"), None)
    if stable and stable not in retained:
        retained.append(stable)
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Breach updates"
    ET.SubElement(channel, "link").text = CONFIG["feedURL"].removesuffix("appcast.xml")
    ET.SubElement(channel, "description").text = "Signed Breach releases for Apple Silicon"
    for record in retained:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = title(record)
        for name, value in (("version", str(record["build"])), ("shortVersionString", record["version"]),
                            ("minimumSystemVersion", CONFIG["minimumSystemVersion"])):
            ET.SubElement(item, f"{{{NS}}}{name}").text = value
        if record["status"] == "beta":
            ET.SubElement(item, f"{{{NS}}}channel").text = "beta"
        ET.SubElement(item, "pubDate").text = record["publishedAt"]
        # ASVS 1.2.1: embedded notes are escaped as HTML before XML serialization.
        ET.SubElement(item, "description").text = "<p>" + html.escape(record["notes"]).replace("\n", "<br>") + "</p>"
        ET.SubElement(item, "enclosure", {"url": record["download"], "length": str(record["bytes"]),
            "type": "application/octet-stream", f"{{{NS}}}edSignature": record["signature"]})
    ET.indent(rss)
    # Keep the feed small independently of the permanent archive catalog.
    while len(ET.tostring(rss, encoding="utf-8", xml_declaration=True)) > 900_000:
        candidates = [item for item in channel.findall("item")
                      if not stable or item.findtext(f"{{{NS}}}version") != str(stable["build"])]
        if len(candidates) <= 1:
            raise ValueError("Newest feed items exceed the appcast size budget")
        channel.remove(candidates[-1])
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def render_site(state, signer):
    with tempfile.TemporaryDirectory(prefix="breach-feed-") as directory:
        feed = Path(directory) / "appcast.xml"
        feed.write_bytes(render_feed(state))
        signer.sign(feed)
        signer.verify(feed)
        signed_feed = feed.read_text()
    return {"public/appcast.xml": signed_feed, "public/index.html": render_downloads(state), "public/.nojekyll": ""}


def render_downloads(state):
    initial_limit = 5
    published = sorted((record for record in state["builds"] if record["status"] in ("beta", "stable")),
                       key=lambda record: record["build"], reverse=True)
    counts = {channel: sum(record["status"] == channel for record in published) for channel in ("stable", "beta")}
    default_channel = "stable" if counts["stable"] else "beta"
    rows = []
    visible_count = 0
    for record in published:
        matches = record["status"] == default_channel
        hidden = " hidden" if not matches or visible_count >= initial_limit else ""
        if matches:
            visible_count += 1
        rows.append(f'<tr data-channel="{record["status"]}"{hidden}><td>{html.escape(title(record))}</td><td>{record["status"].title()}</td>'
                    f'<td><a href="{html.escape(record["download"], quote=True)}">Download ZIP</a></td></tr>')
    page = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
    page += '<link rel="icon" href="favicon.svg" type="image/svg+xml" sizes="any">'
    page += '<title>Breach downloads</title><style>body{font:16px system-ui;max-width:760px;margin:64px auto;padding:0 24px;background:#faf9f6;color:#242424}td,th{padding:12px 24px 12px 0;text-align:left}a{color:#145fa6}table{border-collapse:collapse}tr{border-bottom:1px solid #ddd}button{font:inherit;margin-top:20px;padding:10px 16px;color:#145fa6;background:transparent;border:1px solid currentColor;border-radius:6px;cursor:pointer}'
    page += '.downloads{display:flex;flex-wrap:wrap;gap:12px;margin:24px 0}.download-button{display:inline-flex;flex-direction:column;gap:4px;padding:12px 16px;border:1px solid #145fa6;border-radius:6px;text-decoration:none;transition:background-color 180ms ease,transform 180ms ease}.download-button strong{font-weight:600}.download-button small{font-size:13px}.download-button:hover,.download-button:focus-visible{background:#edf3f8;transform:translateY(-2px)}.download-button.stable{background:#145fa6;color:#fff}.download-button.stable:hover,.download-button.stable:focus-visible{background:#104d87}'
    page += '.channel-picker{margin:24px 0}.channel-picker label{margin-right:12px}.channel-picker select{font:inherit;padding:8px 12px;border:1px solid #ddd;border-radius:6px;background:#faf9f6;color:#242424}'
    page += '@media (prefers-reduced-motion:reduce){.download-button{transition:none}.download-button:hover,.download-button:focus-visible{transform:none}}</style>'
    page += '<h1>Breach downloads</h1><p>For Apple Silicon Macs running macOS 27 or later.</p>'
    downloads = []
    for channel in ("stable", "beta"):
        record = next((record for record in published if record["status"] == channel), None)
        if record:
            downloads.append(f'<a class="download-button {channel}" id="latest-{channel}" '
                             f'href="{html.escape(record["download"], quote=True)}">'
                             f'<strong>Download latest {channel}</strong><small>{html.escape(title(record))}</small></a>')
    if downloads:
        page += '<div class="downloads">' + "".join(downloads) + '</div>'
    has_selector = bool(counts["stable"] and counts["beta"])
    if has_selector:
        page += '<div class="channel-picker" id="channel-picker" hidden><label for="release-channel">Release channel</label><select id="release-channel" aria-controls="releases"><option value="stable" selected>Stable</option><option value="beta">Beta</option></select></div>'
    page += '<table><thead><tr><th>Version</th><th>Channel</th><th>Download</th></tr></thead>'
    page += f'<tbody id="releases" data-channel="{default_channel}" data-limit="{initial_limit}">'
    page += "".join(rows) + '</tbody></table>'
    has_more = max(counts.values()) > initial_limit
    if has_more:
        page += '<button type="button" id="show-all-releases" aria-controls="releases" aria-expanded="false" hidden>Show all releases</button>'
    if has_selector or has_more:
        page += '''<script>
const table = document.getElementById("releases");
const rows = [...table.querySelectorAll("tr")];
const limit = Number(table.dataset.limit);
const selector = document.getElementById("release-channel");
const button = document.getElementById("show-all-releases");
let expanded = false;
function updateReleases() {
    const channel = selector ? selector.value : table.dataset.channel;
    let count = 0;
    rows.forEach(row => {
        const matches = row.dataset.channel === channel;
        row.hidden = !matches || (!expanded && count >= limit);
        if (matches) count++;
    });
    if (button) {
        button.hidden = count <= limit;
        button.setAttribute("aria-expanded", String(expanded));
        button.textContent = expanded ? "Show latest" : "Show all releases";
    }
}
if (selector) {
    selector.addEventListener("change", () => {
        expanded = false;
        updateReleases();
    });
    document.getElementById("channel-picker").hidden = false;
}
if (button) {
    button.addEventListener("click", () => {
        expanded = !expanded;
        updateReleases();
    });
}
updateReleases();
</script>'''
    page += '</html>\n'
    return page


def release_asset(record, channel, archive, signer):
    release_tag = tag(record["version"], record["build"], channel)
    existing = api(endpoint(f"releases/tags/{release_tag}"), missing=True)
    with tempfile.TemporaryDirectory(prefix="breach-release-") as directory:
        work = Path(directory)
        receipt = work / "release.json"
        if existing and not existing["draft"]:
            # An immutable published release is recoverable, never overwritten.
            download(asset_url(record, channel, "release.json"), receipt, 100_000)
            recovered = json.loads(receipt.read_text())
            if any(recovered.get(key) != record[key] for key in ("version", "build", "sourceSHA", "filename")):
                raise ValueError("Published release has a different build identity")
            public_archive = work / record["filename"]
            download(asset_url(record, channel), public_archive, CONFIG["maximumArchiveBytes"])
            verify_archive(public_archive, recovered, signer)
            return recovered
        if not existing:
            head, _ = read_state()
            target = head
            if channel == "release":
                target = api(endpoint(f"git/ref/tags/{record['betaTag']}"))["object"]["sha"]
            existing = api(endpoint("releases"), {"tag_name": release_tag, "target_commitish": target,
                "name": title(record), "body": record["notes"], "draft": True, "prerelease": channel == "beta"})
        published = dict(record, download=asset_url(record, channel))
        receipt.write_text(json.dumps(published, indent=2) + "\n")
        # Clobber is limited to an unpublished draft; published assets are immutable.
        run("gh", "release", "upload", release_tag, archive, receipt, "--clobber", "--repo", CONFIG["repository"])
        _, state = read_state()
        greatest_stable = max([item["build"] for item in state["builds"] if item["status"] == "stable"] + [0])
        api(endpoint(f"releases/{existing['id']}"), {"draft": False, "prerelease": channel == "beta",
            "make_latest": "true" if channel == "release" and record["build"] >= greatest_stable else "false"}, method="PATCH")
        public_archive = work / record["filename"]
        download(published["download"], public_archive, CONFIG["maximumArchiveBytes"])
        verify_archive(public_archive, published, signer)
        return published


def publish_beta(build, source, signer):
    _, state = read_state()
    record = dict(find_build(state, build))
    if record["status"] not in ("building", "failed", "beta"):
        raise ValueError("This build has already been promoted")
    app = source / "build/Breach.app"
    run("codesign", "--verify", "--deep", "--strict", app)
    with tempfile.TemporaryDirectory(prefix="breach-archive-") as directory:
        archive = Path(directory) / record["filename"]
        run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, archive)
        if archive.stat().st_size > CONFIG["maximumArchiveBytes"]:
            raise ValueError("Archive exceeds its allowed size")
        notes = source / f"updates/notes/{record['version']}-beta.md"
        text = notes.read_text().strip() if notes.exists() else f"Automatic beta build of Breach {record['version']}."
        if not text or len(text.encode()) > 50_000:
            raise ValueError("Release notes must be nonempty and under 50 KB")
        record.update(signature=signer.sign(archive, archive=True), sha256=sha256(archive), bytes=archive.stat().st_size,
                      notes=text, publishedAt=timestamp(), status="beta", feed=CONFIG["feedURL"])
        verify_archive(archive, record, signer)
        published = release_asset(record, "beta", archive, signer)
    def finish(state):
        target = find_build(state, build)
        if target["status"] == "stable":
            raise ValueError("A beta retry cannot undo a promotion")
        target.update(published)
        target["status"] = "beta"
        return True
    update_state(finish, f"Publish {title(published)} in beta", signer)
    print(f"Published {title(published)}: https://github.com/{CONFIG['repository']}/releases/tag/{published['betaTag']}")


def promoted(record):
    if record["status"] not in ("beta", "stable"):
        raise ValueError("Only a published beta build can be promoted")
    return dict(record, status="stable", releaseTag=tag(record["version"], record["build"], "release"),
                promotedAt=record.get("promotedAt") or timestamp(), download=asset_url(record, "release"))


def promote(build, signer):
    _, state = read_state()
    original = find_build(state, build)
    record = promoted(original)
    with tempfile.TemporaryDirectory(prefix="breach-promote-") as directory:
        archive = Path(directory) / record["filename"]
        download(asset_url(original), archive, CONFIG["maximumArchiveBytes"])
        verify_archive(archive, original, signer)
        published = release_asset(record, "release", archive, signer)
    def finish(state):
        target = find_build(state, build)
        if target["sha256"] != published["sha256"] or target["signature"] != published["signature"]:
            raise ValueError("Promotion must preserve the original archive")
        target.update(published)
        target["status"] = "stable"
        return True
    update_state(finish, f"Promote {title(published)} to stable", signer)
    print(f"Promoted {title(published)}: https://github.com/{CONFIG['repository']}/releases/tag/{published['releaseTag']}")


def failed(build):
    def mark(state):
        record = find_build(state, build)
        if record["status"] != "building":
            return False
        record["status"] = "failed"
        return True
    update_state(mark, f"Record failed build #{number(build)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--force", action="store_true")
    fetch = commands.add_parser("checkout")
    fetch.add_argument("build", type=number)
    fetch.add_argument("destination", type=Path)
    for name in ("publish-beta", "promote", "failed"):
        command = commands.add_parser(name)
        command.add_argument("build", type=number)
        if name != "failed":
            command.add_argument("--tools", required=True, type=Path)
        if name == "publish-beta":
            command.add_argument("--source", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "plan":
        plan(args.force)
    elif args.command == "checkout":
        checkout(args.build, args.destination)
    elif args.command == "failed":
        failed(args.build)
    else:
        with signing(args.tools) as signer:
            if args.command == "publish-beta":
                publish_beta(args.build, args.source, signer)
            else:
                promote(args.build, signer)


if __name__ == "__main__":
    main()
