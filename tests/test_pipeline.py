import copy
import datetime
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET
import zipfile

spec = importlib.util.spec_from_file_location("pipeline", Path(__file__).resolve().parents[1] / "scripts/pipeline.py")
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


def state():
    return {"schema": 1, "nextBuild": 6, "lastSourceSHA": "a" * 40, "builds": []}


def beta(build=6, extension="zip"):
    return {"version": "0.2.1", "build": build, "sourceSHA": "b" * 40, "runID": str(build),
            "status": "beta", "betaTag": f"0.2.1-beta-build-{build}",
            "filename": f"Breach-0.2.1-build-{build}-arm64.{extension}", "bytes": 1234,
            "sha256": "c" * 64, "signature": "signature", "notes": "Fix <test> & update",
            "publishedAt": "Mon, 05 Oct 2026 12:00:00 +0000",
            "download": f"https://github.com/BREA4/apple-app-update-server/releases/download/0.2.1-beta-build-{build}/Breach-0.2.1-build-{build}-arm64.{extension}"}


class BuildIdentityTests(unittest.TestCase):
    def test_new_builds_use_dmg_and_leave_existing_zips_unchanged(self):
        original = beta()
        catalog = dict(state(), nextBuild=7, builds=[copy.deepcopy(original)])
        new = pipeline.reserve(catalog, "0.3", "c" * 40, "new-run")
        self.assertEqual(new["filename"], "Breach-0.3-build-7-arm64.dmg")
        self.assertEqual(catalog["builds"][0], original)

    def test_retry_of_a_legacy_build_keeps_its_zip_filename(self):
        original = dict(beta(), status="failed")
        catalog = dict(state(), nextBuild=7, builds=[original])
        retried = pipeline.reserve(catalog, "0.2.1", "b" * 40, original["runID"])
        self.assertEqual(retried["filename"], "Breach-0.2.1-build-6-arm64.zip")
        self.assertEqual(catalog["nextBuild"], 7)

    def test_numbers_are_global_and_never_reused_after_failure(self):
        catalog = state()
        first = pipeline.reserve(catalog, "0.2.1", "b" * 40, "100")
        first["status"] = "failed"
        second = pipeline.reserve(catalog, "0.3", "c" * 40, "101")
        self.assertEqual((first["build"], second["build"], catalog["nextBuild"]), (6, 7, 8))

    def test_retry_keeps_its_allocated_build(self):
        catalog = state()
        first = pipeline.reserve(catalog, "0.2.1", "b" * 40, "100")
        first["status"] = "failed"
        retried = pipeline.reserve(catalog, "0.2.1", "b" * 40, "100")
        self.assertEqual(retried["build"], 6)
        self.assertEqual(retried["status"], "building")
        self.assertEqual(catalog["nextBuild"], 7)
        with self.assertRaises(ValueError):
            pipeline.reserve(catalog, "0.3", "c" * 40, "100")

    def test_tags_and_titles_follow_the_channel_contract(self):
        self.assertEqual(pipeline.title(beta(12345)), "0.2.1 Build #12345")
        self.assertEqual(pipeline.tag("0.2", 1, "beta"), "0.2-beta-build-1")
        self.assertEqual(pipeline.tag("0.3", 12345, "release"), "0.3-release-build-12345")
        for version in ("v0.2", "0.2-beta", "0.2 Build #1", "0.2/../x", "00.2"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                pipeline.tag(version, 1, "beta")

    def test_promotion_preserves_archive_identity_and_is_repeatable(self):
        for extension in ("zip", "dmg"):
            with self.subTest(extension=extension):
                original = beta(123, extension)
                promoted = pipeline.promoted(original)
                for key in ("version", "build", "sourceSHA", "filename", "sha256", "signature", "bytes"):
                    self.assertEqual(promoted[key], original[key])
                self.assertEqual(original["status"], "beta")
                self.assertEqual(promoted["status"], "stable")
                self.assertEqual(promoted["releaseTag"], "0.2.1-release-build-123")
                self.assertEqual(pipeline.promoted(promoted), promoted)

    def test_archive_filenames_reject_traversal_and_mismatched_identity(self):
        for filename in ("../Breach.dmg", "Breach-0.2.1-build-7-arm64.dmg", "Breach.pkg"):
            catalog = dict(state(), nextBuild=7, builds=[dict(beta(), filename=filename)])
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                pipeline.validate_state(catalog)

    def test_failed_and_unknown_builds_cannot_be_promoted(self):
        for status in ("building", "failed"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                pipeline.promoted(dict(beta(), status=status))
        with self.assertRaises(ValueError):
            pipeline.find_build(state(), 123)

    def test_catalog_rejects_duplicates_and_a_reused_counter(self):
        catalog = state()
        catalog["builds"] = [beta()]
        with self.assertRaises(ValueError):
            pipeline.validate_state(catalog)
        catalog["nextBuild"] = 7
        pipeline.validate_state(catalog)
        catalog["builds"].append(copy.deepcopy(catalog["builds"][0]))
        with self.assertRaises(ValueError):
            pipeline.validate_state(catalog)


class FeedTests(unittest.TestCase):
    def items(self, records):
        catalog = dict(state(), builds=records)
        return ET.fromstring(pipeline.render_feed(catalog)).findall("channel/item")

    def test_promotion_changes_eligibility_without_changing_build_or_signature(self):
        original = beta(123)
        before = self.items([original])[0]
        after = self.items([pipeline.promoted(original)])[0]
        self.assertEqual(before.findtext(f"{{{pipeline.NS}}}channel"), "beta")
        self.assertIsNone(after.find(f"{{{pipeline.NS}}}channel"))
        self.assertEqual(before.findtext(f"{{{pipeline.NS}}}version"), "123")
        self.assertEqual(after.findtext(f"{{{pipeline.NS}}}version"), "123")
        self.assertEqual(before.find("enclosure").get(f"{{{pipeline.NS}}}edSignature"),
                         after.find("enclosure").get(f"{{{pipeline.NS}}}edSignature"))

    def test_beta_stream_keeps_an_older_stable_download_available(self):
        stable = pipeline.promoted(beta(6))
        records = [stable] + [beta(build) for build in range(7, 150)]
        records.append(dict(beta(150), status="failed"))
        records.append(dict(beta(151), status="building"))
        items = self.items(records)
        builds = [int(item.findtext(f"{{{pipeline.NS}}}version")) for item in items]
        self.assertEqual(builds[0], 149)
        self.assertIn(6, builds)
        self.assertNotIn(150, builds)
        self.assertNotIn(151, builds)
        self.assertEqual(len(records), 146, "Rendering must retain binary history")

    def test_promoting_an_older_build_does_not_offer_a_downgrade(self):
        items = self.items([pipeline.promoted(beta(6)), pipeline.promoted(beta(8)), beta(9)])
        eligible = [int(item.findtext(f"{{{pipeline.NS}}}version")) for item in items
                    if item.find(f"{{{pipeline.NS}}}channel") is None]
        self.assertEqual(eligible, [8, 6])

    def test_release_notes_are_escaped_in_both_xml_and_html(self):
        item = self.items([beta()])[0]
        self.assertEqual(item.findtext("description"), "<p>Fix &lt;test&gt; &amp; update</p>")

    def test_mixed_archive_history_keeps_original_urls_and_signatures(self):
        records = [beta(6), beta(7, "dmg"), pipeline.promoted(beta(8, "dmg"))]
        original = copy.deepcopy(records)
        for item, record in zip(self.items(records), reversed(records)):
            enclosure = item.find("enclosure")
            self.assertEqual(enclosure.get("url"), record["download"])
            self.assertEqual(enclosure.get(f"{{{pipeline.NS}}}edSignature"), record["signature"])
            self.assertEqual(enclosure.get("length"), str(record["bytes"]))
        self.assertEqual(records, original)

    def test_large_notes_trim_the_feed_while_preserving_latest_and_stable(self):
        records = [pipeline.promoted(beta(6))] + [dict(beta(build), notes="x" * 50_000) for build in range(7, 50)]
        catalog = dict(state(), builds=records)
        feed = pipeline.render_feed(catalog)
        self.assertLessEqual(len(feed), 900_000)
        items = ET.fromstring(feed).findall("channel/item")
        builds = [int(item.findtext(f"{{{pipeline.NS}}}version")) for item in items]
        self.assertEqual(builds[0], 49)
        self.assertIn(6, builds)
        self.assertEqual(len(catalog["builds"]), 44)


class DownloadPageTests(unittest.TestCase):
    def parse(self, records):
        class Page(HTMLParser):
            def __init__(self):
                super().__init__()
                self.rows = []
                self.latest_downloads = {}
                self.button = None
                self.selector = None
                self.options = []
                self.row = None

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "tr":
                    self.row = {"hidden": "hidden" in attrs, "text": [], "download": None}
                elif tag == "a" and self.row is not None:
                    self.row["download"] = attrs["href"]
                elif tag == "a" and attrs.get("id") in ("latest-stable", "latest-beta"):
                    self.latest_downloads[attrs["id"]] = attrs["href"]
                elif tag == "button":
                    self.button = attrs
                elif tag == "select":
                    self.selector = attrs
                elif tag == "option":
                    self.options.append(attrs)

            def handle_data(self, text):
                if self.row is not None:
                    self.row["text"].append(text)

            def handle_endtag(self, tag):
                if tag == "tr":
                    if self.row["download"]:
                        self.rows.append(self.row)
                    self.row = None

        page = Page()
        page.feed(pipeline.render_downloads(dict(state(), builds=records)))
        return page

    def test_only_five_newest_successful_builds_are_initially_visible(self):
        records = [beta(build) for build in range(6, 21)]
        records += [dict(beta(21), status="failed"), dict(beta(22), status="building")]
        page = self.parse(records)
        visible = [row for row in page.rows if not row["hidden"]]
        self.assertEqual([row["text"][0] for row in visible],
                         [f"0.2.1 Build #{build}" for build in range(20, 15, -1)])
        self.assertEqual(len(page.rows), 15)
        self.assertEqual([row["download"] for row in page.rows],
                         [record["download"] for record in reversed(records[:15])])
        self.assertTrue(all(row["text"][1] == "Beta" for row in visible))
        self.assertIsNone(page.selector)
        self.assertEqual(page.button["aria-controls"], "releases")
        self.assertEqual(page.button["aria-expanded"], "false")

    def test_five_or_fewer_releases_need_no_button(self):
        for count in (0, 1, 5):
            with self.subTest(count=count):
                page = self.parse([beta(build) for build in range(6, 6 + count)])
                self.assertEqual(len(page.rows), count)
                self.assertTrue(all(not row["hidden"] for row in page.rows))
                self.assertIsNone(page.button)

    def test_download_labels_are_the_same_for_dmg_and_historical_zip_assets(self):
        records = [beta(6), beta(7, "dmg")]
        page = self.parse(records)
        self.assertEqual([row["text"][-1] for row in page.rows], ["Download", "Download"])
        self.assertEqual([row["download"] for row in page.rows],
                         [record["download"] for record in reversed(records)])

    def test_stable_is_default_even_when_beta_builds_are_newer(self):
        records = [beta(build) for build in range(20, 40)]
        records += [pipeline.promoted(beta(build)) for build in range(6, 19)]
        page = self.parse(records)
        visible = [row for row in page.rows if not row["hidden"]]
        self.assertEqual([row["text"][0] for row in visible],
                         [f"0.2.1 Build #{build}" for build in range(18, 13, -1)])
        self.assertTrue(all(row["text"][1] == "Stable" for row in visible))
        self.assertEqual(len(page.rows), 33)
        self.assertEqual(page.selector["aria-controls"], "releases")
        self.assertEqual([option["value"] for option in page.options], ["stable", "beta"])
        self.assertIn("selected", page.options[0])
        self.assertNotIn("selected", page.options[1])

    def test_stable_only_catalog_needs_no_channel_selector(self):
        page = self.parse([pipeline.promoted(beta())])
        self.assertFalse(page.rows[0]["hidden"])
        self.assertEqual(page.rows[0]["text"][1], "Stable")
        self.assertIsNone(page.selector)
        self.assertIsNone(page.button)

    def test_latest_downloads_select_newest_successful_build_per_channel(self):
        stable = pipeline.promoted(beta(20))
        latest_beta = beta(21)
        records = [stable, beta(7), pipeline.promoted(beta(6)), latest_beta,
                   dict(beta(22), status="failed"), dict(beta(23), status="building")]
        page = self.parse(records)
        self.assertEqual(page.latest_downloads, {"latest-stable": stable["download"],
                                               "latest-beta": latest_beta["download"]})

    def test_latest_downloads_only_show_available_channels(self):
        for records, expected in (([], {}), ([beta()], {"latest-beta": beta()["download"]}),
                                  ([pipeline.promoted(beta())],
                                   {"latest-stable": pipeline.promoted(beta())["download"]})):
            with self.subTest(channels=list(expected)):
                self.assertEqual(self.parse(records).latest_downloads, expected)


class SourceOrderTests(unittest.TestCase):
    def test_main_commits_are_built_oldest_first_and_force_uses_current_main(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            git("init", "--quiet")
            git("config", "user.name", "Pipeline test")
            git("config", "user.email", "test@example.invalid")
            git("config", "commit.gpgsign", "false")
            shas = []
            for index in range(3):
                (root / "revision").write_text(str(index))
                git("add", "revision")
                git("commit", "--quiet", "-m", f"Revision {index}")
                shas.append(git("rev-parse", "HEAD"))
            (root / ".git/FETCH_HEAD").write_text(shas[-1] + "\n")
            catalog = dict(state(), lastSourceSHA=shas[0])
            self.assertEqual(pipeline.choose_source(root, catalog, False), shas[1])
            self.assertEqual(pipeline.choose_source(root, catalog, True), shas[-1])
            catalog["lastSourceSHA"] = shas[-1]
            self.assertIsNone(pipeline.choose_source(root, catalog, False))


class ArchiveTests(unittest.TestCase):
    def info(self):
        return {"CFBundleShortVersionString": "0.2.1", "CFBundleVersion": "6",
                "SUFeedURL": pipeline.CONFIG["feedURL"], "SUPublicEDKey": pipeline.CONFIG["publicKey"],
                "SURequireSignedFeed": True, "SUVerifyUpdateBeforeExtraction": True}

    def test_dmg_verification_authenticates_before_mount_and_always_detaches(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "Breach.dmg"
            archive.write_bytes(b"signed image fixture")
            record = dict(beta(extension="dmg"), bytes=archive.stat().st_size, sha256=pipeline.sha256(archive))
            for changes, valid in (({}, True), ({"CFBundleVersion": "7"}, False),
                                   ({"BreachReleaseChannel": "beta"}, False),
                                   ({"SUFeedURL": "https://example.invalid/"}, False)):
                events = []
                signer = Mock()
                signer.verify.side_effect = lambda *args: events.append("signature")
                def run(*args):
                    if args[:2] == ("hdiutil", "attach"):
                        events.append("mount")
                        self.assertIn("-readonly", args)
                        self.assertIn("-nobrowse", args)
                        mount = args[args.index("-mountpoint") + 1]
                        metadata = mount / "Breach.app/Contents/Info.plist"
                        metadata.parent.mkdir(parents=True)
                        metadata.write_bytes(plistlib.dumps(dict(self.info(), **changes)))
                    elif args[0] == "codesign":
                        events.append("codesign")
                    elif args[:2] == ("hdiutil", "detach"):
                        events.append("detach")
                with self.subTest(changes=changes), patch.object(pipeline, "run", side_effect=run):
                    if valid:
                        pipeline.verify_archive(archive, record, signer)
                    else:
                        with self.assertRaises(ValueError):
                            pipeline.verify_archive(archive, record, signer)
                self.assertEqual(events, ["signature", "mount", "codesign", "detach"])

    def test_invalid_dmg_hash_or_signature_never_mounts(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "Breach.dmg"
            archive.write_bytes(b"image")
            record = dict(beta(extension="dmg"), bytes=archive.stat().st_size, sha256=pipeline.sha256(archive))
            signer = Mock()
            with patch.object(pipeline, "run") as command:
                with self.assertRaises(ValueError):
                    pipeline.verify_archive(archive, dict(record, sha256="0" * 64), signer)
                signer.verify.assert_not_called()
                signer.verify.side_effect = ValueError("Invalid signature")
                with self.assertRaises(ValueError):
                    pipeline.verify_archive(archive, record, signer)
                command.assert_not_called()

    def test_dmg_detaches_when_metadata_or_code_signature_verification_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "Breach.dmg"
            archive.write_bytes(b"image")
            record = dict(beta(extension="dmg"), bytes=archive.stat().st_size, sha256=pipeline.sha256(archive))
            external = Path(directory) / "outside.plist"
            external.write_bytes(plistlib.dumps(self.info()))
            for failure in ("missing", "oversized", "symlink", "codesign"):
                detached = []
                def run(*args):
                    if args[:2] == ("hdiutil", "attach"):
                        mount = args[args.index("-mountpoint") + 1]
                        metadata = mount / "Breach.app/Contents/Info.plist"
                        metadata.parent.mkdir(parents=True)
                        if failure == "oversized":
                            metadata.write_bytes(b"x" * 100_001)
                        elif failure == "symlink":
                            metadata.symlink_to(external)
                        elif failure == "codesign":
                            metadata.write_bytes(plistlib.dumps(self.info()))
                    elif args[0] == "codesign":
                        raise RuntimeError("Invalid code signature")
                    elif args[:2] == ("hdiutil", "detach"):
                        detached.append(args[2])
                with self.subTest(failure=failure), patch.object(pipeline, "run", side_effect=run):
                    with self.assertRaises((ValueError, FileNotFoundError, RuntimeError)):
                        pipeline.verify_archive(archive, record, Mock())
                self.assertEqual(len(detached), 1)

    @unittest.skipUnless(sys.platform == "darwin", "Disk images require macOS")
    def test_real_dmg_preserves_signed_bundle_symlinks_and_applications_shortcut(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Breach.app"
            contents = app / "Contents"
            (contents / "MacOS").mkdir(parents=True)
            shutil.copy("/usr/bin/true", contents / "MacOS/Breach")
            info = dict(self.info(), CFBundleIdentifier="app.breach.dmg-test", CFBundleExecutable="Breach",
                        CFBundlePackageType="APPL")
            (contents / "Info.plist").write_bytes(plistlib.dumps(info))
            (contents / "Resources").mkdir()
            (contents / "Resources/target").write_text("symlink fixture")
            (contents / "Resources/link").symlink_to("target")
            pipeline.run("codesign", "--force", "--sign", "-", app)
            archive = Path(directory) / beta(extension="dmg")["filename"]
            pipeline.create_archive(app, archive)
            record = dict(beta(extension="dmg"), bytes=archive.stat().st_size, sha256=pipeline.sha256(archive))
            pipeline.verify_archive(archive, record, Mock())
            with pipeline.mounted_dmg(archive) as mount:
                self.assertEqual((mount / "Applications").readlink(), Path("/Applications"))
                self.assertEqual((mount / "Breach.app/Contents/Resources/link").readlink(), Path("target"))
                self.assertEqual((mount / "Breach.app/Contents/MacOS/Breach").stat().st_mode & 0o111, 0o111)
            self.assertEqual(pipeline.sha256(archive), record["sha256"])

    def test_verified_archive_requires_neutral_channel_and_matching_updater(self):
        class Signer:
            def verify(self, archive, signature):
                self.signature = signature
        info = {"CFBundleShortVersionString": "0.2.1", "CFBundleVersion": "6",
                "SUFeedURL": pipeline.CONFIG["feedURL"], "SUPublicEDKey": pipeline.CONFIG["publicKey"],
                "SURequireSignedFeed": True, "SUVerifyUpdateBeforeExtraction": True}
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "Breach.zip"
            signer = Signer()
            for changes, valid in (({}, True), ({"BreachReleaseChannel": "beta"}, False),
                                   ({"SUFeedURL": "https://example.invalid/appcast.xml"}, False)):
                with zipfile.ZipFile(archive, "w") as file:
                    file.writestr("Breach.app/Contents/Info.plist", plistlib.dumps(dict(info, **changes)))
                record = dict(beta(), bytes=archive.stat().st_size, sha256=pipeline.sha256(archive))
                if valid:
                    pipeline.verify_archive(archive, record, signer)
                    self.assertEqual(signer.signature, record["signature"])
                    with self.assertRaises(ValueError):
                        pipeline.verify_archive(archive, dict(record, sha256="0" * 64), signer)
                else:
                    with self.assertRaises(ValueError):
                        pipeline.verify_archive(archive, record, signer)


class TransactionTests(unittest.TestCase):
    def test_promotion_downloads_original_archive_without_repacking(self):
        for extension in ("zip", "dmg"):
            original = beta(extension=extension)
            catalog = dict(state(), nextBuild=7, builds=[copy.deepcopy(original)])
            signer = Mock()
            def update(change, message, signing_tool):
                self.assertTrue(change(catalog))
                self.assertIs(signing_tool, signer)
            def publish(record, channel, archive, signing_tool):
                self.assertEqual(channel, "release")
                self.assertEqual(archive.name, original["filename"])
                return record
            with self.subTest(extension=extension), \
                 patch.object(pipeline, "read_state", return_value=("head", catalog)), \
                 patch.object(pipeline, "download") as download, \
                 patch.object(pipeline, "verify_archive") as verify, \
                 patch.object(pipeline, "create_archive") as repack, \
                 patch.object(pipeline, "release_asset", side_effect=publish), \
                 patch.object(pipeline, "update_state", side_effect=update), \
                 patch("builtins.print"):
                pipeline.promote(original["build"], signer)
                self.assertEqual(download.call_args.args[0], original["download"])
                verify.assert_called_once()
                repack.assert_not_called()
            promoted = catalog["builds"][0]
            self.assertEqual(promoted["status"], "stable")
            for key in ("filename", "bytes", "sha256", "signature", "sourceSHA", "build"):
                self.assertEqual(promoted[key], original[key])

    def test_retry_recovers_immutable_zip_or_dmg_without_uploading(self):
        for extension in ("zip", "dmg"):
            original = beta(extension=extension)
            def download(url, destination, maximum):
                if destination.name == "release.json":
                    destination.write_text(json.dumps(original))
                else:
                    self.assertEqual(url, original["download"])
            with self.subTest(extension=extension), \
                 patch.object(pipeline, "api", return_value={"draft": False}) as api, \
                 patch.object(pipeline, "download", side_effect=download), \
                 patch.object(pipeline, "verify_archive") as verify, \
                 patch.object(pipeline, "run") as command:
                recovered = pipeline.release_asset(original, "beta", Path("unused"), Mock())
                self.assertEqual(recovered, original)
                verify.assert_called_once()
                api.assert_called_once()
                command.assert_not_called()

    def test_idle_heartbeat_keeps_build_counter_and_source_cursor_intact(self):
        initial = state()
        original = copy.deepcopy(initial)
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=31)).isoformat()
        def update(change, message):
            self.assertTrue(change(initial))
            self.assertEqual(message, "Keep automatic release checks active")
        with patch.object(pipeline, "api", return_value={"committer": {"date": old}}), \
             patch.object(pipeline, "update_state", side_effect=update) as writer:
            pipeline.keep_schedule_active("old-head")
            writer.assert_called_once()
        self.assertIn("lastHeartbeatAt", initial)
        del initial["lastHeartbeatAt"]
        self.assertEqual(initial, original)

    def test_active_repository_does_not_create_heartbeat_commits(self):
        recent = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with patch.object(pipeline, "api", return_value={"committer": {"date": recent}}), \
             patch.object(pipeline, "update_state") as writer:
            pipeline.keep_schedule_active("recent-head")
            writer.assert_not_called()

    def test_feed_and_catalog_commit_atomically_and_ref_is_not_forced(self):
        initial = state()
        calls = []
        def api(path, data=None, method=None):
            calls.append((path, data, method))
            if path.endswith("git/commits/old-head"):
                return {"tree": {"sha": "old-tree"}}
            if path.endswith("git/trees"):
                return {"sha": "new-tree"}
            if path.endswith("git/commits"):
                return {"sha": "new-head"}
            return None
        def change(catalog):
            catalog["lastSourceSHA"] = "b" * 40
            return True
        with patch.object(pipeline, "read_state", return_value=("old-head", initial)), \
             patch.object(pipeline, "api", side_effect=api), \
             patch.object(pipeline, "render_site", return_value={"public/appcast.xml": "signed-feed"}):
            pipeline.update_state(change, "Publish", signer=object())
        tree = next(data for path, data, _ in calls if path.endswith("git/trees"))
        self.assertEqual({file["path"] for file in tree["tree"]}, {"releases.json", "public/appcast.xml"})
        ref = calls[-1]
        self.assertEqual(ref[1], {"sha": "new-head", "force": False})
        self.assertEqual(ref[2], "PATCH")


if __name__ == "__main__":
    unittest.main()
