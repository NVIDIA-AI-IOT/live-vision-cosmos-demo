# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Reachy setup failure contracts, with no robot, network scan or systemd access."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location("reachy_setup", Path(__file__).resolve().parents[1] / "scripts/reachy_setup.py")
reachy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reachy)


def status(name="Reachy test", state="running"):
    return {"robot_name": name, "state": state, "wireless_version": True,
            "backend_status": {"ready": True}, "version": "test"}


def advert(ip="192.168.100.21", hostname="reachy-test.local", service="_reachy-mini._tcp", port="8000"):
    return f"=;eth0;IPv4;An arbitrary service name;{service};local;{hostname};{ip};{port};\"model=Reachy Mini\"\n"


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "reachy.env"
        self.active = False
        self.actions = []
        self.fail_actions = []
        self.changed = []
        self.fetch = Mock(return_value=status())
        self.resolve = Mock(return_value=["192.168.100.21"])
        self.browse = Mock(return_value=advert())
        self.manager = self.make()

    def run_service(self, action):
        self.actions.append(action)
        if self.fail_actions and self.fail_actions.pop(0):
            raise RuntimeError("fixture service failure")
        self.active = action == "restart"

    def make(self, **changes):
        args = dict(on_change=self.changed.append, status_fetcher=self.fetch,
                    resolver=self.resolve, discovery_runner=self.browse,
                    service_runner=self.run_service, service_state=lambda: self.active,
                    prepared_checker=lambda: True, discovery_cooldown=0, configure_cooldown=0)
        args.update(changes)
        return reachy.ReachySetup(self.config, **args)

    def test_snapshot_is_offline_and_rereads_external_config(self):
        self.assertEqual(self.manager.snapshot()["status"], "not_configured")
        self.config.write_text("REACHY_MINI_IP=192.168.100.22\n")
        self.assertEqual(self.manager.snapshot()["address"], "192.168.100.22")
        self.fetch.assert_not_called()
        self.resolve.assert_not_called()
        self.assertEqual(self.actions, [])

    def test_configure_pins_hostname_writes_private_choice_and_restarts_only_bridge(self):
        result = self.manager.configure("reachy-test.local")
        self.assertEqual(result["address"], "192.168.100.21")
        self.assertTrue(result["configured"])
        self.assertFalse(result["busy"])
        self.assertEqual(self.config.read_text(), "REACHY_MINI_IP=192.168.100.21\n")
        self.assertEqual(self.changed, ["192.168.100.21"])
        self.assertEqual(self.actions, ["restart"])
        self.assertEqual(self.manager.state_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.manager.state_path.read_text())["choice"], "configured")

    def test_snapshot_blocks_saved_hostname_or_excluded_ip_without_network(self):
        manager = self.make(blocked_addresses=["192.168.100.22"])
        for saved in ("legacy-robot.local", "192.168.100.22"):
            with self.subTest(saved=saved):
                self.config.write_text("REACHY_MINI_IP=" + saved + "\n")
                result = manager.snapshot()
                self.assertEqual(result["status"], "error")
                self.assertFalse(result["configured"])
                self.assertEqual(result["address"], "")
                self.assertIn("set-reachy-ip.sh <verified-robot-address>", result["message"])
        self.fetch.assert_not_called()
        self.resolve.assert_not_called()
        self.assertEqual(self.actions, [])

    def test_explicit_connect_migrates_saved_legacy_hostname(self):
        self.config.write_text("# retained\nREACHY_MINI_IP=legacy-robot.local\n")
        self.assertEqual(self.manager.snapshot()["status"], "error")
        result = self.manager.configure("verified-robot.local")
        self.assertTrue(result["configured"])
        self.assertEqual(result["address"], "192.168.100.21")
        self.resolve.assert_called_once_with("verified-robot.local", 2.0)
        self.fetch.assert_called_once_with("192.168.100.21", 2.0)
        self.assertEqual(self.config.read_text(), "# retained\nREACHY_MINI_IP=192.168.100.21\n")

    def test_failed_migration_never_reactivates_unsafe_saved_target(self):
        for saved in ("legacy-robot.local", "192.168.100.22"):
            with self.subTest(saved=saved):
                self.config.write_text("REACHY_MINI_IP=" + saved + "\n")
                before = self.config.read_bytes()
                self.active = True
                self.actions.clear()
                callback = Mock(side_effect=[RuntimeError("UI error"), None])
                manager = self.make(on_change=callback, blocked_addresses=["192.168.100.22"])
                with self.assertRaisesRegex(RuntimeError, "robot remains disconnected"):
                    manager.configure("192.168.100.21")
                self.assertEqual(self.config.read_bytes(), before)
                self.assertFalse(self.active)
                self.assertEqual(self.actions, ["restart", "stop"])
                self.assertEqual([call.args[0] for call in callback.call_args_list], ["192.168.100.21", ""])

    def test_same_address_does_not_restart_active_bridge(self):
        self.manager.configure("192.168.100.21")
        self.manager.configure("192.168.100.21")
        self.assertEqual(self.actions, ["restart"])
        self.assertEqual(self.changed, ["192.168.100.21"])

    def test_skip_is_persistent_and_idempotent(self):
        self.manager.configure("192.168.100.21")
        result = self.manager.skip()
        self.assertTrue(result["skipped"])
        self.assertFalse(result["configured"])
        self.assertEqual(self.actions, ["restart", "stop"])
        self.assertEqual(self.changed, ["192.168.100.21", ""])
        self.manager.skip()
        self.assertEqual(self.actions, ["restart", "stop"])
        self.assertTrue(self.make().snapshot()["skipped"])

    def test_skip_works_without_prepared_bridge_if_already_inactive(self):
        manager = self.make(prepared_checker=lambda: False)
        self.assertTrue(manager.skip()["skipped"])
        self.assertEqual(self.actions, [])

    def test_discovery_only_probes_reachy_advertisements_fixed_port(self):
        self.browse.return_value = (advert() + advert(service="_http._tcp", ip="192.168.100.90")
                                   + advert(port="22", ip="192.168.100.91") + advert(ip="8.8.8.8")
                                   + advert(ip="127.0.0.1") + advert())
        result = self.manager.discover()
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["devices"], [{"name": "Reachy test", "address": "192.168.100.21", "hostname": "reachy-test.local"}])
        self.fetch.assert_called_once_with("192.168.100.21", 2.0)
        self.resolve.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_multiple_discovered_robots_are_not_auto_selected(self):
        self.browse.return_value = advert() + advert(ip="192.168.100.22", hostname="second.local")
        result = self.manager.discover()
        self.assertEqual(len(result["devices"]), 2)
        self.assertFalse(self.config.exists())
        self.assertEqual(self.actions, [])

    def test_discovery_rejects_arbitrary_json_even_for_reachy_service_name(self):
        self.fetch.return_value = {"status": "ready"}
        self.assertEqual(self.manager.discover()["devices"], [])

    def test_blocked_discovery_has_manual_recovery_and_does_not_change_choice(self):
        self.manager.skip()
        self.browse.side_effect = RuntimeError("Multicast blocked; enter the robot's LAN address.")
        result = self.manager.discover()
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("LAN address", result["message"])
        self.assertTrue(self.manager.snapshot()["skipped"])
        self.fetch.assert_not_called()

    def test_discovery_limits_candidates_and_retry_frequency(self):
        self.browse.return_value = "".join(advert(ip=f"192.168.100.{i}") for i in range(20, 50))
        manager = self.make(discovery_cooldown=10)
        self.assertEqual(len(manager.discover()["devices"]), reachy.MAX_DEVICES)
        self.assertEqual(self.fetch.call_count, reachy.MAX_DEVICES)
        self.assertEqual(manager.discover()["status"], "cooldown")
        self.assertEqual(self.browse.call_count, 1)

    def test_discovery_oversized_output_is_rejected_before_probes(self):
        self.browse.return_value = "x" * (reachy.MAX_DISCOVERY + 1)
        self.assertEqual(self.manager.discover()["status"], "unavailable")
        self.fetch.assert_not_called()

    def test_invalid_targets_never_resolve_probe_write_or_restart(self):
        targets = ("8.8.8.8", "127.0.0.1", "169.254.1.2", "224.0.0.1", "192.0.2.1",
                   "100.64.0.1", "0.0.0.0", "::1", "192.168.001.2", "127.1", "2130706433",
                   "0x7f000001", "http://192.168.100.21", "192.168.100.21:8000",
                   "192.168.100.21/path", "localhost", "user@robot.local", "robot.local\n", "")
        for target in targets:
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.manager.configure(target)
        self.resolve.assert_not_called()
        self.fetch.assert_not_called()
        self.assertFalse(self.config.exists())
        self.assertEqual(self.actions, [])

    def test_hostname_rejects_public_mixed_or_multiple_addresses(self):
        for results in (["8.8.8.8"], ["192.168.100.21", "8.8.8.8"],
                        ["192.168.100.21", "192.168.100.22"], [], ["::1"]):
            self.resolve.return_value = results
            with self.subTest(results=results), self.assertRaises(ValueError):
                self.manager.configure("robot.local")
        self.fetch.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_explicit_exclusions_are_never_contacted(self):
        manager = self.make(blocked_addresses=["192.168.100.21"])
        self.assertEqual(manager.discover()["devices"], [])
        with self.assertRaises(ValueError):
            manager.configure("192.168.100.21")
        with self.assertRaises(ValueError):
            manager.configure("robot.local")
        self.fetch.assert_not_called()

    def test_failed_validation_keeps_prior_config_and_service_untouched(self):
        self.config.write_text("# prior\nREACHY_MINI_IP=192.168.100.22\n")
        original = self.config.read_bytes()
        self.active = True
        self.fetch.side_effect = RuntimeError("daemon unavailable")
        with self.assertRaises(RuntimeError):
            self.manager.configure("192.168.100.21")
        self.assertEqual(self.config.read_bytes(), original)
        self.assertTrue(self.active)
        self.assertEqual(self.actions, [])

    def test_restart_failure_rolls_back_prior_config_choice_and_active_service(self):
        self.manager.configure("192.168.100.22")
        before = self.config.read_bytes(), self.manager.state_path.read_bytes()
        self.actions.clear()
        self.fail_actions = [True, False]
        with self.assertRaisesRegex(RuntimeError, "were restored"):
            self.manager.configure("192.168.100.21")
        self.assertEqual((self.config.read_bytes(), self.manager.state_path.read_bytes()), before)
        self.assertEqual(self.actions, ["restart", "restart"])
        self.assertTrue(self.active)

    def test_failed_first_restart_restores_missing_files_and_inactive_service(self):
        self.fail_actions = [True, False]
        with self.assertRaisesRegex(RuntimeError, "were restored"):
            self.manager.configure("192.168.100.21")
        self.assertFalse(self.config.exists())
        self.assertFalse(self.manager.state_path.exists())
        self.assertEqual(self.actions, ["restart", "stop"])
        self.assertFalse(self.active)

    def test_callback_failure_rolls_back_callback_and_service(self):
        self.manager.configure("192.168.100.22")
        callback = Mock(side_effect=[RuntimeError("UI error"), None])
        self.manager.on_change = callback
        with self.assertRaisesRegex(RuntimeError, "were restored"):
            self.manager.configure("192.168.100.21")
        self.assertEqual([c.args[0] for c in callback.call_args_list], ["192.168.100.21", "192.168.100.22"])
        self.assertEqual(self.manager.snapshot()["address"], "192.168.100.22")

    def test_failed_rollback_is_reported_as_needing_attention(self):
        self.fail_actions = [True, True]
        with self.assertRaisesRegex(RuntimeError, "rollback needs attention: bridge"):
            self.manager.configure("192.168.100.21")

    def test_skip_failure_restores_configured_robot(self):
        self.manager.configure("192.168.100.21")
        self.fail_actions = [True, False]
        with self.assertRaisesRegex(RuntimeError, "were restored"):
            self.manager.skip()
        self.assertTrue(self.manager.snapshot()["configured"])
        self.assertTrue(self.active)

    def test_simultaneous_changes_are_rejected_across_manager_instances(self):
        second = self.make()
        with self.manager._changing():
            self.assertTrue(self.manager.snapshot()["busy"])
            self.assertTrue(second.snapshot()["busy"])
            with self.assertRaisesRegex(RuntimeError, "already changing"):
                self.manager.skip()
            with self.assertRaisesRegex(RuntimeError, "Another Reachy"):
                second.skip()
        self.assertFalse(self.manager.snapshot()["busy"])
        self.assertFalse(second.snapshot()["busy"])

    def test_unprepared_install_does_not_start_network_or_service_work(self):
        manager = self.make(prepared_checker=lambda: False)
        self.assertFalse(manager.snapshot()["bridge_prepared"])
        with self.assertRaisesRegex(RuntimeError, "not prepared"):
            manager.configure("robot.local")
        self.resolve.assert_not_called()
        self.fetch.assert_not_called()

    def test_symlink_config_is_not_modified(self):
        target = self.config.with_name("unrelated")
        target.write_text("unrelated data")
        self.config.symlink_to(target)
        with self.assertRaises(ValueError):
            self.manager.skip()
        self.assertEqual(target.read_text(), "unrelated data")

    def test_malformed_optional_files_do_not_break_snapshot_or_get_overwritten(self):
        for config, state in ((b"REACHY_MINI_IP=http://invalid\n", None),
                              (b"REACHY_MINI_IP=\n", b"{not json"),
                              (b"REACHY_MINI_IP=\n", b"{}"),
                              (b"REACHY_MINI_IP=\n", b'{"version":1,"choice":[],"address":""}'),
                              (b"REACHY_MINI_IP=\n", b'{"version":1,"choice":"configured","address":"8.8.8.8"}'),
                              (b"\xff", None)):
            with self.subTest(config=config, state=state):
                self.config.write_bytes(config)
                self.manager.state_path.unlink(missing_ok=True)
                if state is not None:
                    self.manager.state_path.write_bytes(state)
                snapshot = self.manager.snapshot()
                self.assertEqual(snapshot["status"], "error")
                self.assertFalse(snapshot["configured"])
                for operation in (self.manager.skip, lambda: self.manager.configure("192.168.100.21")):
                    with self.assertRaises((ValueError, UnicodeError)):
                        operation()
                self.assertEqual(self.config.read_bytes(), config)
                self.assertEqual(self.manager._read(self.manager.state_path), state)
        self.fetch.assert_not_called()
        self.assertEqual(self.actions, [])

    def test_unreadable_optional_config_snapshot_is_nonfatal(self):
        with patch.object(self.manager, "_read", side_effect=PermissionError):
            self.assertEqual(self.manager.snapshot()["status"], "error")
            with self.assertRaises(PermissionError):
                self.manager.skip()

    def test_environment_exclusions_apply_to_ui_and_cli_managers(self):
        with patch.dict(reachy.os.environ, {"REACHY_EXCLUDED_ADDRESSES": "192.168.100.21, 192.168.100.22"}):
            manager = self.make()
            self.assertEqual(manager.blocked_addresses, {"192.168.100.21", "192.168.100.22"})
            self.assertEqual(manager.discover()["devices"], [])
            with self.assertRaises(ValueError):
                manager.configure("192.168.100.21")
            self.assertEqual(self.make(blocked_addresses=[]).blocked_addresses, set())
        self.fetch.assert_not_called()

    def test_invalid_environment_exclusions_are_rejected(self):
        for value in ("8.8.8.8", "robot.local", "192.168.100.21,", "127.0.0.1"):
            with self.subTest(value=value), patch.dict(reachy.os.environ, {"REACHY_EXCLUDED_ADDRESSES": value}):
                with self.assertRaises(ValueError):
                    self.make()

    def test_cooldown_bounds_failed_configure_attempts(self):
        manager = self.make(configure_cooldown=2)
        self.fetch.side_effect = RuntimeError("offline")
        with self.assertRaises(RuntimeError):
            manager.configure("192.168.100.21")
        with self.assertRaisesRegex(RuntimeError, "Wait"):
            manager.configure("192.168.100.21")
        self.assertEqual(self.fetch.call_count, 1)


class BoundaryTests(unittest.TestCase):
    def test_schema_rejects_wrong_types_and_accepts_sleeping_robot(self):
        self.assertEqual(reachy.validate_status(status(state="stopped"))["state"], "stopped")
        for key, value in (("robot_name", "bad\nname"), ("state", []), ("state", "ready"),
                           ("wireless_version", 1), ("backend_status", [])):
            item = status(); item[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                reachy.validate_status(item)

    def test_service_commands_have_no_shell_and_fixed_unit(self):
        with patch.object(reachy.subprocess, "run", return_value=Mock(returncode=0)) as run:
            reachy.service_action("restart")
        self.assertEqual(run.call_args.args[0], ["sudo", "-n", "/usr/bin/systemctl", "restart", reachy.UNIT])
        self.assertNotIn("shell", run.call_args.kwargs)
        with self.assertRaises(ValueError):
            reachy.service_action("restart anything")

    def test_dns_uses_killable_fixed_code_process_and_timeout(self):
        with patch.object(reachy.subprocess, "run", side_effect=subprocess.TimeoutExpired("resolver", 2)) as run:
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                reachy.resolve_host("robot.local", 2)
        self.assertEqual(run.call_args.args[0][-1], "robot.local")
        self.assertEqual(run.call_args.kwargs["timeout"], 2)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_missing_avahi_is_actionable_without_scanning(self):
        with patch.object(reachy.subprocess, "Popen", side_effect=FileNotFoundError) as popen:
            with self.assertRaisesRegex(RuntimeError, "avahi-utils"):
                reachy.browse_services(6)
        self.assertEqual(popen.call_args.args[0], ["avahi-browse", "--resolve", "--parsable", "--terminate", "_reachy-mini._tcp"])

    def http_fixture(self, code=200, body=None, content_type="application/json", length=None):
        response = Mock(status=code)
        response.read.return_value = json.dumps(status()).encode() if body is None else body
        response.getheader.side_effect = lambda key, default=None: {"Content-Type": content_type, "Content-Length": length}.get(key, default)
        connection = Mock()
        connection.getresponse.return_value = response
        return connection, response

    def test_http_is_numeric_fixed_path_and_ignores_redirects(self):
        connection, response = self.http_fixture(code=302)
        with patch.object(reachy.http.client, "HTTPConnection", return_value=connection) as http:
            with self.assertRaises(RuntimeError):
                reachy.fetch_status("192.168.100.21", 2)
        http.assert_called_once_with("192.168.100.21", 8000, timeout=2)
        self.assertEqual(connection.request.call_args.args, ("GET", "/api/daemon/status"))
        response.read.assert_not_called()
        connection.close.assert_called_once()

    def test_http_rejects_oversized_non_json_or_invalid_status(self):
        for kwargs in ({"length": str(reachy.MAX_JSON + 1)}, {"body": b"x" * (reachy.MAX_JSON + 1)},
                       {"content_type": "text/html"}, {"body": b'{}'}):
            connection, _ = self.http_fixture(**kwargs)
            with self.subTest(kwargs=list(kwargs)), patch.object(reachy.http.client, "HTTPConnection", return_value=connection):
                with self.assertRaises(ValueError):
                    reachy.fetch_status("192.168.100.21", 2)

    def test_cli_accepts_config_before_or_after_verb(self):
        for args in (["--config", "/private/reachy.env", "status", "--json"],
                     ["status", "--config", "/private/reachy.env", "--json"]):
            with patch.object(reachy, "ReachySetup") as cls, patch("sys.stdout", new_callable=io.StringIO) as out:
                cls.return_value.snapshot.return_value = {"status": "not_configured"}
                self.assertEqual(reachy.main(args), 0)
                cls.assert_called_once_with("/private/reachy.env")
                self.assertEqual(json.loads(out.getvalue())["status"], "not_configured")


if __name__ == "__main__":
    unittest.main()
