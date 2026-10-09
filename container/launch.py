#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Run an immutable local/private image with a verified, separate engine bundle."""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess

from bundle import SPEC, verify

IMAGE = re.compile(r"(?:sha256:|[A-Za-z0-9_./:-]+@sha256:)[0-9a-f]{64}\Z")


def docker_command(image, bundle, tls, port, host, reachy=None):
    if not IMAGE.fullmatch(image):
        raise ValueError("Use a local sha256 image ID or a registry image pinned by @sha256 digest")
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    import ipaddress
    address = ipaddress.ip_address(host)
    publish_host = f"[{host}]" if address.version == 6 else host
    command = ["docker", "run", "--rm", "--init", "--runtime=nvidia", "--read-only",
               "--cap-drop=ALL", "--security-opt=no-new-privileges",
               "--user", f"{os.getuid()}:{os.getgid()}",
               "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m", "--shm-size=512m",
               "--mount", f"type=bind,src={bundle},dst=/models,readonly",
               "--mount", f"type=bind,src={tls},dst=/tls,readonly",
               "--publish", f"{publish_host}:{port}:8443"]
    if reachy:
        command += ["--env", "REACHY_DAEMON_URL=" + reachy]
    return command + [image]


def check_host(spec):
    target = spec["target"]
    if platform.system() != "Linux" or platform.machine() != target["architecture"]:
        raise ValueError("Use a supported Jetson, not this host, to run the image")
    release = subprocess.check_output(
        ["dpkg-query", "-W", "-f=${Version}", "nvidia-l4t-core"], text=True).strip()
    if not release.startswith(target["l4t"] + "-"):
        raise ValueError("Host JetPack/L4T does not match this engine bundle")
    if "Orin Nano" not in Path("/proc/device-tree/model").read_text():
        raise ValueError("This runtime supports only the Orin Nano Super 8GB")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--tls-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--bind", default="127.0.0.1", help="Use the Jetson LAN IP to allow other devices")
    parser.add_argument("--reachy-daemon-url")
    parser.add_argument("--dry-run", action="store_true", help="Verify artifacts and print the command; skip host/GPU checks")
    args = parser.parse_args()
    try:
        bundle, tls = args.bundle.expanduser().resolve(), args.tls_dir.expanduser().resolve()
        if any("," in str(p) or "\n" in str(p) for p in (bundle, tls)):
            raise ValueError("Mount paths cannot contain commas or newlines")
        manifest = verify(bundle)
        if not all((tls / p).is_file() for p in ("cert.pem", "key.pem")):
            raise ValueError("TLS directory must contain cert.pem and key.pem")
        command = docker_command(args.image, bundle, tls, args.port, args.bind, args.reachy_daemon_url)
        if args.dry_run:
            print(shlex.join(command))
            return
        check_host(json.loads(SPEC.read_text()))
        image_commit = subprocess.check_output([
            "docker", "image", "inspect", args.image, "--format",
            '{{ index .Config.Labels "org.opencontainers.image.revision" }}'], text=True).strip()
        if image_commit != manifest["candidate_commit"]:
            raise ValueError("Bundle/image candidate commit mismatch")
        subprocess.run(command, check=True)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(2, f"Launch failed: {exc}\n")


if __name__ == "__main__":
    main()
