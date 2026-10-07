#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Explicit, bounded Reachy Mini setup shared by the UI and command line.

Discovery consumes Pollen's _reachy-mini._tcp.local advertisements, then verifies
the advertised daemon's REST schema. It never scans addresses or ports. These
checks identify the expected protocol, not cryptographic robot identity.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time


UNIT = "reachy-mjpeg-bridge.service"
SERVICE_TYPE = "_reachy-mini._tcp"
MAX_JSON = 65536
MAX_DISCOVERY = 65536
MAX_DEVICES = 8
LAN = tuple(ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
HOSTNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\Z")
DAEMON_STATES = {"not_initialized", "starting", "running", "stopping", "stopped", "error"}


def private_ipv4(address):
    try:
        value = ipaddress.IPv4Address(address)
    except (ipaddress.AddressValueError, TypeError):
        raise ValueError("Enter a private IPv4 LAN address or a local hostname.") from None
    if not any(value in network for network in LAN):
        raise ValueError("Reachy must use a private IPv4 LAN address.")
    return str(value)


def clean_host(address):
    if not isinstance(address, str) or not address or len(address) > 253 or address != address.strip():
        raise ValueError("Enter only an IPv4 address or hostname, without a URL, port or spaces.")
    if not HOSTNAME.fullmatch(address) or re.fullmatch(r"[0-9.]+", address):
        return private_ipv4(address)
    # Hex, integer and abbreviated numeric addresses must never reach a resolver.
    if address.lower().startswith("0x") or address.lower() in {"localhost", "localhost.localdomain"}:
        raise ValueError("Enter the robot's private LAN address or hostname.")
    return address.lower()


def validate_status(value):
    """Check Pollen's daemon status shape; accepting an arbitrary 200 is unsafe."""
    if not isinstance(value, dict):
        raise ValueError("The address did not return a Reachy Mini daemon status.")
    name = value.get("robot_name")
    if (not isinstance(name, str) or not 1 <= len(name) <= 128
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or not isinstance(value.get("state"), str) or value["state"] not in DAEMON_STATES
            or type(value.get("wireless_version")) is not bool
            or "backend_status" not in value
            or (value["backend_status"] is not None and not isinstance(value["backend_status"], dict))):
        raise ValueError("The address did not return a recognized Reachy Mini daemon status.")
    return value


def fetch_status(address, timeout):
    """Fixed numeric target/path; no proxies, redirects, URL parsing or credentials."""
    address = private_ipv4(address)
    connection = http.client.HTTPConnection(address, 8000, timeout=timeout)
    timer = None
    try:
        connection.connect()
        sock = connection.sock
        def expire():
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        connection.request("GET", "/api/daemon/status", headers={"Accept": "application/json", "Connection": "close"})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError("The robot did not return a successful daemon status.")
        if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("The robot did not return JSON daemon status.")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > MAX_JSON):
            raise ValueError("Robot status response is too large or malformed.")
        data = response.read(MAX_JSON + 1)
        if len(data) > MAX_JSON:
            raise ValueError("Robot status response is too large.")
        try:
            parsed = json.loads(data)
        except (ValueError, RecursionError) as error:
            raise ValueError("Robot status JSON is malformed.") from error
        return validate_status(parsed)
    except (OSError, http.client.HTTPException) as error:
        raise RuntimeError("Cannot reach the Reachy Mini daemon on port 8000.") from error
    finally:
        if timer is not None:
            timer.cancel()
        connection.close()


def resolve_host(hostname, timeout):
    # getaddrinfo has no total timeout. A separate fixed-code process is killable;
    # input is an argv value, never shell code. Only IPv4 results are returned.
    code = ("import json,socket,sys; print(json.dumps(sorted({x[4][0] for x in "
            "socket.getaddrinfo(sys.argv[1],8000,socket.AF_INET,socket.SOCK_STREAM)})))")
    try:
        result = subprocess.run([sys.executable, "-c", code, hostname], capture_output=True,
                                text=True, timeout=timeout, check=True)
        if len(result.stdout) > 4096:
            raise ValueError("Too many hostname results.")
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise RuntimeError("Hostname lookup failed or timed out; enter the robot's LAN IPv4 address.") from error


def browse_services(timeout):
    """Read bounded avahi output without allowing its stdout to grow in memory."""
    command = ["avahi-browse", "--resolve", "--parsable", "--terminate", SERVICE_TYPE]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as error:
        raise RuntimeError("Discovery is unavailable. Install avahi-utils or enter the robot's LAN address.") from error
    data = bytearray()
    deadline = time.monotonic() + timeout
    import select
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Discovery timed out. Multicast may be blocked; enter the robot's LAN address.")
            ready, _, _ = select.select([process.stdout], [], [], remaining)
            if not ready:
                raise RuntimeError("Discovery timed out. Multicast may be blocked; enter the robot's LAN address.")
            block = os.read(process.stdout.fileno(), 4096)
            if not block:
                break
            data.extend(block)
            if len(data) > MAX_DISCOVERY:
                raise RuntimeError("Discovery returned too many advertisements; enter the robot's LAN address.")
        if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
            raise RuntimeError("Discovery could not contact Avahi. Enter the robot's LAN address.")
        return data.decode("utf-8", errors="replace")
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def service_action(action):
    if action not in {"restart", "stop"}:
        raise ValueError("Unsupported bridge service action")
    try:
        subprocess.run(["sudo", "-n", "/usr/bin/systemctl", action, UNIT],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=20, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("The camera bridge could not be changed. Complete Reachy setup on this Orin and retry.") from error


def service_active():
    try:
        result = subprocess.run(["/usr/bin/systemctl", "is-active", "--quiet", UNIT],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("Cannot read the camera bridge service state.") from error
    if result.returncode not in (0, 3, 4):
        raise RuntimeError("Cannot read the camera bridge service state.")
    return result.returncode == 0


class ReachySetup:
    def __init__(self, config_path, on_change=None, *, state_path=None,
                 discovery_runner=None, resolver=None, status_fetcher=None,
                 service_runner=None, service_state=None, prepared_checker=None,
                 discovery_timeout=6.0, request_timeout=2.0,
                 discovery_cooldown=10.0, configure_cooldown=2.0, blocked_addresses=None):
        self.config_path = Path(config_path)
        self.state_path = Path(state_path) if state_path else self.config_path.with_name("reachy-setup.json")
        self.lock_path = self.config_path.with_name("reachy-setup.lock")
        self.on_change = on_change or (lambda address: None)
        self.discovery_runner = discovery_runner or browse_services
        self.resolver = resolver or resolve_host
        self.status_fetcher = status_fetcher or fetch_status
        self.service_runner = service_runner or service_action
        self.service_state = service_state or service_active
        self.prepared_checker = prepared_checker or self._prepared
        self.discovery_timeout, self.request_timeout = discovery_timeout, request_timeout
        self.discovery_cooldown, self.configure_cooldown = discovery_cooldown, configure_cooldown
        if blocked_addresses is None:
            exclusions = os.environ.get("REACHY_EXCLUDED_ADDRESSES", "")
            blocked_addresses = [item.strip() for item in exclusions.split(",")] if exclusions else []
        self.blocked_addresses = frozenset(private_ipv4(value) for value in blocked_addresses)
        self._lock = threading.Lock()
        self._discovery_lock = threading.Lock()
        self._busy = threading.Event()
        self._last_discovery = self._last_configure = float("-inf")

    def _prepared(self):
        root = self.config_path.parent
        return ((root / "reachy_env/bin/python3").is_file()
                and (root / "reachy/reachy_mjpeg_bridge.py").is_file()
                and any((Path(folder) / UNIT).is_file() for folder in
                        ("/etc/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system")))

    @staticmethod
    def _read(path):
        if path.is_symlink():
            raise ValueError("Reachy setup files must not be symbolic links.")
        try:
            with path.open("rb") as handle:
                data = handle.read(16385)
        except FileNotFoundError:
            return None
        if len(data) > 16384:
            raise ValueError("Reachy setup file is unexpectedly large.")
        return data

    def _address(self, data):
        values = [line.split("=", 1)[1] for line in (data or b"").decode().splitlines()
                  if line.startswith("REACHY_MINI_IP=")]
        if len(values) > 1:
            raise ValueError("Reachy configuration contains duplicate addresses.")
        address = values[0] if values else ""
        return clean_host(address) if address else ""

    @staticmethod
    def _choice(data):
        if data is None:
            return {}
        state = json.loads(data)
        if (not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1
                or not isinstance(state.get("choice"), str) or state["choice"] not in {"configured", "skipped"}
                or not isinstance(state.get("address"), str)):
            raise ValueError("Reachy setup choice is malformed. Repair it before changing the robot.")
        if state["choice"] == "configured":
            private_ipv4(state["address"])
        elif state["address"]:
            raise ValueError("A skipped Reachy setup must have an empty address.")
        return state

    def snapshot(self):
        # This optional accessory must never prevent the camera/inference UI
        # from starting. Mutating methods use the strict readers and still fail
        # closed rather than silently replacing a damaged configuration.
        try:
            prepared = bool(self.prepared_checker())
        except OSError:
            prepared = False
        try:
            address = self._address(self._read(self.config_path))
            state = self._choice(self._read(self.state_path))
        except (ValueError, UnicodeError, OSError, RecursionError):
            return {"status": "error", "configured": False, "address": "", "skipped": False,
                    "available": prepared, "bridge_prepared": prepared, "busy": self._is_busy(),
                    "message": "Reachy setup files are unreadable or malformed. Repair reachy.env and reachy-setup.json on the Orin, then retry."}
        if address:
            try:
                private_ipv4(address)
                rejected = address in self.blocked_addresses
            except ValueError:
                rejected = True
            if rejected:
                # Reading configuration must not resolve or contact a legacy
                # hostname. Only an explicit connect may validate and pin it.
                return {"status": "error", "configured": False, "address": "", "skipped": False,
                        "available": prepared, "bridge_prepared": prepared, "busy": self._is_busy(),
                        "message": "Saved Reachy address is unpinned or excluded. Reconnect using set-reachy-ip.sh <verified-robot-address> on the Orin, or correct reachy.env there. Reachy controls are disabled."}
        skipped = not address and state.get("choice") == "skipped"
        return {"status": "configured" if address else "skipped" if skipped else "not_configured",
                "configured": bool(address), "address": address, "skipped": skipped,
                "available": prepared, "bridge_prepared": prepared, "busy": self._is_busy(),
                "message": "Reachy Mini is configured." if address else
                "Reachy Mini setup was skipped. You can add it later." if skipped else
                "Discover a Reachy Mini, enter its LAN address, or skip for now."}

    def _is_busy(self):
        if self._busy.is_set():
            return True
        try:
            descriptor = os.open(self.lock_path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(descriptor)

    def _pin(self, address):
        host = clean_host(address)
        try:
            addresses = [private_ipv4(host)]
        except ValueError:
            if re.fullmatch(r"[0-9.]+", host):
                raise
            addresses = self.resolver(host, self.request_timeout)
            if not isinstance(addresses, list) or not 1 <= len(addresses) <= 16:
                raise ValueError("The hostname did not resolve to a bounded set of IPv4 addresses.")
            addresses = sorted({private_ipv4(value) for value in addresses})
        if len(addresses) != 1:
            raise ValueError("The hostname resolves to multiple devices. Enter the intended robot's IPv4 address.")
        if addresses[0] in self.blocked_addresses:
            raise ValueError("This address is excluded from Reachy setup.")
        return addresses[0]

    def _verify(self, address):
        return validate_status(self.status_fetcher(address, self.request_timeout))

    def discover(self):
        if not self._discovery_lock.acquire(blocking=False):
            return {"devices": [], "status": "cooldown", "message": "Discovery is already running."}
        try:
            if time.monotonic() - self._last_discovery < self.discovery_cooldown:
                return {"devices": [], "status": "cooldown", "message": "Wait a few seconds before trying discovery again."}
            self._last_discovery = time.monotonic()
            try:
                output = self.discovery_runner(self.discovery_timeout)
                if not isinstance(output, str) or len(output.encode()) > MAX_DISCOVERY:
                    raise RuntimeError("Discovery output exceeded its limit.")
                candidates = {}
                for line in output.splitlines():
                    fields = line.split(";")
                    if (len(fields) < 9 or fields[0] != "=" or fields[2] != "IPv4"
                            or fields[4] != SERVICE_TYPE or fields[5] != "local" or fields[8] != "8000"):
                        continue
                    try:
                        address = private_ipv4(fields[7])
                        if address in self.blocked_addresses:
                            continue
                        hostname = clean_host(fields[6])
                    except ValueError:
                        continue
                    candidates.setdefault(address, {"address": address, "hostname": hostname})
                    if len(candidates) >= MAX_DEVICES:
                        break
                def verify(candidate):
                    try:
                        status = self._verify(candidate["address"])
                        return {**candidate, "name": status["robot_name"]}
                    except (ValueError, RuntimeError, OSError):
                        return None
                with ThreadPoolExecutor(max_workers=4) as pool:
                    devices = [item for item in pool.map(verify, candidates.values()) if item is not None]
                return {"devices": devices, "status": "found" if devices else "not_found",
                        "message": "Choose your Reachy Mini." if devices else
                        "No verified Reachy Mini was found. Check the same LAN or enter its address manually."}
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                return {"devices": [], "status": "unavailable", "message": str(error)}
        finally:
            self._discovery_lock.release()

    @contextmanager
    def _changing(self):
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Reachy setup is already changing. Try again when it finishes.")
        descriptor = None
        try:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another Reachy setup operation is running.") from None
            self._busy.set()
            yield
        finally:
            self._busy.clear()
            if descriptor is not None:
                os.close(descriptor)
            self._lock.release()

    @staticmethod
    def _write(path, data, mode):
        if path.is_symlink():
            raise ValueError("Reachy setup files must not be symbolic links.")
        if data is None:
            path.unlink(missing_ok=True)
            return
        descriptor, temporary = tempfile.mkstemp(prefix=".reachy-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), mode)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _service(self, action):
        result = self.service_runner(action)
        if result is False or (hasattr(result, "returncode") and result.returncode != 0):
            raise RuntimeError("The camera bridge service operation failed.")

    def _commit(self, address, choice):
        old_config, old_state = self._read(self.config_path), self._read(self.state_path)
        old_address = self._address(old_config)
        self._choice(old_state)
        was_active = bool(self.service_state())
        prior = self.snapshot()
        # A legacy hostname or excluded address is not a safe rollback target.
        # Keep its original files for repair, but never re-activate that target.
        safe_previous = not old_address or prior["configured"]
        if address == old_address and ((address and was_active) or (not address and prior["skipped"] and not was_active)):
            return prior
        lines = [line for line in (old_config or b"").decode().splitlines()
                 if not line.startswith("REACHY_MINI_IP=")]
        config = ("\n".join(lines + ["REACHY_MINI_IP=" + address]) + "\n").encode()
        state = (json.dumps({"version": 1, "choice": choice, "address": address}) + "\n").encode()
        callback_attempted = service_attempted = False
        try:
            self._write(self.config_path, config, 0o644)
            self._write(self.state_path, state, 0o600)
            if address or was_active:
                service_attempted = True
                self._service("restart" if address else "stop")
            callback_attempted = True
            self.on_change(address)
        except Exception as error:
            failures = []
            for path, data, mode in ((self.config_path, old_config, 0o644), (self.state_path, old_state, 0o600)):
                try:
                    self._write(path, data, mode)
                except Exception:
                    failures.append("configuration")
            if service_attempted:
                try:
                    self._service("restart" if was_active and safe_previous else "stop")
                except Exception:
                    failures.append("bridge")
            if callback_attempted:
                try:
                    self.on_change(old_address if safe_previous else "")
                except Exception:
                    failures.append("UI connection")
            if failures:
                raise RuntimeError("Reachy change failed and rollback needs attention: " + ", ".join(failures)) from error
            if not safe_previous:
                raise RuntimeError("Reachy change failed; previous configuration files were restored, but the unpinned or excluded robot remains disconnected. Reconnect using set-reachy-ip.sh <verified-robot-address> on the Orin.") from error
            raise RuntimeError("Reachy change failed; the previous configuration and bridge state were restored.") from error
        return self.snapshot()

    def configure(self, address):
        with self._changing():
            self._address(self._read(self.config_path))
            self._choice(self._read(self.state_path))
            if not self.prepared_checker():
                raise RuntimeError("Reachy support is not prepared. Run the installer's Reachy setup, then retry.")
            if time.monotonic() - self._last_configure < self.configure_cooldown:
                raise RuntimeError("Wait a few seconds before retrying Reachy setup.")
            self._last_configure = time.monotonic()
            pinned = self._pin(address)
            self._verify(pinned)
            result = self._commit(pinned, "configured")
        result["busy"] = False
        return result

    def skip(self):
        with self._changing():
            result = self._commit("", "skipped")
        result["busy"] = False
        return result


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    commands = parser.add_subparsers(dest="command", required=True)
    for verb in ("status", "discover", "connect", "skip"):
        sub = commands.add_parser(verb, parents=[common])
        if verb == "connect":
            sub.add_argument("address")
    args = parser.parse_args(argv)
    try:
        manager = ReachySetup(getattr(args, "config", "/opt/live-vision-cosmos-demo/reachy.env"))
        result = (manager.configure(args.address) if args.command == "connect" else
                  manager.snapshot() if args.command == "status" else getattr(manager, args.command)())
    except (ValueError, RuntimeError, OSError) as error:
        print(json.dumps({"status": "error", "message": str(error)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
