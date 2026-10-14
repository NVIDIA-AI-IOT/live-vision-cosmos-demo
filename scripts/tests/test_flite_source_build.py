# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Offline integrity, failure, native-policy and selection tests for Flite builds."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("flite_source_build", SCRIPTS / "build-flite.py")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class FliteSourceBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="flite-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / "flite"
        self.manifest = json.loads((SCRIPTS / "flite-sources.json").read_text())
        self.manifest_path = self.root / "manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest))

    def tar(self, members):
        target = self.root / "source.tar.gz"
        with tarfile.open(target, "w:gz") as archive:
            for name, kind, content in members:
                item = tarfile.TarInfo(name)
                item.type = kind
                item.mode = 0o755 if name.endswith("configure") else 0o644
                item.mtime = 1000000000
                if kind == tarfile.REGTYPE:
                    item.size = len(content)
                    archive.addfile(item, io.BytesIO(content))
                else:
                    item.linkname = str(content)
                    archive.addfile(item)
        return target

    def fixture_archive(self):
        root = self.manifest["directory"]
        return self.tar([(f"{root}/{name}", tarfile.REGTYPE, body) for name, body in (
            ("COPYING", b"Verbatim upstream copyright and terms\n"),
            ("ACKNOWLEDGEMENTS", b"Original contributors\n"),
            ("configure", b"#!/bin/sh\nexit 0\n"),
            ("lang/cmu_us_slt/voice.c", b"Original voice notice and data\n"),
        )])

    @contextlib.contextmanager
    def native_fixture(self, fail_build=False):
        archive = self.fixture_archive()

        def fetch(_url, _sha, _destination, **kwargs):
            return archive

        def run(command, cwd, log, **kwargs):
            if command[0] == "make":
                if fail_build:
                    raise BUILD.BuildError("fixture compiler failure")
                (cwd / "bin").mkdir()
                (cwd / "bin/flite").write_bytes(b"fixture compiled speech binary\n")
            return {"argv": list(map(str, command)), "elapsed_s": 0.01}

        with (mock.patch.object(BUILD.platform, "system", return_value="Linux"),
              mock.patch.object(BUILD.platform, "machine", return_value="aarch64"),
              mock.patch.object(BUILD.platform, "platform", return_value="Linux-aarch64-fixture"),
              mock.patch.object(BUILD.shutil, "which", return_value="/fixture/tool"),
              mock.patch.object(BUILD, "download", side_effect=fetch) as downloaded,
              mock.patch.object(BUILD, "run", side_effect=run),
              mock.patch.object(BUILD, "verify", return_value={"voice": "slt"}) as verified,
              mock.patch.object(BUILD.subprocess, "check_output", return_value="fixture gcc")):
            yield downloaded, verified

    def build(self):
        return BUILD.build(self.output, self.manifest_path, self.root / "cache")

    def old_selection(self):
        old = self.root / "previous-build"
        (old / "bin").mkdir(parents=True)
        (old / "bin/flite").write_bytes(b"previous executable")
        self.output.symlink_to(old)
        return old

    def test_success_retains_source_notices_and_atomic_selection(self):
        with self.native_fixture():
            prefix = self.build()
        self.assertEqual(self.output.resolve(), prefix)
        self.assertEqual((prefix / "licenses/COPYING").read_bytes(), b"Verbatim upstream copyright and terms\n")
        self.assertEqual((prefix / "licenses/cmu_us_slt/voice.c").read_bytes(), b"Original voice notice and data\n")
        self.assertTrue((prefix / "archives/flite.tar.gz").is_file())
        receipt = json.loads((prefix / "build.json").read_text())
        self.assertEqual(receipt["payload"], BUILD.payload(prefix))
        self.assertEqual(receipt["recipe"]["manifest"], self.manifest)

    def test_reuse_checks_payload_and_runs_native_verification_again(self):
        with self.native_fixture() as (downloaded, verified):
            prefix = self.build()
            again = self.build()
            self.assertEqual(prefix, again)
            self.assertEqual(downloaded.call_count, 1)
            self.assertEqual(verified.call_count, 2)

    def test_stale_verification_temporary_does_not_block_reuse(self):
        with self.native_fixture():
            prefix = self.build()
            stale = prefix / "last-verification.json.tmp"
            stale.write_text('{"interrupted":')
            self.assertEqual(self.build(), prefix)
        self.assertEqual(json.loads((prefix / "last-verification.json").read_text()), {"voice": "slt"})
        self.assertEqual(stale.read_text(), '{"interrupted":')
        self.assertEqual(list(prefix.glob("last-verification.json.*.tmp")), [])

    def test_failed_receipt_write_preserves_previous_json_and_cleans_temporary(self):
        path = self.root / "receipt.json"
        path.write_text('{"previous": true}\n')
        with mock.patch.object(BUILD.json, "dump", side_effect=OSError("fixture disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                BUILD.write_json(path, {"new": True})
        self.assertEqual(json.loads(path.read_text()), {"previous": True})
        self.assertEqual(list(self.root.glob("receipt.json.*.tmp")), [])
        BUILD.write_json(path, {"new": True})
        self.assertEqual(json.loads(path.read_text()), {"new": True})

    def test_tampered_binary_refuses_reuse(self):
        with self.native_fixture():
            prefix = self.build()
            (prefix / "bin/flite").write_bytes(b"changed binary")
            with self.assertRaisesRegex(BUILD.BuildError, "payload/configuration changed"):
                self.build()

    def test_tampered_source_refuses_reuse(self):
        with self.native_fixture():
            prefix = self.build()
            (prefix / "sources/flite/COPYING").write_bytes(b"altered license")
            with self.assertRaisesRegex(BUILD.BuildError, "payload/configuration changed"):
                self.build()

    def test_tampered_recipe_refuses_reuse(self):
        with self.native_fixture():
            prefix = self.build()
            path = prefix / "build.json"
            receipt = json.loads(path.read_text())
            receipt["recipe"]["make"].append("AUDIOLIBS=-lasound")
            path.write_text(json.dumps(receipt))
            with self.assertRaisesRegex(BUILD.BuildError, "payload/configuration changed"):
                self.build()

    def test_failed_build_preserves_selection_then_allows_retry(self):
        old = self.old_selection()
        with self.native_fixture(fail_build=True):
            with self.assertRaisesRegex(BUILD.BuildError, "compiler failure"):
                self.build()
        self.assertEqual(self.output.resolve(), old)
        self.assertEqual((self.output / "bin/flite").read_bytes(), b"previous executable")
        self.assertFalse(list((self.root / "flite-builds").glob("*/build.json")))
        with self.native_fixture():
            prefix = self.build()
        self.assertEqual(self.output.resolve(), prefix)
        self.assertEqual(len(list((self.root / "flite-builds").glob("*.failed-*"))), 1)

    def test_native_verification_failure_preserves_selection(self):
        old = self.old_selection()
        with self.native_fixture() as (_, verified):
            verified.side_effect = BUILD.BuildError("unexpected runtime library")
            with self.assertRaisesRegex(BUILD.BuildError, "runtime library"):
                self.build()
        self.assertEqual(self.output.resolve(), old)

    def test_activation_failure_restores_old_selection(self):
        old = self.old_selection()
        new = self.root / "new-build"
        new.mkdir()
        with mock.patch.object(BUILD, "digest", side_effect=["wrong", "expected"]):
            with self.assertRaisesRegex(BUILD.BuildError, "Selected speech executable changed"):
                BUILD.activate(new, self.output)
        self.assertEqual(self.output.resolve(), old)
        self.assertFalse(list(self.root.glob(".flite-activate-*")))

    def test_first_activation_failure_removes_only_new_selection(self):
        new = self.root / "new-build"
        new.mkdir()
        with mock.patch.object(BUILD, "digest", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                BUILD.activate(new, self.output)
        self.assertFalse(self.output.is_symlink())
        self.assertTrue(new.is_dir())

    def test_existing_real_directory_is_never_replaced(self):
        self.output.mkdir()
        (self.output / "user-file").write_bytes(b"keep me")
        with self.native_fixture():
            with self.assertRaisesRegex(BUILD.BuildError, "Existing Flite directory preserved"):
                self.build()
        self.assertEqual((self.output / "user-file").read_bytes(), b"keep me")

    def test_source_extraction_rejects_escape_link_and_special_files(self):
        root = self.manifest["directory"]
        for name, kind, content in (
            (f"{root}/../outside", tarfile.REGTYPE, b"bad"),
            ("/tmp/absolute", tarfile.REGTYPE, b"bad"),
            (f"{root}/link", tarfile.SYMTYPE, "../../outside"),
            (f"{root}/hard", tarfile.LNKTYPE, f"{root}/COPYING"),
            (f"{root}/pipe", tarfile.FIFOTYPE, ""),
        ):
            with self.subTest(name=name):
                archive = self.tar([(name, kind, content)])
                with self.assertRaisesRegex(BUILD.BuildError, "Unsafe source member"):
                    BUILD.extract(archive, self.root / "extracted", root)
                self.assertFalse((self.root / "extracted").exists())

    def test_extraction_retains_configure_permissions_and_timestamp(self):
        target = self.root / "extracted"
        BUILD.extract(self.fixture_archive(), target, self.manifest["directory"])
        self.assertTrue(os.access(target / "configure", os.X_OK))
        self.assertEqual((target / "configure").stat().st_mtime, 1000000000)

    def test_corrupt_cached_download_fails_without_network_or_selection_change(self):
        old = self.old_selection()
        cached = self.root / "cached.tar.gz"
        cached.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "different content"):
            BUILD.download(self.manifest["url"], self.manifest["sha256"], cached)
        self.assertEqual(self.output.resolve(), old)

    def test_no_unreviewed_voice_or_source_manifest(self):
        for key, value in (("voice", "kal"), ("revision", "main"), ("sha256", "bad"), ("url", "http://example.com")):
            with self.subTest(key=key), self.assertRaises(BUILD.BuildError):
                BUILD.validate_manifest({**self.manifest, key: value})

    def test_forbidden_audio_or_gpu_library_is_rejected(self):
        known = ["/lib/libc.so.6", "/lib/libm.so.6", "/lib/ld-linux-aarch64.so.1"]
        BUILD.validate_libraries(known)
        for library in ("libespeak-ng.so.1", "libsox.so.3", "libasound.so.2", "libpulse.so.0", "libonnxruntime.so.1", "libcuda.so.1", "libavcodec.so.61"):
            with self.subTest(library=library), self.assertRaises(BUILD.BuildError):
                BUILD.validate_libraries([*known, "/lib/" + library])

    def test_build_environment_drops_gpu_and_compiler_overrides(self):
        with mock.patch.dict(os.environ, {"LD_PRELOAD": "/bad/lib.so", "LD_LIBRARY_PATH": "/cuda", "CC": "unsafe", "MAKEFLAGS": "--eval=bad"}):
            env = BUILD.environment()
        for key in ("LD_PRELOAD", "LD_LIBRARY_PATH", "CC", "MAKEFLAGS"):
            self.assertNotIn(key, env)
        self.assertEqual(env["ac_cv_header_alsa_asoundlib_h"], "no")

    def test_timeout_reaps_child_process(self):
        children = []
        real_popen = subprocess.Popen

        def spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            children.append(process)
            return process

        with (self.root.joinpath("command.log").open("w") as log,
              mock.patch.object(BUILD, "available_kib", return_value=3000000),
              mock.patch.object(BUILD, "child_limits", return_value=None),
              mock.patch.object(BUILD.subprocess, "Popen", side_effect=spawn)):
            with self.assertRaisesRegex(BUILD.BuildError, "time/memory boundary"):
                BUILD.run([sys.executable, "-c", "import time;time.sleep(20)"], self.root, log, timeout=0.02)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())


if __name__ == "__main__":
    unittest.main()
