# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Fail-closed media receipts, using captured-library fixtures and no media dependencies."""

import contextlib
import copy
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "verify-reachy-media.py"
SPEC = importlib.util.spec_from_file_location("reachy_media_verify", SCRIPT)
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)
PREFIX = Path("/private/reachy-ffmpeg")


def valid_report(prefix=PREFIX):
    libraries = {
        name: {"version": list(version), "license": "LGPL version 2.1 or later",
               "configuration": " ".join(sorted(VERIFY.REQUIRED_FLAGS))}
        for name, version in VERIFY.EXPECTED_VERSIONS.items()
    }
    majors = {name: version[0] for name, version in VERIFY.EXPECTED_VERSIONS.items()}
    objects = {
        name: {"path": str(prefix / "lib" / f"{name}.so.{major}"),
               "sha256": "a" * 64, "size_bytes": 4096}
        for name, major in {**majors, "libmp3lame": 0}.items()
    }
    return {
        "prefix": str(prefix), "pyav_version": "13.1.0", "aiortc_version": "1.10.1",
        "ffmpeg_version": "7.1.5", "libraries": libraries,
        "loaded_libraries": objects, "mapped_paths": [r["path"] for r in objects.values()],
        "codecs": {name: {"ok": True} for name in VERIFY.REQUIRED_SMOKES},
    }


class ReachyMediaVerification(unittest.TestCase):
    def setUp(self):
        self.report = valid_report()

    def reject(self, fragment):
        with self.assertRaisesRegex(VERIFY.VerificationError, fragment):
            VERIFY.validate_report(self.report, PREFIX)

    def test_accepts_complete_lgpl_runtime_evidence_without_filesystem_reads(self):
        with mock.patch.object(Path, "resolve", side_effect=AssertionError("not pure")):
            VERIFY.validate_report(self.report, PREFIX)

    def test_rejects_gpl_lgpl3_and_nonfree_licenses_even_with_safe_flags(self):
        for license_text in ("GPL version 2 or later", "GPL version 3 or later",
                             "LGPL version 3 or later", "nonfree and unredistributable", ""):
            with self.subTest(license=license_text):
                self.report["libraries"]["libavcodec"]["license"] = license_text
                self.reject("expected LGPL")

    def test_requires_explicit_configure_guards_for_every_library(self):
        for name in VERIFY.EXPECTED_VERSIONS:
            for flag in VERIFY.REQUIRED_FLAGS:
                with self.subTest(library=name, flag=flag):
                    self.report = valid_report()
                    self.report["libraries"][name]["configuration"] = " ".join(
                        sorted(VERIFY.REQUIRED_FLAGS - {flag}))
                    self.reject("missing explicit")

    def test_rejects_unsafe_enable_flags_even_when_disable_flags_are_also_present(self):
        for flag in VERIFY.FORBIDDEN_FLAGS | {"--enable-libx264=yes"}:
            with self.subTest(flag=flag):
                self.report = valid_report()
                self.report["libraries"]["libavcodec"]["configuration"] += " " + flag
                self.reject("forbidden configure")

    def test_rejects_wrong_package_or_library_versions(self):
        for field, bad in (("pyav_version", "13.0.0"), ("aiortc_version", "1.14.0"),
                           ("ffmpeg_version", "7.1.4")):
            with self.subTest(field=field):
                self.report = valid_report()
                self.report[field] = bad
                self.reject("must be exactly")
        for name in VERIFY.EXPECTED_VERSIONS:
            for component in range(3):
                with self.subTest(library=name, component=component):
                    self.report = valid_report()
                    self.report["libraries"][name]["version"][component] += 1
                    self.reject("unexpected library version")

    def test_rejects_contradictory_network_and_component_enable_flags(self):
        for flag in ("--enable-network", "--enable-everything"):
            for name in VERIFY.EXPECTED_VERSIONS:
                with self.subTest(library=name, flag=flag):
                    self.report = valid_report()
                    self.report["libraries"][name]["configuration"] += " " + flag
                    self.reject("forbidden configure")

    def test_requires_all_seven_metadata_and_loaded_libraries(self):
        del self.report["libraries"]["libavdevice"]
        self.reject("all seven")
        self.report = valid_report()
        self.report["mapped_paths"].remove(self.report["loaded_libraries"]["libavfilter"]["path"])
        self.reject("all seven")

    def test_rejects_ffmpeg_and_lame_loaded_from_system_or_similar_prefix(self):
        for name, parent in (("libavcodec", "/usr/lib"), ("libmp3lame", "/usr/lib"),
                             ("libavcodec", str(PREFIX) + "-other/lib")):
            with self.subTest(name=name, parent=parent):
                self.report = valid_report()
                old = self.report["loaded_libraries"][name]["path"]
                self.report["mapped_paths"].remove(old)
                self.report["mapped_paths"].append(str(Path(parent) / Path(old).name))
                self.reject("outside private")

    def test_rejects_bundled_wheel_libraries_and_extra_gpl_or_gpu_objects(self):
        for path in ("/env/lib/python3.12/site-packages/av.libs/libavcodec-deadbeef.so.61",
                     "/usr/lib/libx264.so.164", "/usr/lib/libx265.so.199",
                     "/usr/lib/libpostproc.so.58", "/usr/lib/libcuda.so.1",
                     "/usr/lib/libnvinfer.so.10", "/usr/lib/libcublas.so.12"):
            with self.subTest(path=path):
                self.report = valid_report()
                self.report["mapped_paths"].append(path)
                self.reject("bundled|forbidden loaded")

    def test_rejects_multiple_loaded_copies(self):
        self.report["mapped_paths"].append(str(PREFIX / "lib/libavcodec-other.so.61"))
        self.reject("multiple loaded")

    def test_requires_checksums_for_the_actual_mapped_paths(self):
        self.report["loaded_libraries"]["libavcodec"]["path"] = "/different/libavcodec.so.61"
        self.reject("checksum path")
        self.report = valid_report()
        self.report["loaded_libraries"]["libmp3lame"]["sha256"] = "not-a-checksum"
        self.reject("missing SHA256")

    def test_requires_every_codec_result_and_rejects_failure(self):
        for name in VERIFY.REQUIRED_SMOKES:
            with self.subTest(codec=name):
                self.report = valid_report()
                del self.report["codecs"][name]
                self.reject("missing required codec")
                self.report = valid_report()
                self.report["codecs"][name]["ok"] = False
                self.reject("codec smoke failed")

    def test_maps_collector_resolves_symlinks_before_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            prefix = root / "private"
            (prefix / "lib").mkdir(parents=True)
            report = valid_report(prefix)
            lines = []
            for record in report["loaded_libraries"].values():
                path = Path(record["path"])
                path.write_bytes(b"fixture shared object")
                lines.append(f"1000-2000 r-xp 0 00:00 1 {path}")
            lame = Path(report["loaded_libraries"]["libmp3lame"]["path"])
            external = root / lame.name
            lame.rename(external)
            lame.symlink_to(external)
            with mock.patch.object(Path, "read_text", return_value="\n".join(lines)):
                report["mapped_paths"], report["loaded_libraries"] = VERIFY.collect_loaded_objects()
            self.assertEqual(report["loaded_libraries"]["libmp3lame"]["path"], str(external))
            with self.assertRaisesRegex(VERIFY.VerificationError, "outside private"):
                VERIFY.validate_report(report, prefix)

    def test_failed_reverification_removes_old_success_receipt_and_prints_no_json(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "verification.json"
            output.write_text('{"status":"verified"}\n')
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(VERIFY, "verify", side_effect=VERIFY.VerificationError("bad media")), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = VERIFY.main(["--prefix", directory, "--output", str(output)])
            self.assertEqual(result, 1)
            self.assertFalse(output.exists())
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("bad media", stderr.getvalue())

    def test_success_receipt_is_written_only_after_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "verification.json"
            report = copy.deepcopy(self.report)
            report["status"] = "verified"
            with mock.patch.object(VERIFY, "verify", return_value=report):
                self.assertEqual(VERIFY.main(["--prefix", directory, "--output", str(output)]), 0)
            self.assertIn('"status": "verified"', output.read_text())


if __name__ == "__main__":
    unittest.main()
