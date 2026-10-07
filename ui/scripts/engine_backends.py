# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Allowlisted, single-resident model services for the Live Vision proxy.

The registry is local administrator configuration, never request-supplied URLs or
commands. Selecting a model drains inference, stops the previous service, then
checks the new service's identity. A failed start restores the previous service.
"""

import http.client
import json
import re
import subprocess
import threading
import time
from pathlib import Path

ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
UNIT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,127}(?:\.service)?\Z")


def validate_registry(entries):
    if not isinstance(entries, dict) or not entries:
        raise ValueError("Expected a nonempty model service registry")
    ports, units = set(), set()
    for key, entry in entries.items():
        if not isinstance(key, str) or not ID.fullmatch(key) or not isinstance(entry, dict):
            raise ValueError("Invalid engine identifier")
        if entry.get("kind") != "service" or entry.get("protocol") != "cosmos":
            raise ValueError("Service engines need an explicit cosmos protocol")
        if not isinstance(entry.get("name"), str) or not entry["name"].strip():
            raise ValueError("Engine name is required")
        if not isinstance(entry.get("model_id"), str) or not entry["model_id"].strip():
            raise ValueError("Engine model_id is required")
        port = entry.get("backend_port")
        if type(port) is not int or not 1 <= port <= 65535 or port in ports:
            raise ValueError("Engines need distinct valid loopback ports")
        service = entry.get("service")
        if not isinstance(service, str) or not UNIT.fullmatch(service):
            raise ValueError("Invalid allowlisted systemd service")
        service = service.removesuffix(".service") + ".service"
        if service in units:
            raise ValueError("Each engine requires its own service")
        path = entry.get("path")
        if not isinstance(path, str) or not Path(path).is_absolute() or ".." in Path(path).parts:
            raise ValueError("Engine path must be absolute")
        ports.add(port)
        units.add(service)
    return entries


class ServiceEngineSwitcher:
    managed = True

    def __init__(self, entries, default, generation_lock, timeout=120):
        self.engines = validate_registry(entries)
        if default not in entries:
            raise ValueError("Default engine is not registered")
        self.selected = default
        self.generation_lock = generation_lock
        self.lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.switching = False
        self.switch_target = None
        self.switch_started_at = None
        self.timeout = timeout
        # UI restarts must not claim the configured default if another service is
        # actually resident. Reading model identities neither starts nor stops one.
        ready = [key for key in entries if self.availability(key)[0] and self.healthy(key)]
        if len(ready) == 1:
            self.selected = ready[0]

    def availability(self, key):
        entry = self.engines[key]
        if entry.get("enabled", True) is not True:
            return False, entry.get("unavailable_reason", "Not installed yet")
        if not (Path(entry["path"]) / "llm.engine").is_file():
            return False, "Engine has not been built on this device"
        return True, ""

    def descriptor(self, key):
        entry = self.engines[key]
        available, reason = self.availability(key)
        return {"id": key, "name": entry["name"], "model_id": entry["model_id"],
                "profile": entry.get("profile", ""), "available": available, "reason": reason}

    def active(self):
        with self.state_lock:
            key = self.selected
        return self.descriptor(key)

    def status(self):
        progress = None
        if self.switching and self.switch_started_at is not None:
            # started_at/timeout, not a percentage: the actual step (stop old, start new, poll
            # health) has no measurable midpoint, so the caller computes elapsed/timeout itself
            # and decides how close to the timeout to let the bar visually reach.
            progress = {"target": self.switch_target, "started_at": self.switch_started_at,
                       "timeout": self.timeout}
        return {"configured": True, "active": self.active(), "switching": self.switching,
                "switch_progress": progress,
                "engines": [self.descriptor(key) for key in self.engines]}

    def backend(self):
        with self.state_lock:
            return dict(self.engines[self.selected])

    def healthy(self, key):
        entry = self.engines[key]
        connection = http.client.HTTPConnection("127.0.0.1", entry["backend_port"], timeout=2)
        try:
            connection.request("GET", "/v1/models")
            response = connection.getresponse()
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                return False
            models = json.loads(raw)
            if entry["model_id"] not in [m.get("id") for m in models.get("data", []) if isinstance(m, dict)]:
                return False
            response.close()
            connection.close()
            connection = http.client.HTTPConnection("127.0.0.1", entry["backend_port"], timeout=2)
            connection.request("GET", "/health/ready")
            response = connection.getresponse()
            raw = response.read(65537)
            return response.status == 200 and len(raw) <= 65536 and json.loads(raw).get("status") == "ready"
        except (OSError, http.client.HTTPException, ValueError, AttributeError, TypeError):
            return False
        finally:
            connection.close()

    def command(self, action, key):
        # No shell and no command or unit name from the HTTP request.
        try:
            result = subprocess.run(["sudo", "-n", "systemctl", action, self.engines[key]["service"]],
                                    capture_output=True, text=True, timeout=30)
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def wait_ready(self, key):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.healthy(key):
                return True
            time.sleep(0.5)
        return False

    def switch(self, key):
        if key not in self.engines:
            return False, "Unknown engine"
        available, reason = self.availability(key)
        if not available:
            return False, reason
        if not self.lock.acquire(blocking=False):
            return False, "Another engine switch is in progress"
        acquired = False
        try:
            self.switching = True
            self.switch_target = key
            self.switch_started_at = time.time()
            acquired = self.generation_lock.acquire(timeout=5)
            if not acquired:
                return False, "Inference is still running; stop Live Vision and retry"
            previous = self.selected
            # Stop every other registered model, including an accidentally started
            # second service, before allocating the new model's shared GPU memory.
            for other in self.engines:
                if other != key and not self.command("stop", other):
                    return False, "Could not stop the previous model; no new model was started"
            if key == previous and self.healthy(key):
                return True, f"Already using {self.engines[key]['name']}"
            if self.command("start", key) and self.wait_ready(key):
                with self.state_lock:
                    self.selected = key
                return True, f"Switched to {self.engines[key]['name']}"
            stopped = self.command("stop", key)
            restored = stopped and self.command("start", previous) and self.wait_ready(previous)
            if restored:
                return False, f"New model failed to start; restored {self.engines[previous]['name']}"
            return False, "New model failed to start and recovery needs attention; inference is unavailable"
        finally:
            if acquired:
                self.generation_lock.release()
            self.switching = False
            self.switch_target = None
            self.switch_started_at = None
            self.lock.release()
