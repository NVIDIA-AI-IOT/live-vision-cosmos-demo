# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Headless recovery tests: mocks and temporary PTYs, never Jetson I/O."""
from contextlib import redirect_stderr, redirect_stdout
import errno
import io
import os
import pty
import select
import subprocess
import termios
import threading
import time
import unittest
from unittest.mock import Mock, mock_open, patch

import headless_retry as retry


class Preconditions(unittest.TestCase):
    def test_account_reader_checks_local_uid_range_without_reporting_fields(self):
        root = "root:HASH-SECRET:0:0:PRIVATE:/root:/bin/bash\n"
        with patch.object(retry.Path, "open", mock_open(read_data=root)):
            self.assertFalse(retry.normal_accounts_exist())
        for uid in (1000, 1001, 65533):
            with patch.object(retry.Path, "open", mock_open(read_data=root +
                    f"chosen:SECRET:{uid}:{uid}:PRIVATE:/home/chosen:/bin/bash\n")):
                self.assertTrue(retry.normal_accounts_exist())
        with patch.object(retry.Path, "open", mock_open(read_data=root +
                "nobody:x:65534:65534::/nonexistent:/usr/sbin/nologin\n")):
            self.assertFalse(retry.normal_accounts_exist())

    def test_unknown_or_invalid_accounts_fail_closed(self):
        for text in ("", "root:x:bad:0::/root:/bin/bash\n", "malformed-secret\n"):
            with patch.object(retry.Path, "open", mock_open(read_data=text)):
                with self.assertRaises(retry.RecoveryError) as caught:
                    retry.normal_accounts_exist()
                self.assertNotIn("secret", str(caught.exception))
        with patch.object(retry.Path, "open", side_effect=PermissionError("PRIVATE-PATH")):
            with self.assertRaises(retry.RecoveryError) as caught:
                retry.normal_accounts_exist()
            self.assertNotIn("PRIVATE-PATH", str(caught.exception))

    def test_gate_requires_root_oobe_no_user_and_required_programs(self):
        with patch.object(retry.os, "geteuid", return_value=1000), \
                patch.object(retry, "run_systemctl") as systemctl:
            with self.assertRaises(retry.RecoveryError):
                retry.check_preconditions()
            systemctl.assert_not_called()
        with patch.object(retry.os, "geteuid", return_value=0), \
                patch.object(retry, "run_systemctl", return_value="graphical.target"), \
                patch.object(retry, "normal_accounts_exist") as accounts:
            with self.assertRaises(retry.RecoveryError):
                retry.check_preconditions()
            accounts.assert_not_called()
        with patch.object(retry.os, "geteuid", return_value=0), \
                patch.object(retry, "run_systemctl", return_value=retry.OOBE_TARGET), \
                patch.object(retry, "normal_accounts_exist", return_value=True):
            with self.assertRaises(retry.RecoveryError):
                retry.check_preconditions()
        with patch.object(retry.os, "geteuid", return_value=0), \
                patch.object(retry, "run_systemctl", return_value=retry.OOBE_TARGET), \
                patch.object(retry, "normal_accounts_exist", return_value=False), \
                patch.object(retry.Path, "is_file", return_value=False):
            with self.assertRaises(retry.RecoveryError):
                retry.check_preconditions()

    def test_console_open_has_fixed_device_and_safe_flags(self):
        with patch.object(retry.os, "open", return_value=42) as opened:
            self.assertEqual(retry.open_console(), 42)
        name, flags = opened.call_args.args
        self.assertEqual(name, "/dev/ttyGS0")
        for flag in (os.O_NOCTTY, os.O_NONBLOCK, os.O_CLOEXEC, os.O_RDWR):
            self.assertTrue(flags & flag)

    def test_failed_systemctl_output_is_never_logged(self):
        error = subprocess.CalledProcessError(1, "systemctl", output="PRIVATE", stderr="SECRET")
        with patch.object(retry.subprocess, "run", side_effect=error):
            with self.assertRaises(retry.RecoveryError) as caught:
                retry.run_systemctl("start", retry.OEM_UNIT)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn("PRIVATE", str(caught.exception))


class ConsoleProtocol(unittest.TestCase):
    def setUp(self):
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        self.original = termios.tcgetattr(self.slave)
        self.done = threading.Event()
        self.errors = []
        self.owned = []

    def open_pty(self):
        fd = os.open(os.ttyname(self.slave), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC)
        self.owned.append(fd)
        return fd

    def collect(self, duration=0.5):
        received = b""
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                break
            received += os.read(self.master, 8192)
            if retry.PROMPT in received:
                break
        return received

    def start_waiter(self, timeout=2):
        def worker():
            try:
                retry.wait_for_enter(timeout)
            except BaseException as error:
                self.errors.append(error)
            finally:
                self.done.set()
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        return thread

    def test_only_empty_enter_proceeds_without_input_echo_and_fd_is_closed(self):
        with patch.object(retry, "open_console", side_effect=self.open_pty):
            self.start_waiter()
            self.assertIn(retry.PROMPT, self.collect())
            os.write(self.master, b"PRIVATE-PASSWORD\r")
            self.assertFalse(self.done.wait(0.15))
            self.assertNotIn(b"PRIVATE-PASSWORD", self.collect(0.05))
            os.write(self.master, b"\r")
            self.assertTrue(self.done.wait(1))
        self.assertEqual(self.errors, [])
        self.assertEqual(termios.tcgetattr(self.slave), self.original)
        for fd in self.owned:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_timeout_restores_settings_and_closes_descriptor(self):
        with patch.object(retry, "open_console", side_effect=self.open_pty):
            with self.assertRaisesRegex(retry.RecoveryError, "Timed out"):
                retry.wait_for_enter(0.05)
        self.assertEqual(termios.tcgetattr(self.slave), self.original)
        with self.assertRaises(OSError):
            os.fstat(self.owned[0])

    def test_absent_device_and_eio_reopen_until_enter(self):
        opens = 0
        def flaky_open():
            nonlocal opens
            opens += 1
            if opens == 1:
                raise FileNotFoundError(errno.ENOENT, "DO-NOT-LOG")
            if opens == 2:
                raise OSError(errno.EIO, "DO-NOT-LOG")
            return self.open_pty()
        with patch.object(retry, "open_console", side_effect=flaky_open), \
                patch.object(retry, "RETRY_SECONDS", 0.01):
            self.start_waiter()
            self.assertIn(retry.PROMPT, self.collect())
            os.write(self.master, b"\r")
            self.assertTrue(self.done.wait(1))
        self.assertGreaterEqual(opens, 3)
        self.assertEqual(self.errors, [])

    def test_io_hangup_after_open_closes_then_reopens(self):
        read = retry.os.read
        failed = False
        def hungup_once(fd, size):
            nonlocal failed
            if fd in self.owned and not failed:
                failed = True
                raise OSError(errno.EIO, "PRIVATE-ERROR")
            return read(fd, size)
        with patch.object(retry, "open_console", side_effect=self.open_pty), \
                patch.object(retry.os, "read", side_effect=hungup_once), \
                patch.object(retry, "RETRY_SECONDS", 0.01):
            self.start_waiter()
            self.assertIn(retry.PROMPT, self.collect())
            os.write(self.master, b"\r")
            self.assertIn(retry.PROMPT, self.collect())
            os.write(self.master, b"\r")
            self.assertTrue(self.done.wait(1))
        self.assertGreaterEqual(len(self.owned), 2)
        self.assertEqual(self.errors, [])

    def test_unexpected_permission_failure_stops_without_device_details(self):
        with patch.object(retry, "open_console", side_effect=PermissionError(errno.EACCES, "PRIVATE-PATH")):
            with self.assertRaises(retry.RecoveryError) as caught:
                retry.wait_for_enter(0.1)
        self.assertNotIn("PRIVATE-PATH", str(caught.exception))

    def test_persistently_absent_device_has_a_bounded_wait(self):
        started = time.monotonic()
        with patch.object(retry, "open_console", side_effect=FileNotFoundError(errno.ENOENT, "PRIVATE")):
            with self.assertRaisesRegex(retry.RecoveryError, "Timed out"):
                retry.wait_for_enter(0.02)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_interruption_restores_terminal_and_closes_owned_descriptor(self):
        with patch.object(retry, "open_console", side_effect=self.open_pty), \
                patch.object(retry.select, "select", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                retry.wait_for_enter(0.1)
        self.assertEqual(termios.tcgetattr(self.slave), self.original)
        with self.assertRaises(OSError):
            os.fstat(self.owned[0])

    def test_failed_restore_still_closes_descriptor_and_cannot_claim_ready(self):
        fd = self.open_pty()
        with patch.object(retry.termios, "tcsetattr", side_effect=termios.error(errno.EIO, "PRIVATE")):
            self.assertFalse(retry.restore_and_close(fd, self.original))
        with self.assertRaises(OSError):
            os.fstat(fd)


class Handoff(unittest.TestCase):
    def test_handoff_rechecks_gate_and_only_controls_exact_units(self):
        actions = []
        def systemctl(*args, **kwargs):
            actions.append((args, kwargs))
            return "graphical.target" if args == ("get-default",) else ""
        output = io.StringIO()
        with patch.object(retry, "check_preconditions", side_effect=lambda: actions.append("gate")), \
                patch.object(retry, "wait_for_enter", side_effect=lambda: actions.append("terminal-closed")), \
                patch.object(retry, "run_systemctl", side_effect=systemctl), \
                patch.object(retry, "normal_accounts_exist", return_value=True), \
                redirect_stdout(output):
            self.assertEqual(retry.main([]), 0)
        self.assertEqual(actions, ["gate", (("stop", retry.GETTY), {}), "terminal-closed", "gate",
                                  (("start", retry.OEM_UNIT), {"timeout": None}), (("get-default",), {})])
        self.assertIn("Verify a fresh login", output.getvalue())

    def test_gate_or_timeout_failure_never_starts_wizard(self):
        for fail_at in ("gate", "wait", "second-gate"):
            calls = []
            gate = Mock(side_effect=(retry.RecoveryError("gate failed") if fail_at == "gate" else
                        [None, retry.RecoveryError("changed during wait")] if fail_at == "second-gate" else None))
            waiter = Mock(side_effect=retry.RecoveryError("wait failed") if fail_at == "wait" else None)
            with patch.object(retry, "check_preconditions", gate), \
                    patch.object(retry, "wait_for_enter", waiter), \
                    patch.object(retry, "run_systemctl", side_effect=lambda *a, **kw: calls.append(a)), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(retry.main([]), 1)
            self.assertNotIn(("start", retry.OEM_UNIT), calls)

    def test_oem_return_without_account_is_not_success(self):
        with patch.object(retry, "check_preconditions"), patch.object(retry, "wait_for_enter"), \
                patch.object(retry, "run_systemctl"), \
                patch.object(retry, "normal_accounts_exist", return_value=False), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(retry.main([]), 1)
        self.assertIn("without a confirmed account/setup transition", errors.getvalue())

    def test_alternate_devices_or_other_arguments_are_rejected(self):
        with patch.object(retry, "check_preconditions") as gate, redirect_stderr(io.StringIO()):
            self.assertEqual(retry.main(["--device", "/dev/other"]), 2)
        gate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
