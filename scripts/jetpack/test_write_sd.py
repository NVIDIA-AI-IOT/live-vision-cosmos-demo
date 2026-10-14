# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Hardware-free safety checks: python3 -m unittest discover -s scripts/jetpack."""
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

import write_sd as writer


def image_bytes():
    blob = bytearray(4096)
    blob[510:512] = b"\x55\xaa"
    blob[512:520] = b"EFI PART"
    struct.pack_into("<I", blob, 524, 92)
    struct.pack_into("<Q", blob, 544, 7)
    struct.pack_into("<I", blob, 528, zlib.crc32(blob[512:604]))
    return bytes(blob)


class SDWriterTests(unittest.TestCase):
    def setUp(self):
        self.info = {"DeviceIdentifier": "disk4", "WholeDisk": True, "WritableMedia": True,
                     "RemovableMedia": True, "BusProtocol": "Secure Digital", "Internal": True,
                     "DeviceBlockSize": 512, "TotalSize": 62_883_102_720,
                     "DeviceTreePath": "test/sd-reader", "MediaName": "Test SD"}
        self.blob = image_bytes()
        self.manifest = {"rootfs_validation_passed": True, "excluded_application_matches": [],
                         "jetpack_version": "7.2.1", "board": "jetson-orin-nano-devkit-super",
                         "gpt_verification_passed": True, "ext4_verification_passed": True,
                         "size_bytes": len(self.blob), "sha256": hashlib.sha256(self.blob).hexdigest()}

    def test_builtin_sd_marked_internal_is_allowed(self):
        self.assertEqual(writer.identity_from_info(self.info)["BusProtocol"], "Secure Digital")

    def test_unsafe_targets_are_rejected(self):
        for changes in ({"DeviceIdentifier": "disk0"}, {"DeviceIdentifier": "disk4s1"},
                        {"WholeDisk": False}, {"RemovableMedia": False}, {"WritableMedia": False},
                        {"BusProtocol": "PCI-Express"}, {"BusProtocol": "USB", "Internal": True},
                        {"TotalSize": 32_000_000_000}, {"DeviceBlockSize": 4096}, {"DeviceTreePath": ""}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                writer.identity_from_info({**self.info, **changes})

    def test_external_removable_usb_reader_is_allowed(self):
        writer.identity_from_info({**self.info, "BusProtocol": "USB", "Internal": False})

    def test_corrupt_gpt_is_rejected(self):
        writer.validate_gpt(self.blob[:1024], len(self.blob))
        for index in (510, 512, 524, 544):
            blob = bytearray(self.blob)
            blob[index] ^= 1
            with self.subTest(index=index), self.assertRaises(RuntimeError):
                writer.validate_gpt(blob[:1024], len(blob))

    def test_raw_image_validates_without_running_zstd(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "test.img"
            image.write_bytes(self.blob)
            with patch.object(writer.subprocess, "Popen", side_effect=AssertionError("No subprocess expected")):
                writer.validate_image(image, self.manifest, "/unused/zstd")
            image.write_bytes(self.blob[:-1] + b"x")
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                writer.validate_image(image, self.manifest, "/unused/zstd")

    def test_partial_writes_are_completed(self):
        class PartialWriter(io.BytesIO):
            def write(self, data):
                return super().write(data[:13])
        target = PartialWriter()
        writer.copy_image(io.BytesIO(self.blob), target, len(self.blob), self.manifest["sha256"])
        self.assertEqual(target.getvalue(), self.blob)

    def test_truncated_or_extra_input_fails(self):
        for content in (self.blob[:-1], self.blob + b"x"):
            with self.subTest(size=len(content)), self.assertRaises(RuntimeError):
                writer.copy_image(io.BytesIO(content), io.BytesIO(), len(self.blob), self.manifest["sha256"])

    def test_identity_change_after_unmount_prevents_opening_raw_device(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            image = directory / "test.img"
            image.write_bytes(self.blob)
            manifest = directory / "image.json"
            manifest.write_text(json.dumps(self.manifest))
            target = writer.identity_from_info(self.info)
            request = directory / "request.json"
            request.write_text(json.dumps({"disk": "/dev/disk4", "image": str(image),
                                          "manifest": str(manifest), "target": target, "zstd": "/unused"}))
            with patch("sys.argv", ["write_sd.py", "--request", str(request), "--write"]), \
                 patch.object(writer, "target_identity", side_effect=[target, {**target, "serial": "changed"}]), \
                 patch.object(writer.os, "geteuid", return_value=0), \
                 patch.object(writer.subprocess, "run") as run, \
                 patch("builtins.open", side_effect=AssertionError("Must never open a device")), \
                 self.assertRaisesRegex(RuntimeError, "identity changed after unmount"):
                writer.main()
            run.assert_called_once_with(["/usr/sbin/diskutil", "unmountDisk", "/dev/disk4"], check=True)

    def test_dry_run_never_unmounts_or_opens_device(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            manifest = directory / "image.json"
            manifest.write_text(json.dumps(self.manifest))
            request = directory / "request.json"
            target = writer.identity_from_info(self.info)
            request.write_text(json.dumps({"disk": "/dev/disk4", "image": "unused.img",
                                          "manifest": str(manifest), "target": target, "zstd": "/unused"}))
            with patch("sys.argv", ["write_sd.py", "--request", str(request)]), \
                 patch.object(writer, "target_identity", return_value=target), \
                 patch.object(writer, "validate_image", return_value=len(self.blob)), \
                 patch.object(writer.subprocess, "run") as run, \
                 patch("builtins.open", side_effect=AssertionError("Must never open a device")):
                writer.main()
            run.assert_not_called()

    def test_write_readback_and_eject_only_report_success_for_matching_bytes(self):
        real_open = open
        for corrupt_readback in (False, True):
            with self.subTest(corrupt_readback=corrupt_readback), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                image, device = directory / "source.img", directory / "device"
                image.write_bytes(self.blob)
                device.write_bytes(b"\0" * len(self.blob))
                manifest = directory / "image.json"
                manifest.write_text(json.dumps(self.manifest))
                receipt = directory / "receipt.json"
                target = writer.identity_from_info(self.info)
                request = directory / "request.json"
                request.write_text(json.dumps({"disk": "/dev/disk4", "image": str(image),
                                              "manifest": str(manifest), "target": target,
                                              "zstd": "/unused", "receipt": str(receipt)}))

                def open_test_device(path, mode, **kwargs):
                    self.assertEqual(path, "/dev/rdisk4")
                    if mode == "rb" and corrupt_readback:
                        device.write_bytes(self.blob[:-1] + b"x")
                    return real_open(device, mode, **kwargs)

                with patch("sys.argv", ["write_sd.py", "--request", str(request), "--write"]), \
                     patch.object(writer, "target_identity", return_value=target), \
                     patch.object(writer.os, "geteuid", return_value=0), \
                     patch.object(writer.subprocess, "run") as run, \
                     patch.object(writer.fcntl, "ioctl"), \
                     patch("builtins.open", side_effect=open_test_device):
                    if corrupt_readback:
                        with self.assertRaisesRegex(RuntimeError, "readback SHA-256 mismatch"):
                            writer.main()
                        self.assertFalse(receipt.exists())
                        self.assertEqual(run.call_count, 1)  # unmounted, never ejected as a success
                    else:
                        writer.main()
                        result = json.loads(receipt.read_text())
                        self.assertEqual(result["readback_sha256"], self.manifest["sha256"])
                        self.assertFalse(result["boot_tested"])
                        self.assertTrue(result["ejected"])
                        run.assert_called_with(["/usr/sbin/diskutil", "eject", "/dev/disk4"], check=True)


if __name__ == "__main__":
    unittest.main()
