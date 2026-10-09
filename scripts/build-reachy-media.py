#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Build and verify private LGPL FFmpeg/PyAV dependencies before atomic promotion.

Run as the deployment account on Linux ARM64; this helper needs no sudo and does
not change services. Failed builds leave reachy-media/current and reachy_env alone.
"""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parent.parent
LIBRARIES = ("avcodec", "avformat", "avdevice", "avutil", "avfilter", "swresample", "swscale")


class BuildError(RuntimeError):
    pass


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    """Replace receipts atomically, including on a failed or interrupted retry."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def load_manifest(path):
    manifest = json.loads(Path(path).read_text())
    if manifest.get("schema_version") != 1 or set(manifest.get("sources", {})) != {"lame", "ffmpeg", "pyav"}:
        raise BuildError("Unsupported Reachy media source manifest")
    for name, source in manifest["sources"].items():
        if not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
            raise BuildError(f"Invalid source checksum: {name}")
        if urllib.parse.urlsplit(source["url"]).scheme != "https":
            raise BuildError(f"Source URL must use HTTPS: {name}")
        for field in ("filename", "root"):
            value = source[field]
            if not value or value in (".", "..") or Path(value).name != value or "\\" in value:
                raise BuildError(f"Unsafe {field} in source manifest: {name}")
        for value in source["licenses"]:
            if PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts:
                raise BuildError(f"Unsafe license path in source manifest: {name}")
    flags = manifest["ffmpeg_configure"]
    for required in ("--disable-gpl", "--disable-nonfree", "--disable-version3", "--disable-autodetect", "--disable-static", "--enable-shared"):
        if required not in flags:
            raise BuildError(f"Missing required FFmpeg restriction: {required}")
    if any(flag in flags for flag in ("--enable-gpl", "--enable-nonfree", "--enable-version3")):
        raise BuildError("GPL, nonfree and version3 FFmpeg options are forbidden")
    return manifest


class HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise BuildError("Refusing source download redirect away from HTTPS")
        return super().redirect_request(request, response, code, message, headers, newurl)


def download(source, cache, attempts=3):
    """Never use an unchecked cache entry or publish an incomplete download."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / source["filename"]
    if target.is_file() and not target.is_symlink() and sha256(target) == source["sha256"]:
        return target
    if target.exists() or target.is_symlink():
        target.unlink()
    opener = urllib.request.build_opener(HTTPSRedirectHandler())
    for attempt in range(attempts):
        descriptor, temporary_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".part", dir=cache)
        temporary = Path(temporary_name)
        try:
            request = urllib.request.Request(source["url"], headers={"User-Agent": "Reachy-Media-Source-Builder/1"})
            with os.fdopen(descriptor, "wb") as output, opener.open(request, timeout=60) as response:
                if urllib.parse.urlsplit(response.geturl()).scheme != "https":
                    raise BuildError("Source response did not use HTTPS")
                started = time.monotonic()
                size = 0
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    if size > 64 * 1024 * 1024 or time.monotonic() - started > 180:
                        raise BuildError("Source download exceeded 64 MiB or 180 seconds")
                    output.write(block)
            if sha256(temporary) != source["sha256"]:
                raise BuildError(f"SHA-256 mismatch downloading {source['filename']}")
            os.replace(temporary, target)
            return target
        except (OSError, urllib.error.URLError, BuildError) as error:
            if attempt + 1 == attempts:
                raise BuildError(f"Source download failed: {source['filename']}: {error}") from error
            time.sleep(min(2 ** attempt, 4))
        finally:
            temporary.unlink(missing_ok=True)
    raise BuildError("Source download exhausted retries")


def extract_source(archive_path, output, expected_root):
    """Pinned releases contain no links: reject links, devices and traversal."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:*") as archive:
        members = archive.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if (not path.parts or path.is_absolute() or ".." in path.parts
                    or path.parts[0] != expected_root or "\\" in member.name
                    or not (member.isfile() or member.isdir())):
                raise BuildError(f"Unsafe source archive member: {member.name}")
        # All members are checked before writing; never preserve archive owners,
        # special bits, or use tarfile's version-dependent extraction defaults.
        for member in members:
            destination = output.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
            destination.chmod(0o755 if member.mode & 0o111 else 0o644)
    source_root = output / expected_root
    if not source_root.is_dir():
        raise BuildError(f"Missing source root: {expected_root}")
    return source_root


def platform_identity():
    return {
        "system": platform.system(), "machine": platform.machine(),
        "python": platform.python_version(), "soabi": sysconfig.get_config_var("SOABI"),
        "libc": list(platform.libc_ver()),
    }


def build_identity(manifest_path, requirements_path, verifier_path):
    inputs = {
        "manifest_sha256": sha256(manifest_path),
        "requirements_sha256": sha256(requirements_path),
        "builder_sha256": sha256(__file__),
        "verifier_sha256": sha256(verifier_path),
        "platform": platform_identity(),
    }
    digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    return digest, inputs


@contextlib.contextmanager
def build_lock(base):
    with (base / ".build.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BuildError(f"Another Reachy media build owns {base / '.build.lock'}") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def build_environment(prefix):
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH", "LIBRARY_PATH", "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "CC", "CXX", "CPP", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "PKG_CONFIG", "PKG_CONFIG_SYSROOT_DIR", "VIRTUAL_ENV", "PIP_TARGET", "PIP_PREFIX", "PIP_USER"):
        env.pop(key, None)
    env.update({
        "PATH": "/usr/local/bin:/usr/bin:/bin", "CC": "cc", "CXX": "c++",
        "CFLAGS": "-O2 -fPIC", "CXXFLAGS": "-O2 -fPIC",
        "CPPFLAGS": f"-I{prefix / 'include'}",
        "LDFLAGS": f"-L{prefix / 'lib'} -Wl,-rpath,{prefix / 'lib'}",
        "PKG_CONFIG_PATH": "", "PKG_CONFIG_LIBDIR": str(prefix / "lib/pkgconfig"),
        "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "LC_ALL": "C.UTF-8",
    })
    return env


def run(command, cwd, env, log, timeout=3600):
    """Keep full build output in the prefix, with a bounded command runtime."""
    command = [str(value) for value in command]
    print(f"Reachy media: {log.name}", flush=True)
    with log.open("a") as stream:
        stream.write("$ " + shlex.join(command) + "\n")
        stream.flush()
        try:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            try:
                returncode = process.wait(timeout=timeout)
            except BaseException:
                # Stop the compiler children as well as make/pip itself.
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise
            if returncode:
                raise subprocess.CalledProcessError(returncode, command)
        except (subprocess.SubprocessError, OSError) as error:
            raise BuildError(f"Command failed; see {log}: {error}") from error


def verify_prefix(prefix, verifier_path):
    env = build_environment(prefix)
    # Deliberately no LD_LIBRARY_PATH: installed objects must resolve private
    # FFmpeg libraries through their embedded runtime paths.
    run([prefix / "venv/bin/python", "-B", verifier_path, "--prefix", prefix,
         "--output", prefix / "verification.json"], prefix, env,
        prefix / "logs/verify.log", timeout=180)


def promote(base, prefix):
    current = base / "current"
    if current.exists() and not current.is_symlink():
        raise BuildError(f"Refusing to replace a real directory at {current}")
    temporary = base / f".current-{os.getpid()}"
    try:
        temporary.symlink_to(prefix.name, target_is_directory=True)
        os.replace(temporary, current)
    finally:
        temporary.unlink(missing_ok=True)


def compile_prefix(prefix, manifest, manifest_path, requirements_path, verifier_path, cache, jobs, inputs):
    prefix.mkdir(parents=True)
    (prefix / "logs").mkdir()
    (prefix / "sources").mkdir()
    (prefix / "licenses").mkdir()
    (prefix / "wheels").mkdir()
    shutil.copyfile(manifest_path, prefix / "source-manifest.json")
    shutil.copyfile(requirements_path, prefix / "requirements.txt")
    shutil.copyfile(__file__, prefix / "build-reachy-media.py")
    shutil.copyfile(verifier_path, prefix / "verify-reachy-media.py")
    receipt = {"schema_version": 1, "inputs": inputs, "prefix": str(prefix), "jobs": jobs,
               "source_manifest": manifest, "status": "building"}
    write_json(prefix / "build-manifest.json", receipt)
    env = build_environment(prefix)
    trees = {}
    for name, source in manifest["sources"].items():
        cached = download(source, cache)
        archive = prefix / "sources" / source["filename"]
        shutil.copyfile(cached, archive)
        if sha256(archive) != source["sha256"]:
            raise BuildError(f"Copied source checksum mismatch: {name}")
        trees[name] = extract_source(archive, prefix / "work", source["root"])
        notices = prefix / "licenses" / name
        notices.mkdir()
        for license_path in source["licenses"]:
            shutil.copyfile(trees[name] / license_path, notices / Path(license_path).name)

    for name in ("lame", "ffmpeg"):
        command = [trees[name] / "configure", f"--prefix={prefix}", f"--libdir={prefix / 'lib'}"]
        command.extend(manifest[f"{name}_configure"])
        if name == "ffmpeg":
            command.extend([f"--extra-cflags=-I{prefix / 'include'}",
                            f"--extra-ldflags={env['LDFLAGS']}"])
        run(command, trees[name], env, prefix / "logs" / f"{name}-configure.log", timeout=600)
        run(["make", f"-j{jobs}"], trees[name], env, prefix / "logs" / f"{name}-build.log")
        run(["make", "install"], trees[name], env, prefix / "logs" / f"{name}-install.log", timeout=600)
    for name in LIBRARIES:
        if not (prefix / "lib/pkgconfig" / f"lib{name}.pc").is_file():
            raise BuildError(f"Private FFmpeg pkg-config metadata missing: lib{name}")
    # PyAV is built and installed before requirements, preventing pip from ever
    # choosing the GPL FFmpeg bundled in the published PyAV wheel.
    run([sys.executable, "-m", "venv", prefix / "venv"], prefix, env, prefix / "logs/venv.log", timeout=180)
    python = prefix / "venv/bin/python"
    pip = [python, "-m", "pip", "--isolated", "--disable-pip-version-check"]
    run(pip + ["install", "--no-cache-dir"] + manifest["build_tools"], prefix, env, prefix / "logs/build-tools.log", timeout=600)
    run(pip + ["wheel", "--no-cache-dir", "--no-build-isolation", "--no-deps", "--no-binary=av",
               "--wheel-dir", prefix / "wheels", prefix / "sources" / manifest["sources"]["pyav"]["filename"]],
        prefix, env, prefix / "logs/pyav-build.log")
    wheels = list((prefix / "wheels").glob("av-*.whl"))
    if len(wheels) != 1:
        raise BuildError("Expected exactly one locally built PyAV wheel")
    run(pip + ["install", "--no-deps", wheels[0]], prefix, env, prefix / "logs/pyav-install.log", timeout=180)
    run(pip + ["install", "--no-binary=av", "--no-build-isolation", "-r", prefix / "requirements.txt"],
        prefix, env, prefix / "logs/requirements.log", timeout=1200)
    run(pip + ["check"], prefix, env, prefix / "logs/pip-check.log", timeout=180)
    # Keep configure receipts, source archives, notices and the built wheel.
    shutil.copyfile(trees["ffmpeg"] / "ffbuild/config.log", prefix / "logs/ffmpeg-config.log")
    shutil.copyfile(trees["ffmpeg"] / "config.h", prefix / "logs/ffmpeg-config.h")
    verify_prefix(prefix, verifier_path)
    shutil.copyfile(prefix / "verification.json", prefix / "build-verification.json")
    receipt.update({"status": "verified", "pyav_wheel_sha256": sha256(wheels[0]),
                    "verification_report": "build-verification.json",
                    "verification_sha256": sha256(prefix / "build-verification.json")})
    write_json(prefix / "build-manifest.json", receipt)
    # Build intermediates are disposable; the exact source archives are retained.
    shutil.rmtree(prefix / "work")
    write_json(prefix / "complete.json", {"inputs": inputs})


def build(install_root, manifest_path, cache=None, jobs=2, requirements_path=None, verifier_path=None):
    if not 1 <= jobs <= 8:
        raise BuildError("--jobs must be between 1 and 8")
    identity = platform_identity()
    if identity["system"] != "Linux" or identity["machine"] not in ("aarch64", "arm64"):
        raise BuildError("Reachy media must be built natively on Linux ARM64")
    requirements_path = Path(requirements_path or ROOT / "reachy/requirements.txt").resolve()
    verifier_path = Path(verifier_path or ROOT / "scripts/verify-reachy-media.py").resolve()
    manifest_path = Path(manifest_path).resolve()
    manifest = load_manifest(manifest_path)
    key, inputs = build_identity(manifest_path, requirements_path, verifier_path)
    base = Path(install_root).resolve() / "reachy-media"
    # Autoconf, make and compiler flag parsers do not reliably support these paths.
    if re.search(r"[\s,:]", str(base)):
        raise BuildError("Install root cannot contain whitespace, commas or colons")
    base.mkdir(parents=True, exist_ok=True)
    prefix = base / key
    cache = Path(cache).resolve() if cache else base / "downloads"
    with build_lock(base):
        if prefix.is_symlink():
            raise BuildError(f"Refusing symlinked build prefix: {prefix}")
        if (prefix / "complete.json").is_file():
            if json.loads((prefix / "complete.json").read_text()).get("inputs") != inputs:
                raise BuildError(f"Cached build receipt does not match current inputs: {prefix}")
            verify_prefix(prefix, verifier_path)
        else:
            if prefix.exists() or prefix.is_symlink():
                if prefix.is_symlink() or (base / "current").resolve() == prefix.resolve():
                    raise BuildError(f"Refusing to rebuild an active or symlinked prefix: {prefix}")
                shutil.rmtree(prefix)
            compile_prefix(prefix, manifest, manifest_path, requirements_path, verifier_path, cache, jobs, inputs)
        promote(base, prefix)
    return prefix


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-root", required=True, type=Path)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--manifest", type=Path, default=ROOT / "scripts/reachy-media-sources.json")
    args = parser.parse_args(argv)
    try:
        prefix = build(args.install_root, args.manifest, args.cache, args.jobs)
    except (BuildError, OSError, ValueError, KeyError, tarfile.TarError) as error:
        print(f"Reachy media build failed: {error}", file=sys.stderr)
        return 1
    print(f"Reachy media verified and selected: {prefix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
