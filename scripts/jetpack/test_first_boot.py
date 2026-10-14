# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Fixture-only checks for USB identity isolation and secret-safe root audits."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import first_boot


def usb(serial="fixture-new", product_id=0x7020, product="Linux for Tegra", ports=(), **extra):
    return {"idVendor": 0x0955, "idProduct": product_id,
            "USB Serial Number": serial, "USB Product Name": product,
            "IORegistryEntryChildren": [{"IORegistryEntryChildren": [
                {"IOCalloutDevice": port} for port in ports]}], **extra}


class USBProbeTests(unittest.TestCase):
    def test_no_nvidia_does_not_mistake_an_unrelated_modem_for_jetson(self):
        registry = [{"idVendor": 0x1234, "idProduct": 0x7020,
                     "IORegistryEntryChildren": [{"IOCalloutDevice": "/dev/cu.usbmodem-other"}]}]
        result = first_boot.classify_usb(registry, ["/dev/cu.usbmodem-other"])
        self.assertEqual(result["state"], "no_device")
        self.assertEqual(result["devices"], [])

    def test_usb_ancestry_correlates_console_without_guessing_its_name(self):
        registry = [usb(ports=["/dev/cu.usbmodem-unrelated-looking-name"]),
                    {"IOCalloutDevice": "/dev/cu.usbmodem-fixture-new"}]
        result = first_boot.classify_usb(registry, ["/dev/cu.usbmodem-unrelated-looking-name",
                                                    "/dev/cu.usbmodem-fixture-new"])
        self.assertEqual(result["state"], "serial_ready")
        self.assertEqual(result["selected"]["serial_ports"], ["/dev/cu.usbmodem-unrelated-looking-name"])
        self.assertFalse(result["boot_verified"])
        self.assertFalse(result["account_verified"])
        self.assertFalse(result["console_opened"])

    def test_two_boards_are_ambiguous_until_exact_serial_is_selected(self):
        registry = [usb("fixture-new", ports=["/dev/cu.usbmodem1"]),
                    usb("fixture-existing", ports=["/dev/cu.usbmodem2"])]
        ports = ["/dev/cu.usbmodem1", "/dev/cu.usbmodem2"]
        self.assertEqual(first_boot.classify_usb(registry, ports)["state"], "ambiguous")
        result = first_boot.classify_usb(registry, ports, "fixture-new")
        self.assertEqual(result["state"], "serial_ready")
        self.assertEqual(result["selected"]["serial_ports"], ["/dev/cu.usbmodem1"])
        mismatch = first_boot.classify_usb(registry, ports, "fixture-n")
        self.assertEqual(mismatch["state"], "different_device")
        self.assertIsNone(mismatch["selected"])

    def test_duplicate_serials_are_not_an_identity_match(self):
        result = first_boot.classify_usb([usb(), usb()], [], "fixture-new")
        self.assertEqual(result["state"], "ambiguous")
        self.assertIsNone(result["selected"])

    def test_nested_device_is_not_coalesced_with_same_serial_parent(self):
        parent = usb()
        parent["IORegistryEntryChildren"] = [usb()]
        self.assertEqual(first_boot.classify_usb([parent], [])["state"], "ambiguous")

    def test_interface_properties_do_not_create_an_extra_device(self):
        parent = usb()
        parent["IORegistryEntryChildren"] = [{"IOObjectClass": "IOUSBHostInterface",
            "idVendor": 0x0955, "idProduct": 0x7020,
            "IORegistryEntryChildren": [{"IOCalloutDevice": "/dev/cu.usbmodem1"}]}]
        result = first_boot.classify_usb([parent], ["/dev/cu.usbmodem1"])
        self.assertEqual(result["state"], "serial_ready")
        self.assertEqual(len(result["devices"]), 1)

    def test_acm_and_ncm_driver_properties_preserve_physical_device_identity(self):
        # Sanitized shape of the macOS IOService tree: the Apple driver nodes
        # repeat VID/PID but omit the interface's serial, location and number.
        parent = usb(IOObjectClass="IOUSBHostDevice", IORegistryEntryID=101, locationID=42)
        parent["IORegistryEntryChildren"] = []
        for index, driver in enumerate(("AppleUSBACMData", "AppleUSBNCMData")):
            descendant = ({"IOObjectClass": "IOSerialBSDClient", "IORegistryEntryID": 110,
                           "IOCalloutDevice": "/dev/cu.usbmodem-fixture"} if index == 0 else
                          {"IOObjectClass": "IOEthernetInterface", "IORegistryEntryID": 111})
            interface = {"IOObjectClass": "IOUSBHostInterface", "IORegistryEntryID": 102 + index,
                         "idVendor": 0x0955, "idProduct": 0x7020,
                         "USB Serial Number": "fixture-new", "locationID": 42,
                         "bInterfaceNumber": index, "IORegistryEntryChildren": [
                             {"IOObjectClass": driver, "IOClass": driver,
                              "IORegistryEntryID": 104 + index,
                              "idVendor": 0x0955, "idProduct": 0x7020,
                              "IOProviderClass": "IOUSBHostInterface",
                              "IORegistryEntryChildren": [descendant]}]}
            parent["IORegistryEntryChildren"].append(interface)
        result = first_boot.classify_usb([parent], ["/dev/cu.usbmodem-fixture"], "fixture-new")
        self.assertEqual(result["state"], "serial_ready")
        self.assertEqual(len(result["devices"]), 1)
        self.assertEqual(result["selected"]["serial_ports"], ["/dev/cu.usbmodem-fixture"])

    def test_physical_child_device_never_merges_despite_interface_like_properties(self):
        parent = usb(IOObjectClass="IOUSBHostDevice", IORegistryEntryID=201)
        child = usb(IOObjectClass="IOUSBHostDevice", IORegistryEntryID=202,
                    IOProviderClass="IOUSBHostInterface", bInterfaceNumber=0,
                    ports=["/dev/cu.usbmodem-child"])
        parent["IORegistryEntryChildren"] = [child]
        result = first_boot.classify_usb([parent], ["/dev/cu.usbmodem-child"], "fixture-new")
        self.assertEqual(result["state"], "ambiguous")
        self.assertEqual(len(result["devices"]), 2)
        self.assertEqual(result["devices"][0]["serial_ports"], [])
        self.assertEqual(result["devices"][1]["serial_ports"], ["/dev/cu.usbmodem-child"])

    def test_other_vendor_descendant_breaks_device_correlation(self):
        parent = usb()
        parent["IORegistryEntryChildren"] = [{"idVendor": 0x1234, "idProduct": 1,
            "IORegistryEntryChildren": [{"IOCalloutDevice": "/dev/cu.usbmodem1"}]}]
        result = first_boot.classify_usb([parent], ["/dev/cu.usbmodem1"])
        self.assertEqual(result["state"], "usb_detected_no_serial")
        self.assertEqual(result["selected"]["serial_ports"], [])

    def test_missing_node_and_multiple_ports_do_not_claim_one_console_ready(self):
        registry = [usb(ports=["/dev/cu.usbmodem1", "/dev/cu.usbmodem2"])]
        self.assertEqual(first_boot.classify_usb(registry, [])["state"], "usb_detected_no_serial")
        self.assertEqual(first_boot.classify_usb(registry, ["/dev/cu.usbmodem1", "/dev/cu.usbmodem2"])["state"], "ambiguous")

    def test_apx_is_recovery_but_unknown_pid_is_not(self):
        recovery = first_boot.classify_usb([usb(product_id=0xFFFF, product="APX")], [])
        self.assertEqual(recovery["state"], "recovery")
        unknown = first_boot.classify_usb([usb(product_id=0xFFFF, product="Unknown NVIDIA device")], [])
        self.assertEqual(unknown["state"], "unknown_device")
        self.assertFalse(unknown["boot_verified"])

    def test_invalid_callout_paths_are_never_selected(self):
        invalid = "/dev/cu.usbmodem1/../../tty"
        result = first_boot.classify_usb([usb(ports=[invalid])], [invalid])
        self.assertEqual(result["state"], "usb_detected_no_serial")

    def test_probe_runs_only_readonly_ioreg_and_does_not_open_device(self):
        result = subprocess.CompletedProcess(first_boot.IOREG_COMMAND, 0,
                                              plistlib.dumps([usb(ports=["/dev/cu.usbmodem1"])]), b"")
        with patch.object(first_boot.platform, "system", return_value="Darwin"), \
             patch.object(first_boot.subprocess, "run", return_value=result) as run, \
             patch.object(first_boot.glob, "glob", return_value=["/dev/cu.usbmodem1"]), \
             patch("socket.socket") as network, patch("builtins.open") as opened:
            self.assertEqual(first_boot.probe("fixture-new")["state"], "serial_ready")
        run.assert_called_once_with(first_boot.IOREG_COMMAND, check=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        network.assert_not_called()
        opened.assert_not_called()

    def test_probe_failure_does_not_claim_device_absence(self):
        with patch.object(first_boot.platform, "system", return_value="Darwin"), \
             patch.object(first_boot.subprocess, "run", side_effect=subprocess.TimeoutExpired("ioreg", 30)):
            with self.assertRaises(first_boot.DiagnosticError):
                first_boot.probe()


class OfflineAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "mounted-root"
        self.root.mkdir()
        for directory in ("etc/systemd/system", "home/demo-user", "var/lib/oem-config", "run"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        (self.root / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/bash\n"
            "demo-user:PASSWD-FIELD-SECRET:1000:1000:GECOS-SECRET:/home/demo-user:/bin/bash\n")
        self.set_shadow("$y$SYNTHETIC-HASH-SECRET")
        (self.root / "etc/nv_tegra_release").write_text("# R39 (release), REVISION: 2.1\n")
        (self.root / "etc/systemd/system/default.target").symlink_to("/lib/systemd/system/nv-oobe.target")

    def set_shadow(self, field):
        (self.root / "etc/shadow").write_text(f"demo-user:{field}:20000:0:99999:7:::\n")

    def test_custom_user_audit_redacts_both_password_fields_and_gecos(self):
        result = first_boot.audit_root(self.root, "demo-user")
        output = json.dumps(result)
        for secret in ("PASSWD-FIELD-SECRET", "SYNTHETIC-HASH-SECRET", "GECOS-SECRET", "$y$"):
            self.assertNotIn(secret, output)
        self.assertEqual(result["account"]["uid"], 1000)
        self.assertEqual(result["state"], "credentials_unverified")
        self.assertTrue(result["account"]["shadow"]["password_hash_present"])
        self.assertFalse(result["credentials_verified"])
        self.assertFalse(result["default_target"]["target_followed"])

    def test_locked_empty_missing_and_unread_shadow_are_distinct(self):
        self.set_shadow("!$y$LOCKED-SECRET")
        result = first_boot.audit_root(self.root, "demo-user")
        self.assertEqual(result["state"], "locked_account")
        self.assertTrue(result["account"]["shadow"]["password_hash_present"])
        self.assertNotIn("LOCKED-SECRET", json.dumps(result))
        self.set_shadow("")
        empty = first_boot.audit_root(self.root, "demo-user")
        self.assertTrue(empty["account"]["shadow"]["empty"])
        self.assertEqual(empty["state"], "credentials_unverified")
        (self.root / "etc/shadow").write_text("")
        self.assertFalse(first_boot.audit_root(self.root, "demo-user")["account"]["shadow"]["entry_exists"])
        (self.root / "etc/shadow").unlink()
        missing = first_boot.audit_root(self.root, "demo-user")
        self.assertEqual(missing["shadow_read"], "missing")
        self.assertIsNone(missing["account"]["shadow"]["entry_exists"])

    def test_missing_user_does_not_imply_password_was_wrong(self):
        result = first_boot.audit_root(self.root, "different-user")
        self.assertEqual(result["state"], "setup_incomplete_or_wrong_username")
        self.assertIsNone(result["account"])

    def test_unreadable_passwd_is_not_classified_as_missing_account(self):
        (self.root / "etc/passwd").unlink()
        self.assertEqual(first_boot.audit_root(self.root, "demo-user")["state"], "audit_incomplete")

    def test_permission_denied_shadow_is_unknown_never_empty(self):
        real_read = first_boot.MountedRoot.read

        def denied(mounted, name):
            return (None, "permission_denied") if name == "/etc/shadow" else real_read(mounted, name)

        with patch.object(first_boot.MountedRoot, "read", denied):
            result = first_boot.audit_root(self.root, "demo-user")
        shadow = result["account"]["shadow"]
        self.assertEqual(shadow["status"], "permission_denied")
        self.assertIsNone(shadow["entry_exists"])
        self.assertNotIn("empty", shadow)
        self.assertEqual(result["state"], "audit_incomplete")
        self.assertIn("remains unknown", result["guidance"])

    def test_malformed_requested_passwd_record_is_not_a_missing_account(self):
        malformed = (
            "demo-user:SECRET:notnumeric:1000:GECOS-SECRET:/home/demo-user:/bin/bash\n",
            "demo-user:SECRET:1000:notnumeric:GECOS-SECRET:/home/demo-user:/bin/bash\n",
            "demo-user:SECRET:1000\n",
        )
        for record in malformed:
            with self.subTest(record_length=len(record)):
                (self.root / "etc/passwd").write_text(record)
                result = first_boot.audit_root(self.root, "demo-user")
                self.assertEqual(result["state"], "invalid_account_database")
                self.assertEqual(result["passwd_invalid_row_count"], 1)
                self.assertNotIn("SECRET", json.dumps(result))

    def test_malformed_unknown_passwd_content_prevents_claim_of_absence(self):
        (self.root / "etc/passwd").write_text("unparseable-private-record\n")
        result = first_boot.audit_root(self.root, "demo-user")
        self.assertEqual(result["state"], "invalid_account_database")
        self.assertNotIn("unparseable-private-record", json.dumps(result))

    def test_blank_passwd_lines_are_ignored(self):
        passwd = self.root / "etc/passwd"
        passwd.write_text("\n  \n" + passwd.read_text() + "\n")
        result = first_boot.audit_root(self.root, "demo-user")
        self.assertEqual(result["passwd_invalid_row_count"], 0)
        self.assertEqual(result["state"], "credentials_unverified")

    def test_malformed_or_duplicate_shadow_entry_stays_unknown(self):
        for content in ("demo-user:SHADOW-SECRET\n",
                        "demo-user:SHADOW-SECRET:20000:0:99999:7:::\n" * 2):
            (self.root / "etc/shadow").write_text(content)
            result = first_boot.audit_root(self.root, "demo-user")
            self.assertEqual(result["state"], "audit_incomplete")
            self.assertIsNone(result["account"]["shadow"]["entry_exists"])
            self.assertNotIn("SHADOW-SECRET", json.dumps(result))

    def test_release_only_emits_parsed_version_not_arbitrary_file_contents(self):
        (self.root / "etc/nv_tegra_release").write_text("UNEXPECTED-SECRET-FILE-CONTENT")
        result = first_boot.audit_root(self.root, "demo-user")
        self.assertIsNone(result["nv_tegra_release"]["release"])
        self.assertNotIn("UNEXPECTED-SECRET", json.dumps(result))

    def test_absolute_and_relative_symlink_escape_is_refused(self):
        outside = Path(self.temp.name) / "host-shadow"
        outside.write_text("demo-user:HOST-SECRET:20000:0:99999:7:::\n")
        shadow = self.root / "etc/shadow"
        for target in (outside, Path("../../host-shadow")):
            shadow.unlink()
            shadow.symlink_to(target)
            result = first_boot.audit_root(self.root, "demo-user")
            self.assertEqual(result["shadow_read"], "symlink_or_non_directory_refused")
            self.assertNotIn("HOST-SECRET", json.dumps(result))

    def test_symlink_in_parent_directory_is_not_followed(self):
        elsewhere = Path(self.temp.name) / "host-home"
        elsewhere.mkdir()
        (elsewhere / "demo-user").mkdir()
        (self.root / "home/demo-user").rmdir()
        (self.root / "home").rmdir()
        (self.root / "home").symlink_to(elsewhere)
        result = first_boot.audit_root(self.root, "demo-user")
        self.assertEqual(result["account"]["home_metadata"]["status"], "symlink_or_non_directory_refused")

    def test_guest_traversal_and_invalid_usernames_are_refused(self):
        with first_boot.MountedRoot(self.root) as mounted:
            for name in ("/../../etc/shadow", "/home/../etc/shadow", "etc/shadow", "/etc/sha\0dow"):
                self.assertEqual(mounted.read(name)[1], "invalid_guest_path")
        for name in ("../demo-user", "demo/user", "user\nname", "user; command", ""):
            with self.assertRaises(first_boot.DiagnosticError):
                first_boot.audit_root(self.root, name)

    def test_auditing_host_root_or_alias_to_it_is_refused(self):
        alias = Path(self.temp.name) / "host-root"
        alias.symlink_to("/")
        for root in ("/", alias):
            with self.assertRaises(first_boot.DiagnosticError):
                first_boot.audit_root(root, "demo-user")

    def test_fifo_and_oversize_file_are_refused_without_hanging(self):
        shadow = self.root / "etc/shadow"
        shadow.unlink()
        os.mkfifo(shadow)
        self.assertEqual(first_boot.audit_root(self.root, "demo-user")["shadow_read"], "non_regular_file_refused")
        shadow.unlink()
        shadow.write_bytes(b"x" * (first_boot.MAX_FILE_BYTES + 1))
        self.assertEqual(first_boot.audit_root(self.root, "demo-user")["shadow_read"], "file_too_large")

    def test_duplicate_account_or_shadow_does_not_claim_healthy_credentials(self):
        passwd = self.root / "etc/passwd"
        passwd.write_text(passwd.read_text() + "demo-user:x:1001:1001::/home/demo-user:/bin/bash\n")
        self.assertEqual(first_boot.audit_root(self.root, "demo-user")["state"], "invalid_account_database")

    def test_cli_json_root_audit_has_no_network_subprocess_or_writes(self):
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file() and not path.is_symlink()}
        output = io.StringIO()
        with patch("socket.socket") as network, patch.object(first_boot.subprocess, "run") as commands, redirect_stdout(output):
            code = first_boot.main(["audit-root", "--root", str(self.root), "--user", "demo-user", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "credentials_unverified")
        network.assert_not_called()
        commands.assert_not_called()
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file() and not path.is_symlink()}
        self.assertEqual(before, after)

    def test_cli_operational_error_is_nonzero_and_json(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = first_boot.main(["audit-root", "--root", "/", "--user", "demo-user", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["state"], "diagnostic_error")


if __name__ == "__main__":
    unittest.main()
