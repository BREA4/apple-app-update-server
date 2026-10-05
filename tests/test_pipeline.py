import copy
import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

spec = importlib.util.spec_from_file_location("pipeline", Path(__file__).resolve().parents[1] / "scripts/pipeline.py")
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


def state():
    return {"schema": 1, "nextBuild": 6, "lastSourceSHA": "a" * 40, "builds": []}


def beta(build=6):
    return {"version": "0.2.1", "build": build, "sourceSHA": "b" * 40, "runID": str(build),
            "status": "beta", "betaTag": f"0.2.1-beta-build-{build}",
            "filename": f"Breach-0.2.1-build-{build}-arm64.zip", "bytes": 1234,
            "sha256": "c" * 64, "signature": "signature", "notes": "Fix <test> & update",
            "publishedAt": "Mon, 05 Oct 2026 12:00:00 +0000",
            "download": f"https://github.com/BREA4/apple-app-update-server/releases/download/0.2.1-beta-build-{build}/Breach-0.2.1-build-{build}-arm64.zip"}


class BuildIdentityTests(unittest.TestCase):
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
        original = beta(123)
        promoted = pipeline.promoted(original)
        for key in ("version", "build", "sourceSHA", "filename", "sha256", "signature", "bytes"):
            self.assertEqual(promoted[key], original[key])
        self.assertEqual(original["status"], "beta")
        self.assertEqual(promoted["status"], "stable")
        self.assertEqual(promoted["releaseTag"], "0.2.1-release-build-123")
        self.assertEqual(pipeline.promoted(promoted), promoted)

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
