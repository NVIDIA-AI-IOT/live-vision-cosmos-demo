#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Validate, write, fully read back, and eject explicitly selected macOS SD media."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import struct
import subprocess
import time
import zlib

CHUNK = 4 * 1024 * 1024
MIN_CARD_BYTES = 60_000_000_000  # Nominal 64 GB cards have varying reported capacities.


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def identity_from_info(info):
    disk = info.get("DeviceIdentifier", "")
    require(re.fullmatch(r"disk[1-9][0-9]*", disk), "Select a whole SD disk, never disk0 or a partition")
    require(info.get("WholeDisk") and info.get("WritableMedia"), "Target must be a writable whole disk")
    require(info.get("RemovableMedia"), "Target must report removable media")
    protocol = info.get("BusProtocol")
    require(protocol == "Secure Digital" or (protocol == "USB" and info.get("Internal") is False),
            "Only SD-reader media or removable external USB media is supported")
    require(info.get("DeviceBlockSize") == 512, "Expected 512-byte SD sectors")
    require(info.get("TotalSize", 0) >= MIN_CARD_BYTES, "Use a 64 GB or larger SD card")
    require(info.get("DeviceTreePath"), "Cannot identify the reader's device path")
    return {key: info.get(key) for key in (
        "DeviceIdentifier", "TotalSize", "DeviceBlockSize", "BusProtocol",
        "DeviceTreePath", "MediaName", "IORegistryEntryName", "Internal", "RemovableMedia")}


def target_identity(disk):
    require(re.fullmatch(r"/dev/disk[1-9][0-9]*", disk), "Use /dev/diskN, never disk0 or a partition")
    info = plistlib.loads(subprocess.check_output(["/usr/sbin/diskutil", "info", "-plist", disk]))
    target = identity_from_info(info)
    if target["BusProtocol"] == "Secure Digital":
        cards = json.loads(subprocess.check_output([
            "/usr/sbin/system_profiler", "SPCardReaderDataType", "-json"]))

        def objects(value):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from objects(child)
            elif isinstance(value, list):
                for child in value:
                    yield from objects(child)

        serials = [item.get("spcardreader_card_serialnumber") for item in objects(cards)
                   if item.get("bsd_name") == target["DeviceIdentifier"]]
        require(len(serials) == 1 and serials[0], "Cannot read SD card identity")
        target["serial"] = serials[0]
    return target


@contextmanager
def image_stream(image, zstd):
    if image.suffix == ".img":
        with image.open("rb") as stream:
            yield stream
        return
    process = subprocess.Popen([zstd, "-q", "-d", "-c", str(image)], stdout=subprocess.PIPE)
    try:
        yield process.stdout
    except BaseException:
        process.terminate()
        process.wait()
        raise
    else:
        process.stdout.close()
        require(process.wait() == 0, "Image decompression failed")


def hash_stream(stream, size, phase, prefix=b""):
    digest = hashlib.sha256(prefix)
    done, last = len(prefix), time.monotonic()
    while done < size:
        data = stream.read(min(CHUNK, size - done))
        require(bool(data), "Unexpected end of input during " + phase)
        done += len(data)
        digest.update(data)
        if time.monotonic() - last >= 15:
            print(f"{phase}: {done / size:.1%}", flush=True)
            last = time.monotonic()
    return digest.hexdigest()


def validate_gpt(prefix, size):
    require(len(prefix) == 1024 and prefix[510:512] == b"\x55\xaa", "Missing disk boot signature")
    header = bytearray(prefix[512:])
    require(header[:8] == b"EFI PART", "Expected a raw GPT SD image, not an installer ISO")
    header_size, checksum = struct.unpack_from("<II", header, 12)
    require(92 <= header_size <= 512, "Invalid GPT header size")
    struct.pack_into("<I", header, 16, 0)
    require(zlib.crc32(header[:header_size]) == checksum, "GPT header checksum mismatch")
    require((struct.unpack_from("<Q", header, 32)[0] + 1) * 512 == size, "GPT/image size mismatch")


def validate_image(image, manifest, zstd):
    require(manifest.get("rootfs_validation_passed") is True, "Missing rootfs validation")
    require(manifest.get("excluded_application_matches") == [], "Unexpected demo payload in image")
    require(manifest.get("jetpack_version") == "7.2.1", "Expected JetPack 7.2.1")
    require(manifest.get("board") == "jetson-orin-nano-devkit-super", "Unsupported board image")
    require(manifest.get("gpt_verification_passed") and manifest.get("ext4_verification_passed"),
            "Missing GPT/ext4 verification")
    size = manifest.get("size_bytes", 0)
    require(type(size) is int and 0 < size < MIN_CARD_BYTES, "Invalid image size")
    require(re.fullmatch(r"[0-9a-f]{64}", manifest.get("sha256", "")), "Invalid image digest")
    require(image.is_file() and (image.name.endswith(".img.zst") or image.suffix == ".img"), "Expected .img or .img.zst")
    expected_size = size if image.suffix == ".img" else manifest.get("compressed_size_bytes")
    require(image.stat().st_size == expected_size, "Image file size mismatch")
    with image_stream(image, zstd) as stream:
        prefix = stream.read(1024)
        validate_gpt(prefix, size)
        digest = hash_stream(stream, size, "Source checksum", prefix)
        require(digest == manifest["sha256"], "Source SHA-256 mismatch")
        require(not stream.read(1), "Unexpected trailing image data")
    return size


def copy_image(stream, device, size, expected_hash):
    done, last, digest = 0, time.monotonic(), hashlib.sha256()
    while data := stream.read(CHUNK):
        require(done + len(data) <= size, "Image exceeds its validated size")
        digest.update(data)
        remaining = memoryview(data)
        while remaining:
            written = device.write(remaining)
            require(bool(written), "Short SD-card write")
            remaining = remaining[written:]
        done += len(data)
        if time.monotonic() - last >= 15:
            print(f"Writing: {done / size:.1%}", flush=True)
            last = time.monotonic()
    require(done == size and digest.hexdigest() == expected_hash, "Image changed during writing")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    disk, image = request["disk"], Path(request["image"])
    manifest = json.loads(Path(request["manifest"]).read_text())
    target = target_identity(disk)
    differences = {key: {"expected": request["target"].get(key), "observed": target.get(key)}
                   for key in target.keys() | request["target"].keys()
                   if target.get(key) != request["target"].get(key)}
    require(not differences, "Card identity changed; stop and select the card again: " + json.dumps(differences))
    print("Card identity confirmed; validating staged image", flush=True)
    size = validate_image(image, manifest, request["zstd"])
    require(size < target["TotalSize"], "Image does not fit this card")
    if not args.write:
        print("PREFLIGHT_PASSED: no SD card writes performed", flush=True)
        return
    require(os.geteuid() == 0, "macOS administrator authentication required")
    print("Image validated; unmounting the SD card", flush=True)
    subprocess.run(["/usr/sbin/diskutil", "unmountDisk", disk], check=True)
    require(target_identity(disk) == target, "Card identity changed after unmount")
    raw = disk.replace("/dev/disk", "/dev/rdisk", 1)
    started = datetime.now(timezone.utc).isoformat()
    with image_stream(image, request["zstd"]) as stream:
        try:
            device = open(raw, "r+b", buffering=0)
        except PermissionError as error:
            raise RuntimeError(
                "macOS denied raw SD-device access after administrator authentication. "
                "No image bytes were written. Run from Terminal using sudo, allow "
                "removable-volume access when macOS asks, and check System Settings > "
                "Privacy & Security for that terminal's disk permissions. "
                "AppleScript administrator helpers do not inherit the app's disk access. "
                "Retry only after resolving the permission."
            ) from error
        with device:
            copy_image(stream, device, size, manifest["sha256"])
            os.fsync(device.fileno())
            fcntl.ioctl(device.fileno(), 0x20006416)  # macOS DKIOCSYNCHRONIZECACHE
    print("Write complete; verifying every image byte from the SD card", flush=True)
    with open(raw, "rb", buffering=0) as device:
        digest = hash_stream(device, size, "Readback verification")
    require(digest == manifest["sha256"], "SD-card readback SHA-256 mismatch; do not boot this card")
    subprocess.run(["/usr/sbin/diskutil", "eject", disk], check=True)
    receipt = {"status": "flashed_verified_and_ejected", "started_at": started,
               "completed_at": datetime.now(timezone.utc).isoformat(),
               "jetpack_version": "7.2.1", "board": manifest["board"],
               "card_capacity_bytes": target["TotalSize"], "bytes_verified": size,
               "readback_sha256": digest, "cosmos3_edge_installed": False,
               "live_vision_ui_installed": False, "full_jetpack_compute_sdk_installed": False,
               "boot_tested": False, "ejected": True}
    Path(request["receipt"]).write_text(json.dumps(receipt, indent=2) + "\n")
    print("SUCCESS: SD card flashed, fully verified, and safely ejected", flush=True)


if __name__ == "__main__":
    main()
