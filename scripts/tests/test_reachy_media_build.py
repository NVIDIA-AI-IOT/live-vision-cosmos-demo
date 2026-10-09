# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Offline safety and provenance tests for the isolated Reachy media builder."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("reachy_media_build", ROOT / "scripts/build-reachy-media.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name)
        self.payload = b"verified source archive"
        self.source = {"url": "https://example.test/source.tar.gz", "filename": "source.tar.gz",
                       "sha256": hashlib.sha256(self.payload).hexdigest()}

    def response(self, payload):
        response = io.BytesIO(payload)
        response.geturl = lambda: self.source["url"]
        return response

    def test_good_cache_is_rehashed_without_network(self):
        target = self.cache / self.source["filename"]
        target.write_bytes(self.payload)
        with patch.object(builder.urllib.request, "build_opener") as opener:
            self.assertEqual(builder.download(self.source, self.cache), target)
        opener.assert_not_called()

    def test_corrupt_cache_is_replaced_only_with_verified_download(self):
        target = self.cache / self.source["filename"]
        target.write_bytes(b"corrupt cache")
        with patch.object(builder.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = self.response(self.payload)
            self.assertEqual(builder.download(self.source, self.cache), target)
        self.assertEqual(target.read_bytes(), self.payload)
        self.assertEqual(list(self.cache.glob("*.part")), [])

    def test_checksum_failure_never_publishes_cache_and_retries_are_bounded(self):
        with patch.object(builder.urllib.request, "build_opener") as opener, patch.object(builder.time, "sleep"):
            opener.return_value.open.side_effect = [self.response(b"wrong"), self.response(b"still wrong")]
            with self.assertRaisesRegex(builder.BuildError, "SHA-256 mismatch"):
                builder.download(self.source, self.cache, attempts=2)
            self.assertEqual(opener.return_value.open.call_count, 2)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_plain_http_redirect_is_rejected(self):
        handler = builder.HTTPSRedirectHandler()
        with self.assertRaisesRegex(builder.BuildError, "away from HTTPS"):
            handler.redirect_request(None, None, 302, "Found", {}, "http://example.test/source")


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def archive(self, entries):
        path = self.root / "archive.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, kind in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.mode = 0o6755
                if kind == tarfile.REGTYPE:
                    member.size = 5
                    archive.addfile(member, io.BytesIO(b"hello"))
                else:
                    member.linkname = "../../outside"
                    archive.addfile(member)
        return path

    def test_traversal_rejected_before_any_member_is_extracted(self):
        for bad in ("source/../../outside", "/outside", "other/file"):
            with self.subTest(bad=bad):
                archive = self.archive([("source/valid", tarfile.REGTYPE), (bad, tarfile.REGTYPE)])
                with self.assertRaisesRegex(builder.BuildError, "Unsafe source archive"):
                    builder.extract_source(archive, self.root / "unpacked", "source")
                self.assertFalse((self.root / "unpacked/source/valid").exists())
                self.assertFalse((self.root / "outside").exists())

    def test_links_and_devices_are_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                archive = self.archive([("source/link", kind)])
                with self.assertRaises(builder.BuildError):
                    builder.extract_source(archive, self.root / "unpacked", "source")

    def test_regular_archive_preserves_execution_but_strips_special_bits(self):
        archive = self.archive([("source/configure", tarfile.REGTYPE)])
        source = builder.extract_source(archive, self.root / "unpacked", "source")
        self.assertEqual((source / "configure").read_bytes(), b"hello")
        self.assertEqual((source / "configure").stat().st_mode & 0o7777, 0o755)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.manifest_path = self.root / "sources.json"
        self.manifest = json.loads((ROOT / "scripts/reachy-media-sources.json").read_text())
        self.manifest_path.write_text(json.dumps(self.manifest))
        self.requirements = self.root / "requirements.txt"
        self.requirements.write_text("av==13.1.0\n--no-binary=av\n")
        self.verifier = self.root / "verify.py"
        self.verifier.write_text("# fixture verifier\n")
        self.platform_patch = patch.object(builder, "platform_identity", return_value={"system": "Linux", "machine": "aarch64"})
        self.platform_patch.start()
        self.addCleanup(self.platform_patch.stop)
        self.base = self.root / "install/reachy-media"
        self.base.mkdir(parents=True)
        self.previous = self.base / "previous"
        self.previous.mkdir()
        (self.previous / "preserved").write_text("old runtime")
        (self.base / "current").symlink_to(self.previous.name)

    def call_build(self):
        return builder.build(self.root / "install", self.manifest_path,
                             requirements_path=self.requirements, verifier_path=self.verifier)

    def complete_fixture(self, prefix, manifest, manifest_path, requirements, verifier, cache, jobs, inputs):
        prefix.mkdir()
        (prefix / "logs").mkdir()
        builder.write_json(prefix / "complete.json", {"inputs": inputs})

    def test_failed_build_keeps_previous_current_and_runtime(self):
        with patch.object(builder, "compile_prefix", side_effect=builder.BuildError("compiler failed")):
            with self.assertRaisesRegex(builder.BuildError, "compiler failed"):
                self.call_build()
        self.assertEqual((self.base / "current").resolve(), self.previous)
        self.assertEqual((self.previous / "preserved").read_text(), "old runtime")

    def test_complete_cache_is_reverified_before_promotion(self):
        with patch.object(builder, "compile_prefix", side_effect=self.complete_fixture) as compile_mock:
            prefix = self.call_build()
            self.assertEqual(compile_mock.call_count, 1)
        (self.base / "current").unlink()
        (self.base / "current").symlink_to(self.previous.name)
        with patch.object(builder, "compile_prefix") as compile_mock, patch.object(builder, "verify_prefix") as verify_mock:
            self.assertEqual(self.call_build(), prefix)
            verify_mock.assert_called_once_with(prefix, self.verifier)
            compile_mock.assert_not_called()
        self.assertEqual((self.base / "current").resolve(), prefix)
        self.assertTrue(self.previous.is_dir())

    def test_cached_verification_failure_preserves_previous_selection(self):
        with patch.object(builder, "compile_prefix", side_effect=self.complete_fixture):
            self.call_build()
        (self.base / "current").unlink()
        (self.base / "current").symlink_to(self.previous.name)
        with patch.object(builder, "verify_prefix", side_effect=builder.BuildError("GPL library detected")):
            with self.assertRaisesRegex(builder.BuildError, "GPL library"):
                self.call_build()
        self.assertEqual((self.base / "current").resolve(), self.previous)

    def test_incomplete_active_prefix_is_not_rebuilt(self):
        key, _ = builder.build_identity(self.manifest_path, self.requirements, self.verifier)
        prefix = self.base / key
        prefix.mkdir()
        (self.base / "current").unlink()
        (self.base / "current").symlink_to(key)
        with patch.object(builder, "compile_prefix") as compile_mock:
            with self.assertRaisesRegex(builder.BuildError, "active or symlinked"):
                self.call_build()
            compile_mock.assert_not_called()
        self.assertTrue(prefix.is_dir())

    def test_incomplete_inactive_prefix_can_be_rebuilt(self):
        key, _ = builder.build_identity(self.manifest_path, self.requirements, self.verifier)
        prefix = self.base / key
        prefix.mkdir()
        (prefix / "partial").write_text("interrupted build")
        with patch.object(builder, "compile_prefix", side_effect=self.complete_fixture):
            self.assertEqual(self.call_build(), prefix)
        self.assertFalse((prefix / "partial").exists())
        self.assertTrue(self.previous.exists())

    def test_lock_rejects_concurrent_writer(self):
        with builder.build_lock(self.base):
            with self.assertRaisesRegex(builder.BuildError, "Another Reachy media build"):
                with builder.build_lock(self.base):
                    self.fail("concurrent writer acquired lock")

    def test_requirements_and_verifier_changes_invalidate_key(self):
        initial, _ = builder.build_identity(self.manifest_path, self.requirements, self.verifier)
        self.requirements.write_text("av==13.1.0\n--no-binary=av\n# changed closure\n")
        changed_requirements, _ = builder.build_identity(self.manifest_path, self.requirements, self.verifier)
        self.verifier.write_text("# changed verification\n")
        changed_verifier, _ = builder.build_identity(self.manifest_path, self.requirements, self.verifier)
        self.assertEqual(len({initial, changed_requirements, changed_verifier}), 3)

    def test_build_uses_private_libraries_and_installs_source_wheel_first(self):
        cache = self.root / "cache"
        cache.mkdir()
        for name, source in self.manifest["sources"].items():
            archive = cache / source["filename"]
            files = list(source["licenses"])
            if name == "ffmpeg":
                files += ["ffbuild/config.log", "config.h"]
            with tarfile.open(archive, "w:gz") as target:
                for filename in files:
                    content = b"fixture source text"
                    member = tarfile.TarInfo(source["root"] + "/" + filename)
                    member.size = len(content)
                    target.addfile(member, io.BytesIO(content))
            source["sha256"] = builder.sha256(archive)
        self.manifest_path.write_text(json.dumps(self.manifest))
        key, inputs = builder.build_identity(self.manifest_path, self.requirements, self.verifier)
        prefix = self.base / key
        commands = []

        def fake_run(command, cwd, env, log, timeout=3600):
            commands.append([str(part) for part in command])
            self.assertNotIn("LD_LIBRARY_PATH", env)
            self.assertEqual(env["PKG_CONFIG_LIBDIR"], str(prefix / "lib/pkgconfig"))
            self.assertIn(f"-Wl,-rpath,{prefix / 'lib'}", env["LDFLAGS"])
            if log.name == "ffmpeg-install.log":
                directory = prefix / "lib/pkgconfig"
                directory.mkdir(parents=True)
                for library in builder.LIBRARIES:
                    (directory / f"lib{library}.pc").write_text("fixture")
            if log.name == "pyav-build.log":
                (prefix / "wheels/av-13.1.0-cp312-linux_aarch64.whl").write_bytes(b"source wheel")
            if log.name == "verify.log":
                builder.write_json(prefix / "verification.json", {"status": "passed"})

        with patch.object(builder, "run", side_effect=fake_run):
            builder.compile_prefix(prefix, self.manifest, self.manifest_path, self.requirements,
                                   self.verifier, cache, 2, inputs)
        wheel_build = next(i for i, command in enumerate(commands) if "wheel" in command)
        wheel_install = next(i for i, command in enumerate(commands) if any(part.endswith(".whl") for part in command))
        requirements_install = next(i for i, command in enumerate(commands) if "-r" in command)
        self.assertLess(wheel_build, wheel_install)
        self.assertLess(wheel_install, requirements_install)
        self.assertIn("--no-build-isolation", commands[wheel_build])
        self.assertIn("--no-deps", commands[wheel_build])
        self.assertIn("--no-binary=av", commands[requirements_install])
        self.assertFalse(any("--system-site-packages" in command for command in commands))
        self.assertEqual(len(list((prefix / "sources").iterdir())), 3)
        self.assertTrue((prefix / "licenses/lame/COPYING").is_file())
        self.assertTrue((prefix / "build-verification.json").is_file())
        self.assertTrue((prefix / "complete.json").is_file())
        self.assertFalse((prefix / "work").exists())
        self.assertEqual(json.loads((prefix / "build-manifest.json").read_text())["status"], "verified")


if __name__ == "__main__":
    unittest.main()
