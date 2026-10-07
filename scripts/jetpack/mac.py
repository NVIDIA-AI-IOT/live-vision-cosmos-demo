#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""One-command clean JetPack 7.2.1 SD preparation for an Apple Silicon Mac."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from write_sd import require, target_identity, validate_image

HERE = Path(__file__).resolve().parent
RELEASE = json.loads((HERE / "release.json").read_text())
IMAGE_NAME = "jetpack-7.2.1-orin-nano-super-clean.img.zst"
DEFAULT_CACHE = Path.home() / "Library/Caches/live-vision-jetpack/7.2.1"


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(item, directory):
    path = directory / item["name"]
    if not path.exists():
        print("Downloading " + item["name"], flush=True)
        partial = path.with_name(path.name + ".part")
        run("curl", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https",
            "--retry", "3", "--continue-at", "-", "--output", partial, item["url"])
        require(digest(partial) == item["sha256"], "Download checksum mismatch: " + str(partial))
        partial.rename(path)
    require(digest(path) == item["sha256"], "Cached download checksum mismatch: " + str(path))
    return path


def ssh_args(vm):
    return ["ssh", "-p", str(vm["port"]), "-i", str(vm["path"] / "client"),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=yes", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "UserKnownHostsFile=" + str(vm["path"] / "known_hosts"), "flash@127.0.0.1"]


def ssh(vm, command, **kwargs):
    return run(*ssh_args(vm), command, **kwargs)


@contextmanager
def linux_vm(cache, downloads):
    directory = Path(tempfile.mkdtemp(prefix="vm-", dir=cache))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    vm = {"path": directory, "port": port}
    seed = directory / "seed"
    seed.mkdir()
    for name in ("client", "host"):
        run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "jetpack-local-vm", "-f", directory / name)
    config = {"users": [{"name": "flash", "groups": ["sudo"], "sudo": "ALL=(ALL) NOPASSWD:ALL",
                         "shell": "/bin/bash", "lock_passwd": True,
                         "ssh_authorized_keys": [(directory / "client.pub").read_text().strip()]}],
              "ssh_pwauth": False, "disable_root": True, "package_update": False,
              "package_upgrade": False, "ssh_keys": {
                  "ed25519_private": (directory / "host").read_text(),
                  "ed25519_public": (directory / "host.pub").read_text().strip()}}
    (seed / "user-data").write_text("#cloud-config\n" + json.dumps(config))
    (seed / "meta-data").write_text(json.dumps({"instance-id": directory.name, "local-hostname": "jetpack-builder"}))
    (directory / "known_hosts").write_text(f"[127.0.0.1]:{port} " + (directory / "host.pub").read_text())
    run("hdiutil", "makehybrid", "-iso", "-joliet", "-default-volume-name", "cidata",
        "-o", directory / "seed.iso", seed, stdout=subprocess.DEVNULL)
    disk = directory / "build.qcow2"
    run("qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b",
        downloads / "jammy-server-cloudimg-amd64.img", disk, "80G")
    helpers = directory / "helpers"
    helpers.mkdir()
    for name in ("build.sh", "validate_rootfs.py", "release.json"):
        shutil.copy2(HERE / name, helpers / name)
    option_path = lambda p: str(p).replace(",", ",,")
    args = ["qemu-system-x86_64", "-name", "jetpack-sd-builder", "-machine", "q35",
            "-accel", "tcg,thread=multi", "-cpu", "max", "-smp", "4", "-m", "8192",
            "-drive", f"file={option_path(disk)},format=qcow2,if=virtio",
            "-drive", f"file={option_path(directory / 'seed.iso')},format=raw,media=cdrom,readonly=on",
            "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{port}-:22",
            "-device", "virtio-net-pci,netdev=net0",
            "-fsdev", f"local,id=downloads,path={option_path(downloads)},security_model=none,readonly=on",
            "-device", "virtio-9p-pci,fsdev=downloads,mount_tag=downloads",
            "-fsdev", f"local,id=helpers,path={option_path(helpers)},security_model=none,readonly=on",
            "-device", "virtio-9p-pci,fsdev=helpers,mount_tag=helpers",
            "-display", "none", "-serial", f"file:{directory / 'serial.log'}"]
    print("Starting isolated Ubuntu amd64 builder (8 GiB RAM); no host disks or USB attached", flush=True)
    with (directory / "qemu.log").open("wb") as log:
        process = subprocess.Popen(args, stdout=log, stderr=log)
        success = False
        try:
            deadline, last = time.monotonic() + 900, 0
            while True:
                require(process.poll() is None, "VM exited; inspect " + str(directory / "qemu.log"))
                probe = subprocess.run(ssh_args(vm) + ["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if probe.returncode == 0:
                    break
                require(time.monotonic() < deadline, "VM SSH startup timed out; inspect " + str(directory / "serial.log"))
                if time.monotonic() - last > 30:
                    print("Waiting for the VM's pinned SSH host key...", flush=True)
                    last = time.monotonic()
                time.sleep(5)
            ssh(vm, "sudo cloud-init status --wait")
            yield vm
            success = True
        finally:
            if process.poll() is None:
                try:
                    ssh(vm, "sudo poweroff", timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except (subprocess.SubprocessError, OSError):
                    pass
                try:
                    process.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            if success:
                shutil.rmtree(directory)
            else:
                print("Build failed; stopped VM and retained diagnostics at " + str(directory), flush=True)


def build_image(cache, output):
    require(shutil.disk_usage(cache).free >= 60 * 1024**3, "A fresh image build requires 60 GiB free on the cache volume")
    require(int(subprocess.check_output(["sysctl", "-n", "hw.memsize"])) >= 16 * 1024**3,
            "A fresh image build requires a Mac with at least 16 GiB RAM")
    for command in ("qemu-system-x86_64", "qemu-img", "ssh", "ssh-keygen", "curl", "hdiutil"):
        require(shutil.which(command), "Missing dependency: " + command + "; see docs/jetpack-sd-mac.md")
    downloads = cache / "downloads"
    downloads.mkdir(exist_ok=True)
    for item in RELEASE["downloads"]:
        download(item, downloads)
    with linux_vm(cache, downloads) as vm:
        # The shell is supplied over stdin; it mounts only the two read-only shares.
        with (HERE / "build.sh").open("rb") as source:
            ssh(vm, "sudo bash -s", stdin=source)
        output.mkdir(parents=True, exist_ok=True)
        for name in (IMAGE_NAME, "image.json"):
            partial = output / (name + ".part")
            print("Retrieving " + name, flush=True)
            with partial.open("wb") as dest:
                ssh(vm, "cat /home/flash/jetpack-sd/" + name, stdout=dest)
            partial.replace(output / name)
        manifest = json.loads((output / "image.json").read_text())
        validate_image(output / IMAGE_NAME, manifest, shutil.which("zstd"))


def atomic_copy(source, destination):
    """Replace our output even when an earlier privileged run owned the old file."""
    handle, name = tempfile.mkstemp(prefix="." + destination.name + "-", dir=destination.parent)
    os.close(handle)
    temporary = Path(name)
    try:
        shutil.copyfile(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def write_card(image_dir, disk, target, zstd):
    require(sys.stdin.isatty(), "Run the flash command in Terminal so sudo can authenticate locally")
    # Expand as the normal user; the writer needs only Apple's Python and a raw
    # image. No Homebrew executable runs as root.
    stage = Path(tempfile.mkdtemp(prefix="live-vision-jetpack-", dir="/private/tmp"))
    success = False
    try:
        source, image = image_dir / IMAGE_NAME, stage / IMAGE_NAME.removesuffix(".zst")
        size = json.loads((image_dir / "image.json").read_text())["size_bytes"]
        require(shutil.disk_usage(stage).free >= size + 1024**3, "Need space for the raw image on the Mac's temporary volume")
        print("Expanding the verified image before administrator authentication...", flush=True)
        run(zstd, "-q", "-d", str(source), "-o", image)
        image.chmod(0o444)
        for name, src in (("image.json", image_dir / "image.json"), ("write_sd.py", HERE / "write_sd.py")):
            shutil.copy2(src, stage / name)
        receipt = stage / "flash-receipt.json"
        request = {"disk": disk, "target": target, "image": str(image), "manifest": str(stage / "image.json"),
                   "zstd": zstd, "receipt": str(receipt)}
        (stage / "request.json").write_text(json.dumps(request, indent=2))
        print("Authenticate with sudo in this terminal. Keep the card inserted until verification finishes.", flush=True)
        # AppleScript's administrator helper is attributed to authtrampoline by
        # TCC and does not inherit the launching app's removable-disk permission.
        # Normal sudo retains the terminal's privacy context. Never capture a password.
        command = ["/usr/bin/sudo", "/usr/bin/python3", "-u", str(stage / "write_sd.py"),
                   "--request", str(stage / "request.json"), "--write"]
        with (stage / "write.log").open("w") as output:
            # Keep the terminal's process group: changing it breaks macOS sudo's
            # credential context. sudo reads its password directly from /dev/tty.
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
        offset = 0
        try:
            while process.poll() is None:
                log = stage / "write.log"
                if log.exists():
                    with log.open() as stream:
                        stream.seek(offset)
                        print(stream.read(), end="", flush=True)
                        offset = stream.tell()
                time.sleep(1)
            log = stage / "write.log"
            if log.exists():
                with log.open() as stream:
                    stream.seek(offset)
                    print(stream.read(), end="", flush=True)
                atomic_copy(log, image_dir / "write.log")
            require(process.returncode == 0 and receipt.exists(), "Flash did not complete; inspect " + str(image_dir / "write.log"))
            atomic_copy(receipt, image_dir / "flash-receipt.json")
            success = True
        except KeyboardInterrupt:
            # Do not remove the image while the privileged writer is still using it.
            print("Waiting for the writer to stop; an interrupted card is not verified...", flush=True)
            process.wait()
            if receipt.exists():
                atomic_copy(receipt, image_dir / "flash-receipt.json")
            raise
    finally:
        if success:
            shutil.rmtree(stage)
        else:
            print("Retained flash diagnostics and staged image at " + str(stage), flush=True)
    print("Receipt: " + str(image_dir / "flash-receipt.json"), flush=True)
    print("Boot the SD on the Orin, complete first-boot setup, enable SSH, then follow the README Quickstart.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disk", help="whole SD device, e.g. /dev/disk4; never selected automatically")
    parser.add_argument("--erase", action="store_true", help="authorize erasing the explicitly selected SD card")
    parser.add_argument("--dry-run", action="store_true", help="validate the card and image without writing")
    parser.add_argument("--build-only", action="store_true", help="build and validate an image without accessing an SD card")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--image-dir", type=Path, help="reuse a previously built image and image.json")
    args = parser.parse_args()
    require(platform.system() == "Darwin" and platform.machine() == "arm64", "This workflow supports Apple Silicon macOS")
    require(int(platform.mac_ver()[0].split(".")[0]) >= 15, "Use macOS 15 or newer")
    require(os.geteuid() != 0, "Run as your normal Mac user; only the SD writer requests administrator authentication")
    require(subprocess.run(["xcode-select", "-p"], capture_output=True).returncode == 0,
            "Install Apple Command Line Tools first: xcode-select --install")
    require(not (args.erase and (args.dry_run or args.build_only)), "--erase cannot be combined with --dry-run or --build-only")
    require(args.build_only or args.disk, "Use diskutil list to identify the SD card, then supply --disk /dev/diskN")
    zstd = shutil.which("zstd")
    require(zstd, "Install dependencies first: brew install python qemu zstd")
    target = None if args.build_only else target_identity(args.disk)
    if target:
        print(f"Selected {args.disk}: {target['MediaName']}, {target['TotalSize'] / 1e9:.1f} GB, {target['BusProtocol']}", flush=True)
    cache = args.cache_dir.expanduser().resolve()
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (cache / "operation.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another JetPack operation is using this cache") from None
        output = args.image_dir.expanduser().resolve() if args.image_dir else cache / "image"
        if args.image_dir:
            require((output / "image.json").is_file(), "--image-dir must contain image.json and the .img.zst")
        elif not (output / "image.json").is_file():
            build_image(cache, output)
        manifest = json.loads((output / "image.json").read_text())
        validate_image(output / IMAGE_NAME, manifest, zstd)
        if args.build_only:
            print("Validated image: " + str(output), flush=True)
        else:
            require(target_identity(args.disk) == target, "Card changed while building; select it again")
            if args.erase:
                write_card(output, args.disk, target, zstd)
            else:
                print("PREFLIGHT_PASSED: no SD writes. Add --erase to flash this card.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        sys.exit("ERROR: " + str(error))
