#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Read-only validation of a pristine NVIDIA sample rootfs after apply_binaries."""
import json
import os
from pathlib import Path
import re
import sys


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def validate(root):
    release = (root / "etc/nv_tegra_release").read_text().splitlines()[0]
    require("R39" in release and "REVISION: 2.1" in release, "Unexpected L4T release")
    require('VERSION_ID="24.04"' in (root / "etc/os-release").read_text(), "Unexpected Ubuntu release")
    names = re.compile(r"cosmos3|live[-_ ]?(?:vision|vlm)", re.I)
    matches = []
    for folder, directories, files in os.walk(root, followlinks=False):
        relative = Path(folder).relative_to(root)
        if relative.parts and relative.parts[0] in {"proc", "sys", "dev", "run"}:
            directories[:] = []
            continue
        matches.extend(str(relative / name) for name in directories + files if names.search(str(relative / name)))
    for folder in ("etc/systemd/system", "etc/init.d", "etc/cron.d", "etc/xdg/autostart"):
        for path in (root / folder).rglob("*"):
            text = os.readlink(path) if path.is_symlink() else path.read_text(errors="replace") if path.is_file() else ""
            if names.search(text):
                matches.append(str(path.relative_to(root)))
    require(not matches, "Unexpected demo payload: " + repr(matches))
    users = [line.split(":")[0] for line in (root / "etc/passwd").read_text().splitlines()
             if 1000 <= int(line.split(":")[2]) < 65534]
    require(not users, "Unexpected precreated user")
    require(not list((root / "home").rglob("authorized_keys"))
            and not list((root / "root").rglob("authorized_keys")), "Unexpected SSH authorization")
    packages = {}
    for stanza in (root / "var/lib/dpkg/status").read_text().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in stanza.splitlines()
                      if ": " in line and not line[0].isspace())
        status = fields.get("Status", "")
        require(not status or status.split()[-1] in {"installed", "config-files", "not-installed"},
                "Unconfigured package: " + fields.get("Package", ""))
        if status == "install ok installed":
            packages[fields["Package"]] = fields.get("Version", "")
    for package in ("nvidia-l4t-core", "nvidia-l4t-oobe"):
        require(packages.get(package, "").startswith("39.2.1-"), "Missing " + package)
    require("nv-oobe.target" in os.readlink(root / "etc/systemd/system/default.target"), "First-boot setup disabled")
    require(not (root / "etc/cloud/cloud.cfg.d/99-nv-preseed.cfg").exists(), "Unexpected first-boot preseed")
    for name in ("Image", "initrd"):
        require((root / "boot" / name).stat().st_size > 0, "Missing boot " + name)
    require("root=/dev/mmcblk0p1" in (root / "boot/extlinux/extlinux.conf").read_text(), "Wrong root device")
    return {"rootfs_validation_passed": True, "jetpack_version": "7.2.1",
            "jetson_linux_release": release, "ubuntu_version": "24.04",
            "board": "jetson-orin-nano-devkit-super", "excluded_application_matches": [],
            "precreated_users": [], "authorized_ssh_keys": [], "first_boot_target": "nv-oobe.target",
            "package_set": "Jetson Linux OS and NVIDIA BSP drivers; full JetPack compute SDK not installed",
            "cosmos3_edge_installed": False, "live_vision_ui_installed": False,
            "full_jetpack_compute_sdk_installed": False}


if __name__ == "__main__":
    print(json.dumps(validate(Path(sys.argv[1]).resolve()), indent=2))
