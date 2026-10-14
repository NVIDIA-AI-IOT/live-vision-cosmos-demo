#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Resume an engine download, authenticate its bytes, and install it atomically."""
import argparse
import http.client
import ipaddress
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.error
import urllib.request

from bundle import digest, external_output, verify

MAX_BYTES = 64 * 1024**3


def check_url(url):
    parsed = urllib.parse.urlsplit(url)
    # HTTP is useful only for local test fixtures; real downloads require TLS.
    loopback = False
    try:
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        pass
    if parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("Download URL must have a host and no embedded credentials")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("Downloads require HTTPS")


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        if urllib.parse.urlsplit(req.full_url).scheme == "https" and not newurl.startswith("https://"):
            raise ValueError("Refusing a redirect from HTTPS to HTTP")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, expected_sha256, output, max_bytes=MAX_BYTES):
    check_url(url)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("An expected SHA-256 from the trusted release record is required")
    output = external_output(output)
    if output.exists():
        if output.is_file() and digest(output) == expected_sha256:
            return output
        raise ValueError("Destination already exists with different content")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".part")
    if partial.is_symlink() or (partial.exists() and not partial.is_file()):
        raise ValueError("Partial download must be a regular file")
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > max_bytes:
        raise ValueError("Partial download exceeds the size limit")
    # A complete .part file can result from interruption just before the atomic rename.
    if partial.exists() and digest(partial) == expected_sha256:
        os.replace(partial, output)
        return output
    headers = {"Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    opener = urllib.request.build_opener(SafeRedirect())
    try:
        response = opener.open(urllib.request.Request(url, headers=headers), timeout=60)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and offset:
            # Stale or corrupt complete partial file: restart once without Range.
            partial.unlink()
            return download(url, expected_sha256, output, max_bytes)
        raise
    with response:
        if response.status == 206:
            content_range = response.headers.get("Content-Range", "")
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
            if not match or int(match[1]) != offset or int(match[2]) < offset or int(match[3]) > max_bytes:
                raise ValueError("Server returned an invalid resume range")
            expected_size = int(match[3])
            mode = "ab"
        elif response.status == 200:
            # Server ignored Range: discard the stale prefix and restart safely.
            offset, mode = 0, "wb"
            length = response.headers.get("Content-Length")
            expected_size = int(length) if length is not None else None
            if expected_size is not None and expected_size > max_bytes:
                raise ValueError("Download exceeds the size limit")
        else:
            raise ValueError(f"Unexpected download status: {response.status}")
        size = offset
        with partial.open(mode) as stream:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("Download exceeds the size limit")
                stream.write(chunk)
        if expected_size is not None and size != expected_size:
            raise ValueError("Download interrupted; rerun the same command to resume")
    if digest(partial) != expected_sha256:
        partial.unlink()  # A corrupt complete prefix must not be resumed on the next attempt.
        raise ValueError("SHA-256 mismatch; discarded the corrupt download")
    os.replace(partial, output)
    return output


def install(archive, output, max_bytes=MAX_BYTES):
    output = external_output(output)
    if output.exists():
        raise ValueError("Bundle destination already exists; choose a new directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=".bundle-install-", dir=output.parent))
    try:
        seen, size = set(), 0
        with tarfile.open(archive, "r:*") as source:
            for member in source:
                name = member.name.removeprefix("./").rstrip("/")
                if not name and member.isdir():
                    continue
                path = PurePosixPath(name)
                if (not name or path.is_absolute() or ".." in path.parts or str(path) != name
                        or name in seen or not (member.isfile() or member.isdir())):
                    raise ValueError(f"Unsafe archive member: {member.name}")
                seen.add(name)
                size += member.size
                if size > max_bytes or len(seen) > 10000:
                    raise ValueError("Archive exceeds the extraction limit")
                target = scratch / name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(member) as reader, target.open("xb") as writer:
                        shutil.copyfileobj(reader, writer)
        verify(scratch)
        os.rename(scratch, output)
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--sha256", required=True, help="Trusted checksum supplied separately from the download")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        # Reject unsafe/existing destinations before transferring a large file.
        if external_output(args.output).exists():
            raise ValueError("Bundle destination already exists")
        archive = download(args.url, args.sha256, args.archive)
        print(install(archive, args.output))
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, http.client.HTTPException) as exc:
        parser.exit(2, f"Install failed: {exc}\n")


if __name__ == "__main__":
    main()
