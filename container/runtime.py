#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Supervise one shim and one HTTPS UI without systemd or a Docker socket."""
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

from bundle import SPEC, verify


def supervise(commands, ready_url, timeout=180):
    processes = []
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        processes.append(subprocess.Popen(commands[0], start_new_session=True))
        deadline = time.monotonic() + timeout
        while not stopping:
            if processes[0].poll() is not None:
                raise RuntimeError("Inference process exited before becoming ready")
            try:
                with urllib.request.urlopen(ready_url, timeout=1) as response:
                    if json.load(response).get("status") == "ready":
                        break
            except (OSError, ValueError, urllib.error.URLError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Inference readiness timed out")
            time.sleep(0.1)
        if stopping:
            return 0
        processes.append(subprocess.Popen(commands[1], start_new_session=True))
        while not stopping:
            for proc in processes:
                if proc.poll() is not None:
                    raise RuntimeError(f"Runtime child exited with status {proc.returncode}")
            time.sleep(0.1)
        return 0
    finally:
        for proc in processes:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for proc in processes:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    app = Path(__file__).resolve().parents[1]
    spec = json.loads(SPEC.read_text())
    if platform.system() != "Linux" or platform.machine() != spec["target"]["architecture"]:
        raise ValueError("Runtime requires aarch64 Linux on the supported Jetson")
    import tensorrt
    if tensorrt.__version__ != spec["target"]["tensorrt"]:
        raise ValueError("TensorRT runtime does not match the bundled compatibility specification")
    bundle = Path(os.environ.get("BUNDLE_DIR", "/models"))
    verify(bundle, spec, candidate_commit=os.environ.get("CANDIDATE_COMMIT", ""))
    tls = Path(os.environ.get("TLS_DIR", "/tls"))
    if not all((tls / name).is_file() for name in ("cert.pem", "key.pem")):
        raise ValueError("Mount cert.pem and key.pem read-only at /tls")
    os.environ.update({
        "ENGINE_DIR": str(bundle / "engines/reasoning"),
        "SHARED_CHECKPOINT_DIR": str(bundle / "model"),
        "PORT": "8001",
    })
    # Invoke uvicorn directly to keep the shim on loopback without changing its source API.
    shim = [sys.executable, "-m", "uvicorn", "cosmos3_shim_v1:app", "--app-dir", str(app / "shim"),
            "--host", "127.0.0.1", "--port", "8001", "--log-level", "warning"]
    ui = [sys.executable, str(app / "ui/scripts/serve_ui.py"), "--host", "0.0.0.0",
          "--port", "8443", "--backend-port", "8001",
          "--cert", str(tls / "cert.pem"), "--key", str(tls / "key.pem")]
    if os.environ.get("REACHY_DAEMON_URL"):
        ui += ["--reachy-daemon-url", os.environ["REACHY_DAEMON_URL"]]
    if os.environ.get("UI_ALLOWED_HOST"):
        ui += ["--allowed-host", os.environ["UI_ALLOWED_HOST"]]
    return supervise([shim, ui], "http://127.0.0.1:8001/health/ready")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        sys.exit(f"Runtime startup failed: {exc}")
