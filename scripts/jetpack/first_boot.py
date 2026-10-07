#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Read-only Jetson USB discovery and offline first-boot account diagnostics.

Neither command opens a serial console, tries a password, accesses the network,
mounts a filesystem, or changes a device. Run audit-root inside the Linux repair
VM against the SD root already mounted read-only. sudo may be needed to read
shadow; denied access remains unknown, never an empty password. USB presence
does not prove successful setup or login.
"""
import argparse
import errno
import glob
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import stat
import subprocess
import sys


NVIDIA_VENDOR_ID = 0x0955
LINUX_GADGET_PRODUCT_ID = 0x7020
MAX_FILE_BYTES = 1024 * 1024
USERNAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\$?\Z")
SERIAL_PORT = re.compile(r"/dev/cu\.usb[A-Za-z0-9_.-]+\Z")
IOREG_COMMAND = ["/usr/sbin/ioreg", "-a", "-p", "IOService", "-l"]


class DiagnosticError(Exception):
    """A diagnostic could not safely inspect the requested input."""


def text_value(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").rstrip("\0")
    return value if isinstance(value, str) else None


def number_value(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16 if value.lower().startswith("0x") else 10)
        except ValueError:
            pass
    return None


def classify_usb(registry, available_ports, expected_serial=None):
    """Correlate callout ports by USB ancestor, never by serial-name guessing.

    registry is an ioreg IOService plist tree; available_ports contains existing
    /dev/cu.usb* paths. Keeping this pure makes fixtures sufficient for testing.
    """
    devices = []
    by_identity = {}
    available = {port for port in available_ports if SERIAL_PORT.fullmatch(port)}

    def walk(node, parent_device=None):
        if not isinstance(node, dict):
            return
        device = parent_device
        vendor = number_value(node.get("idVendor"))
        product_id = number_value(node.get("idProduct"))
        if vendor is not None and product_id is not None:
            # Crossing another USB device is an identity boundary, even when
            # it belongs to another vendor underneath an NVIDIA USB hub.
            device = None
            if vendor == NVIDIA_VENDOR_ID:
                serial = text_value(node.get("USB Serial Number"))
                if serial is None:
                    serial = text_value(node.get("kUSBSerialNumberString"))
                product = text_value(node.get("USB Product Name")) or ""
                location = number_value(node.get("locationID"))
                entry_id = number_value(node.get("IORegistryEntryID"))
                # Interfaces and their drivers repeat vendor/product. macOS
                # AppleUSBACMData and AppleUSBNCMData omit serial/location and
                # bInterfaceNumber, but identify IOUSBHostInterface as their
                # provider. Preserve their physical ancestor's identity.
                # A real child USB device always starts a separate identity,
                # even if it advertises the same serial as its parent.
                node_class = str(node.get("IOObjectClass", node.get("IOClass", "")))
                physical_device = node_class in {"IOUSBHostDevice", "IOUSBDevice"}
                interface = (not physical_device and
                             ("Interface" in node_class or "bInterfaceNumber" in node or
                              node.get("IOProviderClass") in {"IOUSBHostInterface", "IOUSBInterface"}))
                if (interface and parent_device is not None and
                        parent_device["product_id"] == product_id and
                        serial in (None, parent_device["serial"]) and
                        location in (None, parent_device["location_id"])):
                    device = parent_device
                else:
                    key = ("entry", entry_id) if entry_id is not None else ("node", id(node))
                    device = by_identity.get(key)
                    if device is None:
                        device = {"vendor_id": vendor, "product_id": product_id,
                                  "product": product, "serial": serial,
                                  "location_id": location, "serial_ports": []}
                        by_identity[key] = device
                        devices.append(device)
        port = text_value(node.get("IOCalloutDevice"))
        if device is not None and port in available and port not in device["serial_ports"]:
            device["serial_ports"].append(port)
        for child in node.get("IORegistryEntryChildren", []):
            walk(child, device)

    roots = registry if isinstance(registry, list) else [registry]
    for root in roots:
        walk(root)
    for device in devices:
        device["serial_ports"].sort()
        # APX is NVIDIA's recovery USB product name. Do not guess recovery
        # from unrecognized product IDs or the absence of a serial console.
        if device["product"].strip().upper() == "APX":
            device["mode"] = "recovery"
        elif device["product_id"] == LINUX_GADGET_PRODUCT_ID:
            device["mode"] = "linux_gadget"
        else:
            device["mode"] = "unknown"

    matches = devices if expected_serial is None else [
        device for device in devices if device["serial"] == expected_serial]
    selected = matches[0] if len(matches) == 1 else None
    if not devices:
        state = "no_device"
    elif expected_serial is not None and not matches:
        state = "different_device"
    elif len(matches) != 1:
        state = "ambiguous"
    elif selected["mode"] == "recovery":
        state = "recovery"
    elif len(selected["serial_ports"]) > 1:
        state = "ambiguous"
    elif selected["serial_ports"]:
        state = "serial_ready"
    elif selected["mode"] == "linux_gadget":
        state = "usb_detected_no_serial"
    else:
        state = "unknown_device"

    guidance = {
        "no_device": "No NVIDIA USB device found. Check power and the USB data cable; this does not establish a firmware fault.",
        "different_device": "No exact match for the expected USB serial. Do not select another device or fall back to a remembered network address.",
        "ambiguous": "Multiple matching devices or console ports. Verify the intended physical device before opening any console.",
        "recovery": "The selected device advertises APX recovery mode. This is not a Linux login console.",
        "serial_ready": "A USB serial device node is available. No console was opened; boot, setup and login remain unverified.",
        "usb_detected_no_serial": "The Linux USB gadget is visible, but no associated serial device node is available yet.",
        "unknown_device": "An NVIDIA USB device is present, but its mode is not recognized. No firmware or boot diagnosis is implied.",
    }
    return {"diagnostic": "usb_probe_read_only", "state": state,
            "expected_serial": expected_serial, "devices": devices, "selected": selected,
            "guidance": guidance[state], "boot_verified": False,
            "account_verified": False, "network_probed": False, "console_opened": False}


def probe(expected_serial=None):
    if platform.system() != "Darwin":
        raise DiagnosticError("USB probe currently supports macOS only; audit-root also works on Linux.")
    try:
        result = subprocess.run(IOREG_COMMAND, check=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=30)
        registry = plistlib.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, plistlib.InvalidFileException):
        raise DiagnosticError("Could not read the macOS USB registry; no devices were changed.") from None
    return classify_usb(registry, glob.glob("/dev/cu.usb*"), expected_serial)


def safe_parts(name):
    """Validate guest paths before any descriptor-relative filesystem access."""
    if (not isinstance(name, str) or not name.startswith("/") or len(name) > 4096 or
            any(ord(char) < 32 for char in name) or ".." in name.split("/")):
        raise DiagnosticError("invalid_guest_path")
    return [part for part in name.split("/") if part not in ("", ".")]


def error_status(error):
    if isinstance(error, DiagnosticError):
        return str(error)
    if isinstance(error, FileNotFoundError):
        return "missing"
    if isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, OSError) and error.errno in (errno.ELOOP, errno.ENOTDIR):
        return "symlink_or_non_directory_refused"
    return "read_error"


class MountedRoot:
    """Read files beneath a fixed root descriptor without following symlinks.

    Refusing symlinks (including absolute Linux symlinks) avoids following one
    into the audit host. O_NOFOLLOW on every open also prevents path-swap races.
    Only a final symlink's metadata/target may be reported; it is never followed.
    """
    def __init__(self, root):
        resolved = Path(root).resolve(strict=True)
        if resolved == Path("/") or not resolved.is_dir():
            raise DiagnosticError("Pass an offline mounted root directory, never the auditing host root (/).")
        self.fd = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        os.close(self.fd)

    def _parent(self, name):
        parts = safe_parts(name)
        if not parts:
            raise DiagnosticError("invalid_guest_path")
        fd = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = following
            return fd, parts[-1]
        except BaseException:
            os.close(fd)
            raise

    def read(self, name):
        parent = fd = None
        try:
            parent, base = self._parent(name)
            info = os.stat(base, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise DiagnosticError("symlink_or_non_directory_refused")
            if not stat.S_ISREG(info.st_mode):
                raise DiagnosticError("non_regular_file_refused")
            fd = os.open(base, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise DiagnosticError("non_regular_file_refused")
            with os.fdopen(fd, "rb") as stream:
                fd = None
                data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                return None, "file_too_large"
            return data.decode("utf-8", "replace"), "read"
        except (OSError, DiagnosticError) as error:
            return None, error_status(error)
        finally:
            if fd is not None:
                os.close(fd)
            if parent is not None:
                os.close(parent)

    def inspect(self, name, show_link=False):
        parent = None
        try:
            parent, base = self._parent(name)
            info = os.stat(base, dir_fd=parent, follow_symlinks=False)
            kind = ("symlink" if stat.S_ISLNK(info.st_mode) else
                    "directory" if stat.S_ISDIR(info.st_mode) else
                    "file" if stat.S_ISREG(info.st_mode) else "other")
            result = {"exists": True, "uid": info.st_uid, "gid": info.st_gid,
                      "mode": oct(stat.S_IMODE(info.st_mode)), "kind": kind}
            if kind == "symlink":
                result["target_followed"] = False
                if show_link:
                    result["link_target"] = os.readlink(base, dir_fd=parent)
            return result
        except (OSError, DiagnosticError) as error:
            status = error_status(error)
            return {"exists": False if status == "missing" else None, "status": status}
        finally:
            if parent is not None:
                os.close(parent)


def shadow_status(field):
    remainder = field.lstrip("!")
    return {"entry_exists": True, "locked": field.startswith(("!", "*")),
            "empty": field == "", "password_field_present": bool(field),
            "password_hash_present": bool(remainder) and not remainder.startswith("*"),
            "credentials_verified": False}


def audit_root(root, username):
    if not isinstance(username, str) or not USERNAME.fullmatch(username):
        raise DiagnosticError("Pass one Linux username, without whitespace, slashes or shell syntax.")
    with MountedRoot(root) as mounted:
        passwd, passwd_read = mounted.read("/etc/passwd")
        shadow, shadow_read = mounted.read("/etc/shadow")
        accounts = []
        invalid_rows = 0
        requested_record_invalid = False
        for line in (passwd or "").splitlines():
            if not line.strip():
                continue
            fields = line.split(":")
            if len(fields) != 7:
                invalid_rows += 1
                requested_record_invalid |= fields[0] == username
                continue
            name, _password, uid, gid, _gecos, home, shell = fields
            if not uid.isdecimal() or not gid.isdecimal():
                invalid_rows += 1
                requested_record_invalid |= name == username
                continue
            if name != username:
                continue
            # Never include the passwd password field or GECOS information.
            account = {"name": name, "uid": int(uid), "gid": int(gid),
                       "home": home, "shell": shell,
                       "home_metadata": mounted.inspect(home),
                       "shadow": {"entry_exists": None, "status": shadow_read}}
            if shadow_read == "read":
                matches = [entry.split(":") for entry in shadow.splitlines()
                           if entry.split(":", 1)[0] == name]
                account["shadow"] = {"entry_exists": False}
                if len(matches) == 1 and len(matches[0]) == 9:
                    account["shadow"] = shadow_status(matches[0][1])
                elif matches:
                    account["shadow"] = {"entry_exists": None, "status": "malformed_or_duplicate_entry"}
            accounts.append(account)
        account = accounts[0] if len(accounts) == 1 else None
        if passwd_read != "read":
            state = "audit_incomplete"
        elif (len(accounts) > 1 or requested_record_invalid or
              (not account and invalid_rows)):
            state = "invalid_account_database"
        elif not account:
            state = "setup_incomplete_or_wrong_username"
        elif account["shadow"].get("entry_exists") is not True:
            state = "audit_incomplete"
        elif account["shadow"].get("locked"):
            state = "locked_account"
        else:
            state = "credentials_unverified"
        release, release_read = mounted.read("/etc/nv_tegra_release")
        release_match = re.match(r"# R([0-9]+) \(release\), REVISION: ([0-9.]+)", release or "")
        release_summary = ("R" + release_match[1] + ", REVISION: " + release_match[2]
                           if release_match else None)
        guidance = {
            "audit_incomplete": "Account or shadow data is unavailable, missing or malformed. Verify the offline mount, reported flags and read permissions; sudo inside the Linux repair VM may be needed. Credential state remains unknown. Do not infer account absence or an empty password.",
            "invalid_account_database": "Malformed or duplicate passwd records prevent a reliable account assessment. Preserve the image and inspect the account database before attempting a repair; do not infer that a new account is needed.",
            "setup_incomplete_or_wrong_username": "The requested account was not found. Confirm the chosen username and first-boot completion before recreating an account.",
            "locked_account": "The stored account password field is locked. Preserve the installation and arrange an explicit account repair; no password was tested.",
            "credentials_unverified": "The account exists, but login is unverified. Inspect the reported flags and try one controlled console login before selecting a repair.",
        }
        read_only = bool(os.fstatvfs(mounted.fd).f_flag & os.ST_RDONLY)
        return {"diagnostic": "offline_account_status_read_only", "state": state,
                "requested_user": username, "passwd_read": passwd_read,
                "passwd_invalid_row_count": invalid_rows, "shadow_read": shadow_read,
                "account": account, "root_filesystem_read_only": read_only,
                "default_target": mounted.inspect("/etc/systemd/system/default.target", show_link=True),
                "oem_config_run": mounted.inspect("/var/lib/oem-config/run"),
                "etc_nologin": mounted.inspect("/etc/nologin"),
                "run_nologin": mounted.inspect("/run/nologin"),
                "nv_tegra_release": {"status": release_read,
                                     "release": release_summary},
                "credentials_verified": False,
                "guidance": guidance[state],
                "limitations": "A stored password hash does not establish that a supplied password works. No password was tested. Offline /run state may be stale; boot and login require separate verification.",
                "mount_guidance": "Keep the SD root mounted read-only; this tool does not mount, unmount or repair it."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    usb = subcommands.add_parser("probe", help="Inspect macOS USB identities and associated serial nodes.")
    usb.add_argument("--serial", help="Select an exact NVIDIA USB serial; never fall back to another device.")
    usb.add_argument("--json", action="store_true", help="Print a structured diagnostic.")
    audit = subcommands.add_parser("audit-root", help="Inspect an offline SD root in the Linux repair VM; sudo may be needed for shadow.",
        description="Run inside the Linux repair VM against the SD root already mounted read-only. sudo may be needed to read shadow. Permission denied means unknown, never an empty password; this command never repairs an account.")
    audit.add_argument("--root", required=True, type=Path)
    audit.add_argument("--user", required=True)
    audit.add_argument("--json", action="store_true", help="Print a structured diagnostic without password hashes.")
    args = parser.parse_args(argv)
    try:
        result = probe(args.serial) if args.command == "probe" else audit_root(args.root, args.user)
    except (DiagnosticError, OSError) as error:
        # OSError paths may contain host details; emit a fixed public message.
        message = str(error) if isinstance(error, DiagnosticError) else "Requested root or diagnostic data is inaccessible."
        if args.json:
            print(json.dumps({"state": "diagnostic_error", "error": message}))
        else:
            print("Diagnostic stopped: " + message, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("State: " + result["state"])
        print(result["guidance"])
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
