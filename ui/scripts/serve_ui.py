#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Serve the camera UI and proxy only the loopback Edge-LLM API.

No model, cloud fallback, downloaded dependency or synthetic inference lives here.
Run on the Jetson alongside the Cosmos3-Edge shim (shim/cosmos3_shim_v1.py).

It also relays the Reachy Mini bridge (reachy/reachy_mjpeg_bridge.py) under /reachy/.
The bridge listens on loopback only, and a same-origin relay is what lets the page draw the
robot's frames into a canvas for inference without tainting it.
Those routes need this process's token, which only the page itself can read (/api/access),
and a Host that DNS rebinding cannot fake: see RELAY_TOKEN and host_allowed().
"""

import argparse
import base64
from datetime import datetime, timezone
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine_backends import ServiceEngineSwitcher
from reachy_setup import ReachySetup

ROOT = Path(__file__).resolve().parents[1] / "web"
# reachy/reachy.py: the robot daemon client. Path-inserted rather than vendored so there is one
# copy, not two that can drift.
# Import is best-effort: motor/app/volume control is optional (--reachy-daemon-url, empty by
# default) and reachy.py's only dependency is `requests` - not installed everywhere this UI's own
# stdlib-only camera/mic proxy already runs. A missing `requests` should not crash the whole UI
# over a feature nobody asked for; main() refuses --reachy-daemon-url instead, at the point where
# the user actually asked for it.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "reachy"))
try:
    from reachy import Reachy
except ImportError:
    Reachy = None
MAX_BODY = 2 * 1024 * 1024
MAX_RESPONSE = 1024 * 1024
MAX_SECONDS = 120
GENERATION_LOCK = threading.Lock()
GENERATION_WAIT_LOCK = threading.Lock()
HANDOFF_SECONDS = 0.75

# Reachy bridge routes: page path -> (bridge path, the only Content-Type accepted from it).
REACHY_FETCHES = {"/reachy/healthz": ("/healthz", "application/json"),
                  "/reachy/still.jpg": ("/still.jpg", "image/jpeg")}
REACHY_STREAMS = {"/reachy/mjpeg": ("/mjpeg", "multipart/x-mixed-replace"),
                  "/reachy/audio.mp3": ("/audio.mp3", "audio/mpeg")}
REACHY_SECONDS = 3
# Each open stream pins a handler thread (and a disconnect watcher) for as long as a tab
# watches or listens, in a process with a listen queue of 8 (request_queue_size). The cap is
# per process, so the HTTP and HTTPS listeners share it, and inference never waits on it.
MAX_REACHY_STREAMS = 4
REACHY_STREAM_SLOTS = threading.BoundedSemaphore(MAX_REACHY_STREAMS)
# Replacing an <img> src closes the old stream and opens the new one at once, but the old
# stream's watcher needs up to 0.2 s to notice and hand its slot back. A full cap waits this
# long for that handoff before it refuses, so a tab reopening its own stream is not turned away.
STREAM_HANDOFF_SECONDS = 0.5
# The bridge writes nothing while it has no new frame or no microphone audio. A stream silent
# this long gives its slot back; the page reopens it when the bridge reports live again.
STREAM_IDLE_SECONDS = 20
STREAM_CHUNK = 16384
# Open streams by the page's ?stream= token -> [monotonic time of the last byte relayed].
# An MJPEG <img> tells the page nothing when its stream ends cleanly (a restart of this
# server, say) and keeps showing the last frame, so the page asks here instead: each
# /reachy/healthz?stream=<token> answer carries X-Reachy-Stream: closed | idle_ms=<n>.
RELAYED_STREAMS = {}
RELAYED_STREAMS_LOCK = threading.Lock()
STREAM_TOKEN = re.compile(r"[A-Za-z0-9_-]{8,64}")
# Streams holding a slot, for X-Reachy-Slots on /reachy/healthz. The semaphore cannot be asked
# how many it has given out, so this is counted beside it, under RELAYED_STREAMS_LOCK: taken
# after the slot, given up before it, so it never shows more open than there really are.
OPEN_REACHY_STREAMS = 0
# The relay's key, new in every process. The robot's camera and microphone must not be open to
# every page a LAN browser visits: an <img> or <audio> on another site carries no Origin, and a
# DNS-rebound name looks same-origin to the browser. /api/access hands this to the page, which
# only a same-origin page can read, and every /reachy/ URL must carry it as ?token=. This stops
# drive-by pages, not a determined LAN attacker.
RELAY_TOKEN = secrets.token_urlsafe(24)
HOSTNAME = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?\.?")


class DeviceTelemetry:
    """One cheap, shared sampler; HTTP requests never trigger hardware probes.

    CPU is aggregate busy time across all cores (0–100%, not per-core summed),
    correct at any read cadence since /proc/stat's counters are cumulative and
    only the delta between reads matters. GPU is not: Orin's devfreq load node
    (thousandths) is an instantaneous reading, not a counter, and inference is
    bursty relative to a 1-per-second read of it - a single sample can easily
    land in the idle gap between bursts and read near zero right as the GPU
    was busy, making a live demo look like the GPU isn't doing anything. A
    second thread samples that node every gpu_sample_interval (much faster
    than the publish interval) and collect() reports the peak seen since the
    previous publish, not a single point-in-time read. Memory is system RAM
    unavailable, including CPU/GPU/OS use, not a CUDA allocator or dedicated
    VRAM counter.
    """

    GPU_LOAD_PATHS = (
        "/sys/devices/platform/bus@0/17000000.gpu/load",
        "/sys/devices/platform/17000000.gpu/load",
        "/sys/class/devfreq/17000000.gpu/device/load",
    )

    def __init__(self, proc_root=Path("/proc"), gpu_paths=None, interval=0.25, gpu_sample_interval=0.05):
        self.proc_root = Path(proc_root)
        self.gpu_paths = tuple(Path(p) for p in (self.GPU_LOAD_PATHS if gpu_paths is None else gpu_paths))
        self.interval = interval
        self.gpu_sample_interval = gpu_sample_interval
        self.previous_cpu = None
        self.lock = threading.Lock()
        self.gpu_lock = threading.Lock()
        self.gpu_peak = None
        self.stopped = threading.Event()
        self.thread = None
        self.gpu_thread = None
        self.sample_time = None
        self.sample = self.empty_sample()

    @staticmethod
    def empty_sample():
        return {"sampled_at": None, "status": "unavailable",
                "cpu": {"utilization_percent": None},
                "gpu": {"utilization_percent": None},
                "memory": {"used_bytes": None, "total_bytes": None,
                           "utilization_percent": None, "shared": True,
                           "measurement": "MemTotal - MemAvailable"}}

    @staticmethod
    def read_small(path):
        with path.open(encoding="ascii") as source:
            return source.read(8192)

    def collect(self):
        sample = self.empty_sample()
        try:
            fields = self.read_small(self.proc_root / "stat").splitlines()[0].split()
            if fields[0] != "cpu" or len(fields) < 9:
                raise ValueError("Missing aggregate CPU counters")
            # guest/guest_nice are already included in user/nice: do not add them.
            counters = tuple(int(value) for value in fields[1:9])
            if any(value < 0 for value in counters):
                raise ValueError("Negative CPU counter")
            previous, self.previous_cpu = self.previous_cpu, counters
            if previous is not None:
                delta = [now - before for now, before in zip(counters, previous)]
                total = sum(delta)
                if total > 0 and min(delta) >= 0:
                    idle = delta[3] + delta[4]
                    sample["cpu"]["utilization_percent"] = round(100 * (total - idle) / total, 1)
        except (OSError, ValueError, IndexError, UnicodeError):
            self.previous_cpu = None

        with self.gpu_lock:
            load, self.gpu_peak = self.gpu_peak, None
        if load is not None:
            sample["gpu"]["utilization_percent"] = load / 10

        try:
            values = {}
            for line in self.read_small(self.proc_root / "meminfo").splitlines():
                fields = line.split()
                if fields and fields[0] in {"MemTotal:", "MemAvailable:"}:
                    if len(fields) != 3 or fields[2] != "kB":
                        raise ValueError("Unexpected memory units")
                    values[fields[0]] = int(fields[1]) * 1024
            total, available = values["MemTotal:"], values["MemAvailable:"]
            if not 0 <= available <= total or total <= 0:
                raise ValueError("Invalid memory counters")
            sample["memory"].update(used_bytes=total - available, total_bytes=total,
                                    utilization_percent=round(100 * (total - available) / total, 1))
        except (OSError, ValueError, KeyError, UnicodeError):
            pass

        present = [sample[name]["utilization_percent"] is not None for name in ("cpu", "gpu", "memory")]
        sample["status"] = "ok" if all(present) else "partial" if any(present) else "unavailable"
        sample["sampled_at"] = datetime.now(timezone.utc).isoformat()
        with self.lock:
            self.sample, self.sample_time = sample, time.monotonic()

    def sample_gpu_peak(self):
        """Take one fast, cheap GPU load reading and fold it into the running peak.

        Runs on its own thread at gpu_sample_interval, much faster than the publish
        interval, so collect() has several readings to take the peak of rather than
        one point-in-time snapshot that can miss a burst entirely.
        """
        for path in self.gpu_paths:
            try:
                load = int(self.read_small(path).strip())
                if not 0 <= load <= 1000:
                    raise ValueError("GPU load outside permille range")
            except (OSError, ValueError, UnicodeError):
                continue
            with self.gpu_lock:
                self.gpu_peak = load if self.gpu_peak is None else max(self.gpu_peak, load)
            return

    def snapshot(self):
        with self.lock:
            # Nested data is never mutated after publication by the sampler.
            return {**self.sample, "age_ms": None if self.sample_time is None else
                    round(max(0, time.monotonic() - self.sample_time) * 1000),
                    "sample_interval_ms": round(self.interval * 1000)}

    def start(self):
        def run():
            while not self.stopped.is_set():
                self.collect()
                if self.stopped.wait(self.interval):
                    break
        def run_gpu():
            while not self.stopped.is_set():
                self.sample_gpu_peak()
                if self.stopped.wait(self.gpu_sample_interval):
                    break
        self.thread = threading.Thread(target=run, name="device-telemetry", daemon=True)
        self.gpu_thread = threading.Thread(target=run_gpu, name="device-telemetry-gpu", daemon=True)
        self.thread.start()
        self.gpu_thread.start()
        return self

    def close(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=2)
        if self.gpu_thread:
            self.gpu_thread.join(timeout=2)


def validate_request(payload):
    """Accept one bounded image and prompt, with no URLs or conversation history."""
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object.")
    allowed = {"model", "messages", "stream", "temperature", "max_tokens", "top_p", "stream_options",
               "max_image_tokens_per_image"}
    if "seed" in payload:
        raise ValueError("Upstream TensorRT-Edge-LLM does not support seed. Use temperature 0.")
    if set(payload) - allowed or payload.get("stream") is not True:
        raise ValueError("Only the supported streaming image request is accepted.")
    if not isinstance(payload.get("model"), str) or not 1 <= len(payload["model"]) <= 256:
        raise ValueError("A model name is required.")
    tokens = payload.get("max_tokens")
    if type(tokens) is not int or not 1 <= tokens <= 512:
        raise ValueError("max_tokens must be from 1 to 512.")
    if type(payload.get("temperature")) not in (int, float) or payload["temperature"] not in (0, 0.7):
        raise ValueError("Use temperature 0 (Lightweight) or 0.7 (Live VLM WebUI).")
    if "top_p" in payload and (type(payload["top_p"]) not in (int, float)
            or not math.isfinite(payload["top_p"]) or not 0 < payload["top_p"] <= 1):
        raise ValueError("top_p must be greater than 0 and at most 1.")
    if "max_image_tokens_per_image" in payload and (type(payload["max_image_tokens_per_image"]) is not int
            or not 4 <= payload["max_image_tokens_per_image"] <= 512):
        raise ValueError("Image token budget must be an integer from 4 to 512.")
    if "stream_options" in payload:
        options = payload["stream_options"]
        if not isinstance(options, dict) or set(options) != {"include_usage"} or options["include_usage"] is not True:
            raise ValueError("Only stream_options.include_usage=true is supported.")
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        raise ValueError("Send exactly one user message; history is disabled.")
    message = messages[0]
    if not isinstance(message, dict) or set(message) != {"role", "content"} or message["role"] != "user":
        raise ValueError("A single user message is required.")
    content = message["content"]
    if not isinstance(content, list) or len(content) != 2 or not all(isinstance(x, dict) for x in content):
        raise ValueError("Send one text prompt and one JPEG image.")
    texts = [x for x in content if x.get("type") == "text"]
    images = [x for x in content if x.get("type") == "image_url"]
    if len(texts) != 1 or len(images) != 1:
        raise ValueError("Send one text prompt and one JPEG image.")
    text = texts[0].get("text")
    if set(texts[0]) != {"type", "text"} or not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
        raise ValueError("Prompt must contain 1–2000 characters.")
    image = images[0].get("image_url")
    if set(images[0]) != {"type", "image_url"} or not isinstance(image, dict) or set(image) != {"url"}:
        raise ValueError("Only an embedded JPEG is accepted.")
    url = image["url"]
    prefix = "data:image/jpeg;base64,"
    if not isinstance(url, str) or not url.startswith(prefix):
        raise ValueError("External image URLs are disabled. Send an embedded JPEG.")
    raw = base64.b64decode(url[len(prefix):], validate=True)
    if not raw.startswith(b"\xff\xd8\xff") or len(raw) > MAX_BODY * 3 // 4:
        raise ValueError("Invalid or oversized JPEG image.")


# --------------------------------------------------------------------------- service inventory
# The "which services are up, how much RAM and disk do they use, SD card or NVMe" panel. Every
# service here besides this UI process itself and the Piper subprocess (which this process starts
# and already holds a handle to) is a *separate* process this UI does not manage - found by
# scanning /proc for a matching command line, the only channel available without adding an agent
# or a dependency on each service exposing its own metrics endpoint.

def read_proc_rss_mb(pid):
    """Resident memory for one PID, or None if it has already exited or /proc denies us."""
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        return None
    return None


def find_pid_by_cmdline(substring):
    """First PID whose command line contains substring, or None. O(processes), fine at UI poll
    rates (every few seconds) on a single-board machine with a few dozen processes."""
    try:
        proc_root = Path("/proc")
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = entry.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            except OSError:
                continue
            if substring in cmdline:
                return int(entry.name)
    except OSError:
        pass
    return None


def find_pid_by_systemd_unit(unit):
    """MainPID of a systemd unit, or None if it is not running. Unlike find_pid_by_cmdline, this
    needs no assumption about a service's argv shape - the engine registry already names the unit
    (ServiceEngineSwitcher.command uses the same name to start/stop it), so this is the reliable
    way to report a managed engine's own process state without duplicating that name elsewhere."""
    try:
        result = subprocess.run(["systemctl", "show", unit, "-p", "MainPID", "--value"],
                                capture_output=True, text=True, timeout=3)
        pid = int(result.stdout.strip())
        return pid if result.returncode == 0 and pid > 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def disk_for_path(path):
    """'SD card', 'NVMe', or the raw block device name, for whichever filesystem contains path -
    the longest-prefix-matching entry in /proc/mounts, same algorithm findmnt uses."""
    try:
        target = os.path.realpath(str(path))
    except OSError:
        return "unknown"
    best_mount, best_device = "", ""
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue
                device, mount_point = parts[0], parts[1].replace("\\040", " ")
                if (target == mount_point or target.startswith(mount_point.rstrip("/") + "/")
                        or mount_point == "/") and len(mount_point) >= len(best_mount):
                    best_mount, best_device = mount_point, device
    except OSError:
        return "unknown"
    if "mmcblk" in best_device:
        return "SD card"
    if "nvme" in best_device:
        return "NVMe"
    return best_device.rsplit("/", 1)[-1] or "unknown"


_dir_size_cache = {}


def dir_size_mb(path):
    """Total on-disk size under path, in MB. Cached per path: these are static build artifacts
    (engine files, a voice model) that do not change size during a UI process's lifetime, and a
    multi-GB engine directory is too slow to re-walk on every poll."""
    key = str(path)
    if key in _dir_size_cache:
        return _dir_size_cache[key]
    total = 0
    try:
        p = Path(path)
        if p.is_file():
            total = p.stat().st_size
        else:
            for entry in p.rglob("*"):
                try:
                    if entry.is_file():
                        total += entry.stat().st_size
                except OSError:
                    continue
    except OSError:
        _dir_size_cache[key] = None
        return None
    result = round(total / (1024 * 1024), 1)
    _dir_size_cache[key] = result
    return result


def parse_host(host_header):
    """(host, IP address or None) named by a Host header, which may carry a port."""
    if not isinstance(host_header, str) or not host_header or any(
            ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in host_header):
        raise ValueError("Invalid Host header.")
    try:
        parsed = urlsplit("//" + host_header)
        host = parsed.hostname
        parsed.port  # Validate the caller's port before anything relies on the authority.
        if not host or parsed.username is not None or parsed.password is not None or \
                parsed.path or parsed.query or parsed.fragment:
            raise ValueError("Invalid host authority")
        try:
            return host, ipaddress.ip_address(host)
        except ValueError:
            if len(host) > 253 or not HOSTNAME.fullmatch(host):
                raise ValueError("Invalid hostname")
            return host, None
    except ValueError as exc:
        raise ValueError("Invalid Host header.") from exc


def is_localhost_name(host):
    name = host.rstrip(".").lower()
    return name == "localhost" or name.endswith(".localhost")


def camera_redirect_url(host_header, https_port):
    """Upgrade the same requested LAN host, preserving secure-context localhost.

    Never use the peer address: a forwarded browser request can arrive over a
    loopback connection while its actual document origin is still a LAN host.
    """
    if not https_port:
        return None
    host, address = parse_host(host_header)
    if address is None:
        if is_localhost_name(host):
            return None
        authority = host
    else:
        if address.is_loopback:
            return None
        authority = f"[{host}]" if address.version == 6 else host
    return f"https://{authority}:{https_port}/"


def host_allowed(host_header, allowed_hosts=frozenset()):
    """Whether the browser asked for this server by a name that cannot be DNS-rebound.

    Rebinding points an attacker's own name at this address: the browser then treats the
    attacker's page as same-origin here, sends Sec-Fetch-Site: same-origin, and would let it
    read /api/access. The one thing it cannot change is the name it asked for, in Host. An IP
    literal or localhost resolves nowhere else; any other name must be one the operator listed.
    """
    try:
        host, address = parse_host(host_header)
    except ValueError:
        return False
    return address is not None or is_localhost_name(host) or host.rstrip(".").lower() in allowed_hosts


def allowed_host_name(value):
    """An --allowed-host value: one DNS name, no scheme or port, as Host will be compared."""
    name = value.strip().rstrip(".").lower()
    if not name or len(name) > 253 or not HOSTNAME.fullmatch(name):
        raise ValueError(f"--allowed-host takes a bare host name such as orin.local, not {value!r}")
    return name


def bridge_address(url):
    """(host, port) of the Reachy bridge. Plain HTTP only: it never leaves the Jetson."""
    message = "--reachy-url must look like http://127.0.0.1:8099"
    try:
        parsed = urlsplit(url)
        port = parsed.port or 80
    except ValueError as exc:
        raise ValueError(message) from exc
    if parsed.scheme != "http" or not parsed.hostname or parsed.username is not None or \
            parsed.password is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(message)
    return parsed.hostname, port


def media_type(response):
    return response.getheader("Content-Type", "").split(";")[0].strip().lower()


class EngineSwitcher:
    """Swap the TensorRT engine the local shim serves, and restart it to load the swap.

    The recipe: an atomic symlink swap the shim's own launch config always reads the same path
    from, then a systemctl restart, then poll the shim's own /v1/models until it answers again.
    Requires passwordless sudo for exactly `ln -sfn` on --engine-link and `systemctl restart` on
    --shim-service, which this class does not configure: a sudoers line like `USER ALL=(root)
    NOPASSWD: /usr/bin/ln -sfn * <engine-link>, /usr/bin/systemctl restart <shim-service>`.
    ServiceEngineSwitcher, the "kind": "service" registry setup-orin.sh writes, needs only the
    start/stop rule it installs instead.
    """

    switch_timeout = 120

    def __init__(self, engine_link: Path, engines: dict, shim_service: str, backend_port: int):
        self.engine_link = engine_link
        self.engines = engines
        self.shim_service = shim_service
        self.backend_port = backend_port
        self.lock = threading.Lock()
        self.switching = False
        self.switch_target = None
        self.switch_started_at = None

    def _run(self, cmd, timeout=180):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.returncode == 0, (r.stderr or r.stdout)[-400:]
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def active(self) -> dict:
        try:
            target = os.path.realpath(self.engine_link)
        except OSError:
            target = ""
        for eid, e in self.engines.items():
            if os.path.realpath(e["path"]) == target:
                return {"id": eid, **e}
        return {"id": "unknown", "name": "Unknown", "path": target,
               "profile": "", "notes": "active engine does not match any configured build"}

    def switch(self, eid: str) -> tuple[bool, str]:
        if not self.lock.acquire(blocking=False):
            return False, "another engine switch is already in progress"
        # Set before the restart/poll below, which can take up to switch_timeout seconds - a
        # concurrent GET /api/engines from another tab must see this, not just the caller of
        # switch() once the synchronous call finally returns.
        self.switching = True
        self.switch_target = eid
        self.switch_started_at = time.time()
        try:
            return self._switch(eid)
        finally:
            self.switching = False
            self.switch_target = None
            self.switch_started_at = None
            self.lock.release()

    def _switch(self, eid: str) -> tuple[bool, str]:
        if eid not in self.engines:
            return False, f"unknown engine {eid}"
        path = self.engines[eid]["path"]
        if not os.path.isfile(os.path.join(path, "llm.engine")):
            return False, f"engine not built at {path}"
        ok, msg = self._run(["sudo", "-n", "ln", "-sfn", path, str(self.engine_link)])
        if not ok:
            return False, f"symlink failed: {msg}"
        ok, msg = self._run(["sudo", "-n", "systemctl", "restart", self.shim_service])
        if not ok:
            return False, f"restart failed: {msg}"
        for _ in range(60):
            conn = http.client.HTTPConnection("127.0.0.1", self.backend_port, timeout=2)
            try:
                conn.request("GET", "/v1/models")
                if conn.getresponse().status == 200:
                    return True, f"switched to {self.engines[eid]['name']}"
            except (OSError, http.client.HTTPException):
                pass
            finally:
                conn.close()
            time.sleep(2)
        return False, "engine swapped but the shim did not become ready in 120s"


SPEECH_RATES = (0.5, 0.8, 1.0, 1.25, 1.5, 2.0, 2.5)


class Piper:
    """Local TTS via a persistent Piper process, one JSON line per utterance on stdin.

    A fresh process per utterance would pay Piper's ~1-3 s cold model load every time, which alone
    misses any reasonable speak-after-caption latency target. Keeping one process warm (--json-input
    mode) means only the first call after startup pays that cost.
    """

    def __init__(self, binary: Path, model: Path, out_dir: Path):
        self.binary = binary
        self.model = model
        self.out_dir = out_dir
        self.proc = None
        self.lock = threading.Lock()

    def _start(self, reason: str = "startup"):
        # Piper's own stderr used to go to DEVNULL, so a process that died left no trace at all -
        # the next speak request would silently pay a full cold restart (this is what "Reachy
        # went quiet for a while, then was fine" reports usually are) with nothing in the journal
        # explaining why the previous process was gone. Now captured to a small ring buffer and
        # printed on exit, and the restart itself is logged with the dead process's exit code -
        # negative means killed by a signal (-9 is almost always the OOM killer on this board).
        self.out_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, LD_LIBRARY_PATH=str(self.binary.parent))
        print(f"[piper] starting ({reason})", flush=True)
        self.proc = subprocess.Popen(
            [str(self.binary), "-m", str(self.model), "--json-input", "--length_scale", "0.85", "-q"],
            cwd=str(self.out_dir), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, env=env, text=True)
        threading.Thread(target=self._drain_stderr, args=(self.proc,), daemon=True).start()

    def _drain_stderr(self, proc):
        tail = []
        try:
            for line in proc.stderr:
                tail.append(line.rstrip("\n"))
                tail[:] = tail[-20:]
        except (OSError, ValueError):
            pass
        code = proc.poll()
        if code in (None, 0):
            return
        detail = ("; last output:\n  " + "\n  ".join(tail)) if tail else "; no output"
        print(f"[piper] process exited (code={code}){detail}", flush=True)

    def close(self):
        """Release the optional voice worker when the robot is disconnected."""
        with self.lock:
            proc, self.proc = self.proc, None
            if proc is None:
                return
            if proc.poll() is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            for stream in (proc.stdin, proc.stderr):
                if stream:
                    stream.close()

    def synth(self, text: str, timeout: float = 8.0) -> Path | None:
        """Synthesize text to a WAV file. Thread-safe (holds self.lock for the whole call)."""
        with self.lock:
            if self.proc is None:
                self._start()
            elif self.proc.poll() is not None:
                self._start(reason=f"previous process exited with code {self.proc.poll()}")
            name = f"{uuid.uuid4().hex}.wav"
            out = self.out_dir / name
            line = json.dumps({"text": text, "output_file": name}) + "\n"
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._start(reason=f"broken pipe on write ({exc})")
                try:
                    self.proc.stdin.write(line)
                    self.proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    return None
            # Two consecutive equal, non-empty size readings is "done writing" - avoids reading a
            # half-written file without an fsync/rename dance for something that lives seconds.
            deadline = time.monotonic() + timeout
            last_size, stable = -1, 0
            while time.monotonic() < deadline:
                if out.exists():
                    size = out.stat().st_size
                    if size > 44 and size == last_size:
                        stable += 1
                        if stable >= 2:
                            return out
                    else:
                        stable = 0
                    last_size = size
                time.sleep(0.015)
            return None

    def retime(self, wav: Path, rate: float) -> Path:
        """Speed up or slow down an already-synthesized WAV with sox's tempo effect (time-stretch,
        pitch-preserving - unlike a naive resample, which would also shift pitch). Piper's own
        --length_scale only takes effect at process start (confirmed: a --json-input line's own
        length_scale field is silently ignored by this build), so honouring a per-request rate
        without restarting the warm process - and paying its cold-load cost, the whole reason it
        stays warm - means adjusting the output afterward instead. Falls back to the untouched
        file on any sox failure; a wrong rate is a much smaller problem than no speech at all."""
        if rate == 1.0:
            return wav
        out = wav.with_name(f"{wav.stem}-x{rate}.wav")
        try:
            result = subprocess.run(["sox", str(wav), str(out), "tempo", str(rate)],
                                    capture_output=True, timeout=10)
            if result.returncode == 0 and out.is_file():
                wav.unlink(missing_ok=True)
                return out
        except (OSError, subprocess.SubprocessError):
            pass
        return wav

    @staticmethod
    def spoken_summary(text: str, max_chars: int = 500) -> str:
        """First sentence, spoken in full. A caption that runs long before its first period used
        to get cut off mid-word at 140 characters ("...with a Game Boy-style |console partially
        visible..." spoken as "...Game Boy-style" - reported live) - a longer utterance is a much
        smaller problem than an answer that stops mid-sentence, so a detected sentence end is
        never truncated. max_chars only guards the pathological case of no sentence-ending
        punctuation at all, where it still breaks at the last complete word, not mid-word."""
        m = re.search(r"[.!?](\s|$)", text)
        if m:
            return text[:m.end()].strip()
        s = text.strip()
        if len(s) > max_chars:
            s = s[:max_chars].rsplit(" ", 1)[0].rstrip(",;:") + "…"
        return s


class ReachyRuntime:
    """Shared HTTP/HTTPS robot selection; changing it never touches inference."""

    def __init__(self, setup, piper_factory=None, client_factory=None):
        self.setup = setup
        self.lock = threading.RLock()
        self.address = None
        self.client = None
        self.piper = None
        self.client_factory = client_factory or Reachy
        self.piper_factory = piper_factory

    def apply(self, address):
        with self.lock:
            if address == self.address:
                return
            if address and self.client_factory is None:
                raise RuntimeError("Reachy control needs python3-requests. Run setup-orin.sh --reachy-only.")
            client = self.client_factory(f"http://{address}:8000") if address else None
            piper = self.piper_factory() if address and self.piper_factory else None
            old_client, old_piper = self.client, self.piper
            self.client, self.piper, self.address = client, piper, address
            if old_piper:
                old_piper.close()
            if old_client and getattr(old_client, "session", None):
                old_client.session.close()

    def refresh(self):
        # CLI changes and UI changes use the same device-local config file.
        snapshot = self.setup.snapshot()
        if not snapshot.get("busy"):
            address = snapshot.get("address") or ""
            # Optional robot dependencies must not prevent image inference.
            self.apply(address if self.client_factory is not None else "")

    def close(self):
        self.apply("")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, *args, telemetry=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.owns_telemetry = telemetry is None
        self.telemetry = telemetry if telemetry is not None else DeviceTelemetry().start()
        self.https_port = None
        # Names besides IP literals and localhost that /reachy/ and /api/access answer to.
        self.allowed_hosts = frozenset()
        # Populated in main(): {name: {"cmdline_match": str, "path": str}} for services this
        # process does not itself manage (the shim, the Reachy bridge) - see --services-config.
        self.services_config = {}
        self.reachy_setup = None
        self.reachy_runtime = None

    @property
    def reachy_client(self):
        return self.reachy_runtime.client if self.reachy_runtime else getattr(self, "_reachy_client", None)

    @reachy_client.setter
    def reachy_client(self, value):
        self._reachy_client = value

    @property
    def piper(self):
        return self.reachy_runtime.piper if self.reachy_runtime else getattr(self, "_piper", None)

    @piper.setter
    def piper(self, value):
        self._piper = value

    def server_close(self):
        if getattr(self, "owns_telemetry", False):
            self.telemetry.close()
        super().server_close()

    def service_list(self):
        """Live Vision itself and the Piper subprocess are known directly (this process started
        them); everything else in --services-config is a separate process, found by scanning
        /proc for a matching command line - see find_pid_by_cmdline. A registry of separate model
        services (ServiceEngineSwitcher) is reported from that registry directly rather than
        needing its own mirror entries in --services-config: --services-config and engines.json
        naming the same service under two different keys is exactly how a switchable model can
        show as permanently "down" once it stops being the selected one - see the "active" field,
        which tells the caller a stopped-and-unselected engine apart from a stopped one that
        should be running."""
        if self.reachy_runtime:
            self.reachy_runtime.refresh()
        out = [{"name": "Live Vision UI", "running": True,
               "memory_mb": read_proc_rss_mb(os.getpid()),
               "storage_mb": dir_size_mb(Path(__file__).resolve().parent),
               "disk": disk_for_path(Path(__file__).resolve())}]
        # Keep installed, lazy-start speech visible even when no robot is selected.
        piper = self.piper or getattr(self, "_piper", None)
        if piper:
            pid = piper.proc.pid if piper.proc and piper.proc.poll() is None else None
            out.append({"name": "TTS (Piper)", "running": pid is not None,
                       "optional": True,
                       "state_detail": "" if pid else ("Idle · starts on speech" if self.reachy_client else "Idle · connect Reachy Mini"),
                       "memory_mb": read_proc_rss_mb(pid) if pid else None,
                       "storage_mb": dir_size_mb(piper.model.parent),
                       "disk": disk_for_path(piper.model)})
        for name, spec in self.services_config.items():
            pid = find_pid_by_cmdline(spec["cmdline_match"])
            bridge = "reachy_mjpeg_bridge.py" in spec["cmdline_match"]
            out.append({"name": name, "running": pid is not None,
                       "optional": bridge,
                       "state_detail": ("Waiting for robot camera" if self.reachy_client else "Not configured") if bridge and not pid else "",
                       "memory_mb": read_proc_rss_mb(pid) if pid else None,
                       "storage_mb": dir_size_mb(spec["path"]) if spec.get("path") else None,
                       "disk": disk_for_path(spec["path"]) if spec.get("path") else "unknown"})
        switcher = self.engine_switcher
        if switcher is not None and getattr(switcher, "managed", False):
            active_id = switcher.active()["id"]
            for eid, entry in switcher.engines.items():
                pid = find_pid_by_systemd_unit(entry["service"])
                out.append({"name": entry["name"], "running": pid is not None,
                           "memory_mb": read_proc_rss_mb(pid) if pid else None,
                           "storage_mb": dir_size_mb(entry["path"]) if entry.get("path") else None,
                           "disk": disk_for_path(entry["path"]) if entry.get("path") else "unknown",
                           "active": eid == active_id})
        return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_request(self, code="-", size="-"):
        # Polling is routine; do not write a system journal line every second or two.
        if self.path.partition("?")[0] in {"/api/metrics", "/reachy/healthz"} and code == 200:
            return
        # Nor the relay token that every /reachy/ URL carries: more can read the journal than this page.
        self.log_message('"%s" %s %s', re.sub(r"([?&]token=)[^&\s]*", r"\1<redacted>", self.requestline),
                         str(getattr(code, "value", code)), str(size))

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def send_headers(self, code, content_type, length=None, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Connection", "close")
        if length is not None:
            self.send_header("Content-Length", str(length))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.close_connection = True

    def json_error(self, code, message):
        body = json.dumps({"error": {"message": message}}).encode()
        self.send_headers(code, "application/json", len(body))
        self.wfile.write(body)

    def origin_allowed(self):
        origin = self.headers_in.get("Origin")
        if not origin:
            return True
        try:
            parsed = urlsplit(origin)
            return parsed.scheme in {"http", "https"} and parsed.netloc == self.headers_in.get("Host")
        except ValueError:
            return False

    def fetch_site_allowed(self):
        # An <img> or <audio> request carries no Origin header, so origin_allowed() alone cannot
        # stop another site embedding the robot's camera or microphone and using up its stream
        # slots. Browsers send Fetch Metadata over HTTPS: only this page, or a typed URL, passes.
        # Plain HTTP gets no Fetch Metadata from most browsers, which is why the token exists.
        return self.headers_in.get("Sec-Fetch-Site", "same-origin") in {"same-origin", "none"}

    def host_refused(self):
        """Answer 421 unless Host names this server in a way DNS rebinding cannot fake."""
        if host_allowed(self.headers_in.get("Host"), self.server.allowed_hosts):
            return False
        self.json_error(421, "Live Vision does not answer to this host name. Open it by IP address, "
                             "or start it with --allowed-host for this name.")
        return True

    @staticmethod
    def relay_token_valid(query):
        supplied = parse_qs(query).get("token") or []
        # Bytes: compare_digest refuses str with non-ASCII characters, which a query can carry.
        return len(supplied) == 1 and secrets.compare_digest(supplied[0].encode(), RELAY_TOKEN.encode())

    def do_GET(self):
        self.headers_in = self.headers
        if not self.origin_allowed():
            self.json_error(403, "Cross-origin requests are disabled.")
            return
        if self.path == "/" and not isinstance(self.connection, ssl.SSLSocket):
            try:
                target = camera_redirect_url(self.headers.get("Host"), self.server.https_port)
            except ValueError as exc:
                self.json_error(400, str(exc))
                return
            if target:
                self.send_headers(302, "text/plain; charset=utf-8", 0, {"Location": target})
                return
        if self.path in {"/health/ready", "/v1/models", "/api/runtime"}:
            self.proxy(self.path)
            return
        # The page makes every stream URL it opens unique, so a reopened stream is really
        # fetched again; the video's ?stream= token also names it for X-Reachy-Stream.
        # Every route also needs the relay's ?token= (RELAY_TOKEN), checked after Host and Fetch
        # Metadata so those refusals keep saying what is wrong. Queries never reach the bridge.
        route, _, query = self.path.partition("?")
        if route == "/api/reachy/setup":
            if self.host_refused():
                return
            if not self.fetch_site_allowed():
                self.json_error(403, "Cross-site requests are disabled.")
                return
            self.reachy_setup_status()
            return
        if route in REACHY_FETCHES or route in REACHY_STREAMS:
            stream = (parse_qs(query).get("stream") or [""])[0]
            stream = stream if STREAM_TOKEN.fullmatch(stream) else None
            if self.host_refused():
                return
            if not self.fetch_site_allowed():
                self.json_error(403, "Cross-site requests are disabled.")
            elif not self.relay_token_valid(query):
                # Also what a page still open across a restart of this server gets: it asks
                # /api/access again for the new token.
                self.json_error(401, "Missing or out-of-date relay token. Reload Live Vision.")
            elif route in REACHY_STREAMS:
                self.reachy_stream(*REACHY_STREAMS[route], stream)
            else:
                self.reachy_fetch(*REACHY_FETCHES[route], stream)
            return
        if self.path == "/api/metrics":
            body = json.dumps(self.server.telemetry.snapshot()).encode()
            self.send_headers(200, "application/json", len(body))
            self.wfile.write(body)
            return
        if self.path == "/api/access":
            # The relay token goes only to a page that can read this answer: a cross-origin page
            # cannot, and a rebound name is refused here before it gets the chance.
            if self.host_refused():
                return
            body = json.dumps({"https_port": self.server.https_port, "reachy_token": RELAY_TOKEN}).encode()
            self.send_headers(200, "application/json", len(body))
            self.wfile.write(body)
            return
        if route == "/api/reachy/state":
            self.reachy_state()
            return
        if route == "/api/reachy/apps":
            self.reachy_apps()
            return
        if route == "/api/engines":
            switcher = self.server.engine_switcher
            if not switcher:
                body = json.dumps({"configured": False, "engines": []}).encode()
            elif getattr(switcher, "managed", False):
                body = json.dumps(switcher.status()).encode()
            else:
                available = [{"id": eid, **e,
                              "available": (Path(e["path"]) / "llm.engine").is_file(),
                              "reason": "" if (Path(e["path"]) / "llm.engine").is_file()
                              else "Engine has not been built on this device"}
                             for eid, e in switcher.engines.items()]
                progress = ({"target": switcher.switch_target, "started_at": switcher.switch_started_at,
                            "timeout": switcher.switch_timeout} if switcher.switching else None)
                body = json.dumps({"configured": True, "active": switcher.active(),
                                   "switching": switcher.switching, "switch_progress": progress,
                                   "engines": available}).encode()
            self.send_headers(200, "application/json", len(body))
            self.wfile.write(body)
            return
        if route == "/api/services":
            body = json.dumps({"services": self.server.service_list()}).encode()
            self.send_headers(200, "application/json", len(body))
            self.wfile.write(body)
            return
        # Only the bare page is upgraded to HTTPS above, for the camera. A link such as
        # /?source=reachy is served where it was asked for: the robot needs no secure
        # context, and a detour through a self-signed certificate would only add a warning.
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if route not in assets:
            self.json_error(404, "Not found.")
            return
        name, content_type = assets[route]
        data = (ROOT / name).read_bytes()
        self.send_headers(200, content_type, len(data))
        self.wfile.write(data)

    def do_POST(self):
        self.headers_in = self.headers
        if not self.origin_allowed():
            self.json_error(403, "Cross-origin requests are disabled.")
            return
        if self.path.startswith("/api/reachy/setup/"):
            self.reachy_setup_action(self.path[len("/api/reachy/setup/"):])
            return
        if self.path.startswith("/api/reachy/"):
            self.reachy_control(self.path[len("/api/reachy/"):])
            return
        if self.path.startswith("/api/engines/"):
            self.engine_switch(self.path[len("/api/engines/"):])
            return
        if self.path != "/v1/chat/completions":
            self.json_error(404, "Not found.")
            return
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Chunked uploads are disabled.")
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("Content-Type must be application/json.")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                self.json_error(413, "The image request must be smaller than 2 MiB.")
                return
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete request body.")
            payload = json.loads(body)
            validate_request(payload)
        except socket.timeout:
            self.json_error(408, "Request upload timed out.")
            return
        except (ValueError, TypeError, KeyError) as exc:
            self.json_error(400, str(exc))
            return
        if not self.acquire_generation():
            if self.client_disconnected():
                return
            self.json_error(429, "One inference is already active. Retry after it finishes.")
            return
        try:
            if self.client_disconnected():
                return
            switcher = getattr(self.server, "engine_switcher", None)
            if getattr(switcher, "managed", False):
                backend = switcher.backend()
                if switcher.switching or payload["model"] != backend["model_id"]:
                    self.json_error(409, "The model changed; refresh Live Vision before sending another image.")
                    return
            self.proxy(self.path, body)
        finally:
            GENERATION_LOCK.release()

    def client_disconnected(self):
        # The complete body has been consumed and responses close the connection;
        # EOF or unexpected pipelined bytes mean this request must not start work.
        try:
            return bool(select.select([self.connection], [], [], 0)[0])
        except (OSError, ValueError):
            return True

    def acquire_generation(self):
        if GENERATION_LOCK.acquire(blocking=False):
            return True
        # A browser abort settles before the 200 ms disconnect watcher releases
        # the active slot. Admit at most one short handoff waiter, not a backlog.
        if not GENERATION_WAIT_LOCK.acquire(blocking=False):
            return False
        try:
            deadline = time.monotonic() + HANDOFF_SECONDS
            while not self.client_disconnected():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if GENERATION_LOCK.acquire(timeout=min(0.05, remaining)):
                    return True
            return False
        finally:
            GENERATION_WAIT_LOCK.release()

    def proxy(self, path, body=None):
        switcher = getattr(self.server, "engine_switcher", None)
        managed = getattr(switcher, "managed", False)
        backend = switcher.backend() if managed else None
        if managed and switcher.switching:
            self.json_error(503, "Switching model services; inference is paused.")
            return
        if managed and not switcher.active()["available"]:
            self.json_error(503, switcher.active()["reason"] or "The selected model is not qualified on this device.")
            return
        connection = http.client.HTTPConnection("127.0.0.1", backend["backend_port"] if managed else self.server.backend_port,
                                               timeout=MAX_SECONDS if body else 3)
        finished = threading.Event()
        expired = threading.Event()
        stream_started = False
        response = None
        watcher = None
        try:
            connection.connect()
            upstream_socket = connection.sock
            if body:
                def watch_disconnect():
                    deadline = time.monotonic() + MAX_SECONDS
                    while not finished.wait(0.2):
                        if time.monotonic() >= deadline:
                            expired.set()
                            break
                        try:
                            ready, _, _ = select.select([self.connection], [], [], 0)
                            if ready:
                                # Requests do not pipeline on this close-after-response
                                # connection. EOF (or new unexpected bytes) cancels work.
                                self.connection.recv(1)
                                break
                        except (OSError, ValueError):
                            break
                    if not finished.is_set():
                        try:
                            upstream_socket.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                watcher = threading.Thread(target=watch_disconnect, daemon=True)
                watcher.start()
            connection.request("POST" if body else "GET", path, body=body,
                headers={"Content-Type": "application/json", "Accept": "text/event-stream" if body else "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raw = response.read(MAX_RESPONSE + 1)
                try:
                    message = json.loads(raw).get("error", {}).get("message", "Backend request failed.")
                except (ValueError, AttributeError):
                    message = "Backend request failed."
                self.json_error(response.status, message)
                return
            if body:
                if "text/event-stream" not in response.getheader("Content-Type", ""):
                    raise ValueError("Backend did not return SSE token streaming.")
                self.send_headers(200, "text/event-stream; charset=utf-8")
                stream_started = True
                size = 0
                while True:
                    data = response.read1(4096)
                    if not data:
                        break
                    size += len(data)
                    if size > MAX_RESPONSE:
                        raise ValueError("Backend stream exceeded 1 MiB.")
                    self.wfile.write(data)
                    self.wfile.flush()
                if expired.is_set():
                    raise TimeoutError("Inference exceeded the 120-second request limit.")
            else:
                data = response.read(MAX_RESPONSE + 1)
                if len(data) > MAX_RESPONSE:
                    raise ValueError("Backend response exceeded the size limit.")
                self.send_headers(200, "application/json", len(data))
                self.wfile.write(data)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            message = "Local TensorRT-Edge-LLM backend is not ready." if not body else str(exc)
            try:
                if stream_started:
                    self.wfile.write(("\nevent: error\ndata: " + json.dumps({"error": {"message": message}}) + "\n\n").encode())
                    self.wfile.flush()
                else:
                    self.json_error(503 if not body else 502, message)
            except OSError:
                pass
        finally:
            finished.set()
            if response:
                response.close()
            connection.close()
            if watcher:
                watcher.join(timeout=0.5)

    def reachy_refusal(self, status, raw):
        # Pass the bridge's own status and words through: its 503 says why there is no frame
        # ("no fresh frame (dormant: camera held by the robot app 'x')"), which is the useful part.
        text = raw[:MAX_RESPONSE].decode("utf-8", "replace").strip()[:300]
        self.json_error(status if 400 <= status <= 599 else 502,
                        f"Reachy bridge: {text or f'HTTP {status}'}")

    def reachy_fetch(self, path, content_type, token=None):
        """One bounded answer from the bridge: its health, or its newest frame."""
        extra_headers = {}
        with RELAYED_STREAMS_LOCK:
            entry = RELAYED_STREAMS.get(token) if token else None
            idle = None if entry is None else time.monotonic() - entry[0]
            slots = OPEN_REACHY_STREAMS
        if token:
            extra_headers["X-Reachy-Stream"] = "closed" if idle is None else f"idle_ms={round(idle * 1000)}"
        if path == "/healthz":
            # A refused <img> cannot see its 503, so health says whether the cap is why.
            extra_headers["X-Reachy-Slots"] = f"{slots}/{MAX_REACHY_STREAMS}"
        connection = http.client.HTTPConnection(*self.server.reachy_address, timeout=REACHY_SECONDS)
        response = None
        try:
            try:
                connection.request("GET", path, headers={"Accept": content_type})
                response = connection.getresponse()
                data = response.read(MAX_RESPONSE + 1)
            except (OSError, http.client.HTTPException):
                self.json_error(502, "Reachy bridge is not reachable.")
                return
            if len(data) > MAX_RESPONSE:
                self.json_error(502, "Reachy bridge response exceeded the size limit.")
            elif response.status != 200:
                self.reachy_refusal(response.status, data)
            elif media_type(response) != content_type:
                self.json_error(502, "Reachy bridge returned an unexpected content type.")
            else:
                self.send_headers(200, content_type, len(data), extra_headers)
                self.wfile.write(data)
        except OSError:
            pass  # The browser has gone; nobody is left to tell.
        finally:
            if response:
                response.close()
            connection.close()

    def reachy_stream(self, path, content_type, token=None):
        """Relay an endless bridge stream (MJPEG or MP3) until the browser or the bridge stops.

        Never takes GENERATION_LOCK: watching or listening to the robot must neither wait for
        inference nor hold it up. Every way out - browser gone, bridge restarted, idle timeout -
        ends the same way: both sockets closed, the slot returned, the token forgotten.
        """
        global OPEN_REACHY_STREAMS
        if not REACHY_STREAM_SLOTS.acquire(timeout=STREAM_HANDOFF_SECONDS):
            self.json_error(503, f"{MAX_REACHY_STREAMS} Reachy streams are already open on this server. "
                                 "Close another Live Vision tab or turn Listen off.")
            return
        with RELAYED_STREAMS_LOCK:
            OPEN_REACHY_STREAMS += 1
        connection = http.client.HTTPConnection(*self.server.reachy_address, timeout=REACHY_SECONDS)
        finished = threading.Event()
        response = None
        watcher = None
        started = False
        entry = [time.monotonic()]
        if token:
            with RELAYED_STREAMS_LOCK:
                RELAYED_STREAMS[token] = entry
        try:
            connection.connect()
            upstream_socket = connection.sock
            connection.request("GET", path, headers={"Accept": content_type})
            response = connection.getresponse()
            if response.status != 200:
                self.reachy_refusal(response.status, response.read(MAX_RESPONSE + 1))
                return
            # Relayed whole, because the multipart boundary lives in it.
            upstream_type = response.getheader("Content-Type", "")
            if media_type(response) != content_type or not re.fullmatch(r"[\x20-\x7e]{1,200}", upstream_type):
                self.json_error(502, "Reachy bridge returned an unexpected content type.")
                return
            upstream_socket.settimeout(STREAM_IDLE_SECONDS)

            def watch_disconnect():
                # The browser sends nothing after its GET on this close-after-response
                # connection, so readable means gone: Stop, img.src = "", a closed tab.
                # Shutting the bridge socket unblocks the relay now, not at the next frame,
                # which may be minutes away while the robot is dormant.
                while not finished.wait(0.2):
                    if self.client_disconnected():
                        break
                if not finished.is_set():
                    try:
                        upstream_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            watcher = threading.Thread(target=watch_disconnect, name="reachy-stream-watch", daemon=True)
            watcher.start()
            self.send_headers(200, upstream_type)
            started = True
            while True:
                data = response.read1(STREAM_CHUNK)
                if not data:
                    break
                self.wfile.write(data)
                entry[0] = time.monotonic()
        except (OSError, http.client.HTTPException):
            if not started:
                try:
                    self.json_error(502, "Reachy bridge is not reachable.")
                except OSError:
                    pass
        finally:
            finished.set()
            if token:
                with RELAYED_STREAMS_LOCK:
                    if RELAYED_STREAMS.get(token) is entry:
                        del RELAYED_STREAMS[token]
            if response:
                response.close()
            connection.close()
            if watcher:
                watcher.join(timeout=0.5)
            with RELAYED_STREAMS_LOCK:
                OPEN_REACHY_STREAMS -= 1
            REACHY_STREAM_SLOTS.release()

    # ------------------------------------------------------------------ Reachy motors/apps/speech
    # Distinct from REACHY_FETCHES/REACHY_STREAMS above: those relay the bridge's camera/mic feed
    # (read-only, token-gated against drive-by embedding) and stay open for a browser tab. These
    # talk to the ROBOT'S OWN DAEMON (motors, apps, speaker) - one-shot requests, no stream, no
    # relay token - trusting the same same-origin check (origin_allowed) as /v1/chat/completions.

    def read_json_body(self, max_len=8192):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        if length > max_len:
            raise ValueError(f"body must be under {max_len} bytes")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def reachy_result(self, ok, msg):
        body = json.dumps({"ok": ok, "message": msg}).encode()
        self.send_headers(200 if ok else 400, "application/json", len(body))
        self.wfile.write(body)

    def reachy_setup_status(self):
        setup = self.server.reachy_setup
        client = self.server.reachy_client
        result = (setup.snapshot() if setup else {
            "available": False, "bridge_prepared": False, "configured": bool(client),
            "address": urlsplit(client.base).hostname if client else "", "skipped": False, "busy": False,
            "message": "Reachy setup is not enabled on this installation. Run setup-orin.sh --reachy-only."})
        runtime = self.server.reachy_runtime
        if runtime and runtime.client_factory is None:
            result.update(status="error", configured=False, available=False, address="",
                          message="Reachy control dependencies are missing. Run setup-orin.sh --reachy-only on the Orin; image inference remains available.")
        body = json.dumps(result).encode()
        self.send_headers(200, "application/json", len(body))
        self.wfile.write(body)

    def reachy_setup_action(self, action):
        # Address changes and discovery require the same process token as engine
        # selection. A different website cannot use this UI as a LAN probe.
        if self.host_refused():
            return
        if not self.fetch_site_allowed():
            self.json_error(403, "Cross-site requests are disabled.")
            return
        token = self.headers.get("X-Reachy-Token", "")
        if not secrets.compare_digest(token.encode(), RELAY_TOKEN.encode()):
            self.json_error(401, "Missing or out-of-date access token. Reload Live Vision.")
            return
        setup = self.server.reachy_setup
        if setup is None:
            self.json_error(503, "Reachy setup is not enabled. Run setup-orin.sh --reachy-only.")
            return
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Chunked uploads are disabled.")
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("Content-Type must be application/json.")
            if not 0 < int(self.headers.get("Content-Length", "0")) <= 1024:
                raise ValueError("Setup requires a JSON object under 1024 bytes.")
            payload = self.read_json_body(max_len=1024)
            if action == "discover":
                if payload:
                    raise ValueError("Discovery takes no network targets or ranges.")
                result = setup.discover()
            elif action in {"configure", "skip"}:
                runtime = self.server.reachy_runtime
                if not runtime:
                    raise RuntimeError("Reachy setup runtime is not available.")
                # Finish any in-flight speech/control request against the previous
                # robot before changing the target for both HTTP and HTTPS.
                if not runtime.lock.acquire(blocking=False):
                    self.json_error(409, "Reachy is busy. Wait for the current operation and retry.")
                    return
                try:
                    if action == "configure":
                        if set(payload) != {"address"} or not isinstance(payload["address"], str):
                            raise ValueError("Enter one robot IP address or hostname.")
                        result = setup.configure(payload["address"])
                    else:
                        if payload:
                            raise ValueError("Skip takes no parameters.")
                        result = setup.skip()
                    runtime.refresh()
                finally:
                    runtime.lock.release()
            else:
                self.json_error(404, "Unknown Reachy setup action.")
                return
            body = json.dumps(result).encode()
            self.send_headers(200, "application/json", len(body))
            self.wfile.write(body)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.json_error(400, str(exc))
        except (OSError, RuntimeError) as exc:
            self.json_error(503, str(exc))

    def reachy_state(self):
        if self.server.reachy_runtime:
            self.server.reachy_runtime.refresh()
        client = self.server.reachy_client
        if not client:
            body = json.dumps({"enabled": False}).encode()
            self.send_headers(200, "application/json", len(body))
            self.wfile.write(body)
            return
        st = client.state()
        st["enabled"] = True
        st["speech_enabled"] = self.server.piper is not None
        body = json.dumps(st).encode()
        self.send_headers(200, "application/json", len(body))
        self.wfile.write(body)

    def reachy_apps(self):
        if self.server.reachy_runtime:
            self.server.reachy_runtime.refresh()
        if not self.server.reachy_client:
            self.json_error(503, "reachy daemon not configured (--reachy-daemon-url)")
            return
        body = json.dumps(self.server.reachy_client.apps()).encode()
        self.send_headers(200, "application/json", len(body))
        self.wfile.write(body)

    def reachy_control(self, sub):
        runtime = self.server.reachy_runtime
        if runtime:
            with runtime.lock:
                runtime.refresh()
                if (not runtime.address or
                        self.headers.get("X-Reachy-Address", "") != runtime.address):
                    self.json_error(409, "The selected robot changed. Refresh Reachy Mini setup before controlling it.")
                    return
                self._reachy_control(sub)
        else:
            self._reachy_control(sub)

    def _reachy_control(self, sub):
        """Dispatch every POST /api/reachy/<sub>. sub has no leading slash (stripped by do_POST)."""
        client = self.server.reachy_client
        parts = sub.split("/")
        try:
            if sub == "speak":
                self.reachy_speak()
                return
            if not client:
                self.json_error(503, "reachy daemon not configured (--reachy-daemon-url)")
                return
            if parts[0] == "action" and len(parts) == 2:
                actions = {"wake": client.wake, "sleep": client.sleep, "center": client.center,
                          "look-at-voice": client.look_at_voice}
                fn = actions.get(parts[1])
                if not fn:
                    self.json_error(404, f"unknown action {parts[1]}")
                    return
                self.reachy_result(*fn())
                return
            if parts[0] == "motors" and len(parts) == 2:
                self.reachy_result(*client.set_motor_mode(parts[1]))
                return
            if parts[0] == "volume" and len(parts) == 3:
                if parts[1] not in ("speaker", "mic"):
                    self.json_error(400, "which must be speaker or mic")
                    return
                self.reachy_result(*client.set_volume(parts[1], int(parts[2])))
                return
            if parts[0] == "look" and len(parts) == 3:
                if parts[1] not in ("pitch", "yaw", "roll", "body_yaw"):
                    self.json_error(400, "axis must be pitch, yaw, roll or body_yaw")
                    return
                self.reachy_result(*client.look(**{parts[1]: float(parts[2])}))
                return
            if sub == "target":
                body = self.read_json_body()
                pose = {k: body[k] for k in ("x", "y", "z", "roll", "pitch", "yaw")
                       if body.get(k) is not None}
                antennas = body.get("antennas")
                if antennas is not None and (not isinstance(antennas, (list, tuple)) or len(antennas) != 2):
                    self.json_error(400, "antennas must be [left, right]")
                    return
                self.reachy_result(*client.set_target(pose=pose or None, body_yaw=body.get("body_yaw"),
                                                       antennas=antennas))
                return
            if parts[0] == "apps" and len(parts) == 3 and parts[1] == "start":
                self.reachy_result(*client.start_app(parts[2]))
                return
            if sub == "apps/stop":
                self.reachy_result(*client.stop_app())
                return
            self.json_error(404, "unknown Reachy control route")
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self.json_error(400, str(exc))

    def reachy_speak(self):
        piper = self.server.piper
        if not piper:
            self.json_error(503, "speech not configured (--piper-bin / --piper-model)")
            return
        try:
            body = self.read_json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            self.json_error(400, str(exc))
            return
        text = str(body.get("text") or "").strip()
        if not text:
            self.json_error(400, "expected a non-empty 'text'")
            return
        rate = body.get("rate", 1.0)
        if type(rate) not in (int, float) or round(float(rate), 2) not in SPEECH_RATES:
            self.json_error(400, f"rate must be one of {', '.join(str(r) for r in SPEECH_RATES)}")
            return
        rate = float(rate)
        if not self.server.reachy_client:
            self.json_error(503, "reachy daemon not configured (--reachy-daemon-url)")
            return
        t0 = time.monotonic()
        wav = piper.synth(Piper.spoken_summary(text))
        if not wav:
            self.json_error(502, "speech synthesis failed or timed out")
            return
        wav = piper.retime(wav, rate)
        t1 = time.monotonic()
        try:
            ok, remote = self.server.reachy_client.upload_sound(wav.read_bytes(), wav.name)
            if not ok:
                self.reachy_result(False, f"upload failed: {remote}")
                return
            ok, msg = self.server.reachy_client.play_sound(remote)
        finally:
            try:
                wav.unlink()
            except OSError:
                pass
        t2 = time.monotonic()
        if ok:
            msg = f"spoke in {t2 - t0:.2f}s (synth {t1 - t0:.2f}s, play {t2 - t1:.2f}s)"
        self.reachy_result(ok, msg)

    def engine_switch(self, eid):
        if self.host_refused():
            return
        token = self.headers.get("X-Reachy-Token", "")
        if not self.fetch_site_allowed() or not secrets.compare_digest(token.encode(), RELAY_TOKEN.encode()):
            self.json_error(403, "Reload Live Vision before switching engines.")
            return
        switcher = self.server.engine_switcher
        if not switcher:
            self.json_error(503, "engine switching not configured (--engine-link/--engines-config)")
            return
        # Can take up to 120s (symlink + service restart + readiness poll) - see EngineSwitcher.
        # No separate ack-then-poll: a slow synchronous response is simpler for a demo UI and this
        # server already has one thread per request (ThreadingHTTPServer).
        ok, msg = switcher.switch(eid)
        self.reachy_result(ok, msg)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--backend-port", type=int, default=8000)
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--https-port", type=int,
                        help="Add HTTPS on this port while the primary port serves HTTP.")
    parser.add_argument("--allow-insecure-lan", action="store_true",
                        help="Explicitly allow LAN HTTP for image uploads. Cameras need HTTPS.")
    parser.add_argument("--reachy-url", default="http://127.0.0.1:8099",
                        help="Reachy Mini bridge relayed under /reachy/ (plain HTTP on the Jetson).")
    parser.add_argument("--allowed-host", action="append", default=[], metavar="NAME",
                        help="A host name (such as orin.local) that /reachy/ and /api/access answer "
                             "to, besides IP addresses and localhost. Repeat for more names.")
    parser.add_argument("--reachy-daemon-url", default="",
                        help="Robot daemon REST API (motors, apps, volume) - NOT the camera/mic "
                             "bridge. Empty disables motor/app control; the camera/mic still works "
                             "through --reachy-url either way.")
    parser.add_argument("--reachy-config", type=Path, default=None,
                        help="Device-local reachy.env managed by optional Reachy setup in the UI.")
    parser.add_argument("--piper-bin", type=Path, default=None,
                        help="Path to a Piper TTS binary. Empty disables /api/reachy/speak.")
    parser.add_argument("--piper-model", type=Path, default=None,
                        help="Path to a Piper .onnx voice model. Required with --piper-bin.")
    parser.add_argument("--piper-out-dir", type=Path, default=Path("/tmp/reachy_tts"),
                        help="Scratch directory for synthesized WAV files.")
    parser.add_argument("--engine-link", type=Path, default=None,
                        help='Symlink the shim reads its engine directory from. Only for a '
                             'registry whose entries are not "kind": "service", where it is '
                             'required; needs passwordless `sudo ln -sfn` on this path.')
    parser.add_argument("--engines-config", type=Path, default=None,
                        help='JSON file: {"id": {"name": "...", "path": "...", "profile": "..."}, '
                             '...}. Empty disables /api/engines.')
    parser.add_argument("--default-engine", default=None,
                        help="Initial engine ID for a registry of separate model services.")
    parser.add_argument("--shim-service", default="live-vision-cosmos-demo-shim",
                        help='With --engine-link: systemd unit restarted after an engine swap - '
                             'needs passwordless `sudo systemctl restart` on this unit. Default '
                             'matches systemd/live-vision-cosmos-demo-shim.service.')
    parser.add_argument("--services-config", type=Path, default=None,
                        help='JSON file: {"Display Name": {"cmdline_match": "substring to find '
                             'in /proc/*/cmdline", "path": "/dir/to/report/size/and/disk/for"}, '
                             '...} - for /api/services, services this process does not itself '
                             "manage (Live Vision itself and Piper are always included).")
    args = parser.parse_args()
    try:
        allowed_hosts = frozenset(allowed_host_name(name) for name in args.allowed_host)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        loopback = args.host == "localhost" or ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        parser.error("--host must be an IP address or localhost.")
    try:
        reachy_address = bridge_address(args.reachy_url)
    except ValueError as exc:
        parser.error(str(exc))
    if args.reachy_config and args.reachy_daemon_url:
        parser.error("Use --reachy-config or --reachy-daemon-url, not both.")
    if args.reachy_daemon_url and Reachy is None:
        parser.error("--reachy-daemon-url needs the `requests` package "
                     "(sudo apt install python3-requests).")
    if bool(args.piper_bin) != bool(args.piper_model):
        parser.error("Provide both --piper-bin and --piper-model.")
    if args.piper_bin and not (args.reachy_daemon_url or args.reachy_config):
        parser.error("--piper-bin needs --reachy-daemon-url or --reachy-config (speech plays through the robot).")
    engines = {}
    managed_engines = False
    if args.engines_config:
        try:
            engines = json.loads(args.engines_config.read_text())
            if not isinstance(engines, dict) or not all(
                    isinstance(e, dict) and "name" in e and "path" in e for e in engines.values()):
                raise ValueError("expected {id: {name, path, ...}}")
            managed_engines = bool(engines) and all(e.get("kind") == "service" for e in engines.values())
            if any(e.get("kind") == "service" for e in engines.values()) and not managed_engines:
                raise ValueError("Do not mix symlink and separate-service engines")
            if managed_engines and not args.default_engine:
                raise ValueError("Separate-service engines require --default-engine")
            if not managed_engines and not args.engine_link:
                raise ValueError("Legacy symlink engines require --engine-link")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(f"--engines-config: {exc}")
    elif args.engine_link:
        parser.error("--engine-link requires --engines-config")
    services_config = {}
    if args.services_config:
        try:
            services_config = json.loads(args.services_config.read_text())
            if not isinstance(services_config, dict) or not all(
                    isinstance(s, dict) and "cmdline_match" in s for s in services_config.values()):
                raise ValueError("expected {name: {cmdline_match, path?}}")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(f"--services-config: {exc}")
    if any(not 1 <= port <= 65535 for port in
           (args.port, args.backend_port, *([args.https_port] if args.https_port is not None else []))):
        parser.error("Ports must be from 1 to 65535.")
    if bool(args.cert) != bool(args.key):
        parser.error("Provide both --cert and --key.")
    if args.https_port is not None and (not args.cert or args.https_port == args.port):
        parser.error("--https-port requires a certificate and a different port from --port.")
    primary_tls = bool(args.cert) and args.https_port is None
    if not loopback and not primary_tls and not args.allow_insecure_lan:
        parser.error("LAN HTTP requires explicit --allow-insecure-lan. Cameras need HTTPS.")
    if ":" in args.host:
        Server.address_family = socket.AF_INET6
    context = None
    if args.cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.cert, args.key)
    servers = []
    threads = []
    reachy_runtime = None
    try:
        reachy_client = Reachy(args.reachy_daemon_url) if args.reachy_daemon_url else None
        piper = (Piper(args.piper_bin, args.piper_model, args.piper_out_dir)
                if args.piper_bin and args.piper_model else None)
        reachy_setup = None
        if args.reachy_config:
            reachy_setup = ReachySetup(args.reachy_config)
            piper_factory = (lambda: Piper(args.piper_bin, args.piper_model, args.piper_out_dir)) if piper else None
            reachy_runtime = ReachyRuntime(reachy_setup, piper_factory=piper_factory)
            reachy_setup.on_change = reachy_runtime.apply
            reachy_runtime.refresh()
        if managed_engines:
            engine_switcher = ServiceEngineSwitcher(engines, args.default_engine, GENERATION_LOCK)
        else:
            engine_switcher = (EngineSwitcher(args.engine_link, engines, args.shim_service, args.backend_port)
                               if engines else None)
        server = Server((args.host, args.port), Handler)
        servers.append(server)
        server.backend_port = args.backend_port
        server.reachy_address = reachy_address
        server.reachy_client = reachy_client
        server.piper = piper
        server.reachy_setup = reachy_setup
        server.reachy_runtime = reachy_runtime
        server.engine_switcher = engine_switcher
        server.services_config = services_config
        server.https_port = args.https_port
        server.allowed_hosts = allowed_hosts
        if primary_tls:
            server.socket = context.wrap_socket(server.socket, server_side=True)
        if args.https_port is not None:
            secure = Server((args.host, args.https_port), Handler, telemetry=server.telemetry)
            servers.append(secure)
            secure.backend_port = args.backend_port
            secure.reachy_address = reachy_address
            secure.reachy_client = reachy_client
            secure.piper = piper
            secure.reachy_setup = reachy_setup
            secure.reachy_runtime = reachy_runtime
            secure.engine_switcher = engine_switcher
            secure.services_config = services_config
            secure.allowed_hosts = allowed_hosts
            secure.socket = context.wrap_socket(secure.socket, server_side=True)
            worker = threading.Thread(target=secure.serve_forever, name="https-ui", daemon=True)
            worker.start()
            threads.append((secure, worker))
            print(f"Camera UI: https://{args.host}:{args.https_port}", flush=True)
        scheme = "https" if primary_tls else "http"
        print(f"UI: {scheme}://{args.host}:{args.port}; backend: http://127.0.0.1:{args.backend_port}", flush=True)
        print(f"Reachy Mini bridge: {args.reachy_url}, relayed under /reachy/", flush=True)
        print(f"Reachy Mini setup: {'available in the UI' if reachy_setup else args.reachy_daemon_url or 'not configured'}", flush=True)
        print(f"Speech: {'Piper installed; starts on first speech request when a robot is configured' if piper else 'not configured'}", flush=True)
        print(f"Engines: {', '.join(engines) if engines else 'not configured'}", flush=True)
        print("UI availability does not imply model or Jetson readiness.", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for running, worker in threads:
            running.shutdown()
            worker.join(timeout=2)
        for server in reversed(servers):
            server.server_close()
        if reachy_runtime:
            reachy_runtime.close()


if __name__ == "__main__":
    main()
