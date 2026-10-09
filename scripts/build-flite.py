#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Build and atomically select pinned, WAV-only Flite/SLT on native Linux ARM64."""
import argparse
import array
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import resource
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / "container" if (ROOT / "container/download.py").is_file() else Path(__file__).parent
sys.path.insert(0, str(SUPPORT))
from download import download

CONFIGURE = ["--with-audio=none", "--disable-shared"]
MAKE = ["LANGS=usenglish", "LEXES=cmulex", "VOXES=cmu_us_slt", "EXTRABINS=", "AUDIOLIBS="]
MAX_SOURCE_BYTES = 256 * 1024**2


class BuildError(RuntimeError):
    pass


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_manifest(m):
    revision = m.get("revision", "")
    if (m.get("schema_version") != 1 or m.get("name") != "flite"
            or m.get("voice") != "slt" or m.get("voice_directory") != "lang/cmu_us_slt"
            or not re.fullmatch(r"[0-9a-f]{40}", revision)
            or m.get("directory") != "flite-" + revision
            or m.get("url") != "https://codeload.github.com/festvox/flite/tar.gz/" + revision
            or not re.fullmatch(r"[0-9a-f]{64}", m.get("sha256", ""))):
        raise BuildError("A pinned Flite source/SLT manifest is required")


def extract(archive, target, directory):
    """Same regular-file-only boundary as the shared artifact installer.

    Engine bundle validation is inapplicable to upstream source archives. Reject
    links and special files outright; this exact upstream archive needs neither.
    """
    if target.exists() or target.is_symlink():
        raise BuildError("Refusing to overlay a source tree")
    with tarfile.open(archive, "r:*") as source:
        members = source.getmembers()
        if len(members) > 10000 or sum(m.size for m in members) > MAX_SOURCE_BYTES:
            raise BuildError("Source extraction limit exceeded")
        seen = set()
        for member in members:
            name = member.name.rstrip("/")
            path = PurePosixPath(name)
            if (not path.parts or path.is_absolute() or ".." in path.parts or str(path) != name
                    or path.parts[0] != directory or name in seen
                    or not (member.isfile() or member.isdir())):
                raise BuildError(f"Unsafe source member: {member.name}")
            seen.add(name)
        target.mkdir()
        try:
            for member in members:
                relative = PurePosixPath(member.name).relative_to(directory)
                out = target / relative
                if member.isdir():
                    out.mkdir(parents=True, exist_ok=True)
                else:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(member) as reader, out.open("xb") as writer:
                        shutil.copyfileobj(reader, writer)
                    out.chmod(0o755 if member.mode & 0o111 else 0o644)
                    # Preserve configure/configure.in ordering to avoid invoking
                    # unpinned autoconf merely because extraction changed mtimes.
                    os.utime(out, (member.mtime, member.mtime))
        except BaseException:
            shutil.rmtree(target)
            raise


def environment():
    # Inference's CUDA paths and arbitrary compiler/preload options must not leak
    # into this CPU-only build or its native verification.
    return {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LC_ALL": "C.UTF-8",
            "ac_cv_header_alsa_asoundlib_h": "no", "CFLAGS": "-O2"}


def available_kib():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])
    raise BuildError("Cannot determine available memory")


def child_limits():
    os.nice(10)
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))


def stop(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def run(command, cwd, log, timeout=900):
    command = list(map(str, command))
    log.write("\n$ " + shlex.join(command) + "\n")
    log.flush()
    start = time.monotonic()
    process = subprocess.Popen(command, cwd=cwd, env=environment(), stdin=subprocess.DEVNULL,
                               stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                               preexec_fn=child_limits)
    try:
        while process.poll() is None:
            if time.monotonic() - start > timeout or available_kib() < 256 * 1024:
                raise BuildError("Speech build exceeded its time/memory boundary")
            time.sleep(0.1)
        if process.returncode:
            raise BuildError(f"Command failed ({process.returncode}); see {log.name}")
    except BaseException:
        stop(process)
        raise
    return {"argv": command, "elapsed_s": time.monotonic() - start}


def validate_libraries(paths):
    libraries = set()
    for name in paths:
        basename = Path(name).name
        if not re.fullmatch(r"(?:lib[cm]\.so(?:\.[0-9]+)*|ld-linux-aarch64\.so(?:\.[0-9]+)*)", basename):
            raise BuildError(f"Unexpected Flite runtime library: {name}")
        libraries.add(basename)
    if not any(n.startswith("libc.so") for n in libraries) or not any(n.startswith("libm.so") for n in libraries):
        raise BuildError("Native libc/libm evidence is incomplete")


def verify(prefix):
    executable = prefix / "bin/flite"
    ldd = subprocess.check_output(["ldd", str(executable)], env=environment(), text=True, timeout=10)
    if "not found" in ldd:
        raise BuildError("Flite has an unresolved shared library")
    paths = re.findall(r"(/[^\s]+)", ldd)
    validate_libraries(paths)
    voices = subprocess.check_output([str(executable), "-lv"], env=environment(), text=True, timeout=10)
    if voices.strip() != "Voices available: slt":
        raise BuildError("Flite must contain only the reviewed SLT voice")
    samples = []
    with tempfile.TemporaryDirectory(prefix="flite-verify-") as temporary:
        for rate in (0.8, 1.0, 1.5):
            wav = Path(temporary) / f"{rate}.wav"
            command = [str(executable), "-voice", "slt", "--setf", f"duration_stretch={1/rate:.9f}",
                       "-t", "A gray Game Boy sits beside a Jetson Orin Nano box and an orange toy.", "-o", str(wav)]
            with (Path(temporary) / "speech.log").open("w") as log:
                result = run(command, prefix, log, timeout=30)
            with wave.open(str(wav)) as audio:
                if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, "NONE"):
                    raise BuildError("Flite returned an unexpected WAV format")
                frames = audio.getnframes()
                data = audio.readframes(frames)
            values = array.array("h", data)
            if len(data) != frames * 2 or frames < 16000 or not values or max(map(abs, values)) < 100:
                raise BuildError("Flite returned silent/truncated speech")
            samples.append({"speed": rate, "seconds": frames / 16000, "sha256": digest(wav), **result})
    if not samples[0]["seconds"] > samples[1]["seconds"] > samples[2]["seconds"]:
        raise BuildError("Speech speed verification failed")
    return {"voice": "slt", "ldd": ldd, "library_paths": paths, "samples": samples,
            "binary_sha256": digest(executable)}


def payload(prefix):
    result = {}
    for name in ("bin", "archives", "sources", "licenses", "rebuild"):
        root = prefix / name
        if not root.is_dir() or root.is_symlink():
            raise BuildError(f"Missing retained build material: {name}")
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or (not path.is_file() and not path.is_dir()):
                raise BuildError(f"Unexpected retained file: {path}")
            if path.is_file():
                result[str(path.relative_to(prefix))] = {"sha256": digest(path), "mode": path.stat().st_mode & 0o777}
    return result


def activate(prefix, output):
    # Only an owned selection link is replaced; never relocate a user's directory.
    if output.exists() and not output.is_symlink():
        raise BuildError("Flite selection must be absent or a symlink; existing directory preserved")
    old = os.readlink(output) if output.is_symlink() else None
    with tempfile.TemporaryDirectory(prefix=".flite-activate-", dir=output.parent) as temporary:
        staged = Path(temporary) / "selection"
        staged.symlink_to(prefix)
        os.replace(staged, output)
        try:
            if digest(output / "bin/flite") != digest(prefix / "bin/flite"):
                raise BuildError("Selected speech executable changed")
        except BaseException:
            if old is None:
                output.unlink()
            else:
                staged.symlink_to(old)
                os.replace(staged, output)
            raise


def build(output, manifest_path, cache, jobs=2):
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text())
    validate_manifest(manifest)
    if platform.system() != "Linux" or platform.machine() not in {"aarch64", "arm64"}:
        raise BuildError("Flite builds require native Linux ARM64")
    if jobs not in (1, 2):
        raise BuildError("Use one or two build jobs on Orin")
    raw = Path(output).expanduser().absolute()
    output = raw.parent.resolve() / raw.name  # Do not resolve the selected generation.
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", str(output)) or "public" in output.parts:
        raise BuildError("Choose a non-public installation path without spaces or shell metacharacters")
    if output.exists() and not output.is_symlink():
        raise BuildError("Existing Flite directory preserved; choose a new selection path")
    for tool in ("gcc", "make", "ldd"):
        if not shutil.which(tool, path=environment()["PATH"]):
            raise BuildError(f"Missing prerequisite: {tool}")
    output.parent.mkdir(parents=True, exist_ok=True)
    builds = output.parent / "flite-builds"
    if builds.is_symlink():
        raise BuildError("Build directory must not be a symlink")
    builds.mkdir(exist_ok=True)
    recipe = {"manifest": manifest, "configure": CONFIGURE, "make": MAKE, "jobs": jobs,
              "builder_sha256": digest(__file__), "support": {n: digest(SUPPORT / n) for n in ("download.py", "bundle.py")}}
    identity = hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()[:24]
    prefix = builds / identity
    with (builds / ".build.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        receipt_path = prefix / "build.json"
        if prefix.exists():
            if prefix.is_symlink():
                raise BuildError("Build generation must not be a symlink")
            if not receipt_path.is_file():
                if output.is_symlink() and output.resolve() == prefix:
                    raise BuildError("Selected build has lost its receipt; refusing replacement")
                # Keep failed logs/sources while allowing the download cache to
                # resume on retry. Never rename the currently selected build.
                prefix.rename(builds / f"{identity}.failed-{time.time_ns()}")
        if prefix.exists():
            receipt = json.loads(receipt_path.read_text())
            if receipt.get("recipe") != recipe or receipt.get("payload") != payload(prefix):
                raise BuildError("Existing build payload/configuration changed; refusing reuse")
            write_json(prefix / "last-verification.json", verify(prefix))
            activate(prefix, output)
            return prefix
        prefix.mkdir()
        for name in ("archives", "sources", "licenses", "rebuild", "bin"):
            (prefix / name).mkdir()
        # Shared downloader preserves interrupted .part files and rechecks cached bytes.
        archive = download(manifest["url"], manifest["sha256"], Path(cache) / (manifest["sha256"] + ".tar.gz"), max_bytes=32 * 1024**2)
        shutil.copyfile(archive, prefix / "archives/flite.tar.gz")
        source = prefix / "sources/flite"
        extract(archive, source, manifest["directory"])
        for name in ("COPYING", "ACKNOWLEDGEMENTS"):
            shutil.copyfile(source / name, prefix / "licenses" / name)
        # Keep voice notices/data provenance verbatim in addition to complete sources.
        shutil.copytree(source / manifest["voice_directory"], prefix / "licenses/cmu_us_slt")
        shutil.copyfile(__file__, prefix / "rebuild/build-flite.py")
        shutil.copyfile(manifest_path, prefix / "rebuild/flite-sources.json")
        for name in ("download.py", "bundle.py"):
            shutil.copyfile(SUPPORT / name, prefix / "rebuild" / name)
        commands = []
        with (prefix / "build.log").open("w") as log:
            commands.append(run(["./configure", *CONFIGURE], source, log))
            commands.append(run(["make", f"-j{jobs}", *MAKE], source, log))
        shutil.copyfile(source / "bin/flite", prefix / "bin/flite")
        (prefix / "bin/flite").chmod(0o755)
        verification = verify(prefix)
        receipt = {"schema_version": 1, "identity": identity, "recipe": recipe, "commands": commands,
                   "platform": platform.platform(), "compiler": subprocess.check_output(["gcc", "--version"], env=environment(), text=True),
                   "verification": verification, "payload": payload(prefix)}
        write_json(receipt_path, receipt)
        activate(prefix, output)
        return prefix


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("flite-sources.json"))
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache/live-vision-cosmos-demo/flite")
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args(argv)
    try:
        prefix = build(args.output, args.manifest, args.cache, args.jobs)
    except (BuildError, OSError, ValueError, KeyError, TypeError, tarfile.TarError, subprocess.SubprocessError) as error:
        parser.exit(2, f"Flite build failed: {error}\n")
    print(f"Flite SLT verified and selected: {prefix}")


if __name__ == "__main__":
    main()
