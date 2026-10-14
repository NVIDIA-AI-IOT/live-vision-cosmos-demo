#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Temporary, account-preserving entry to NVIDIA's existing headless OEM wizard.

This helper takes no arguments. It does not create users, set passwords, change
debconf answers, or log terminal input. Remove its temporary systemd overrides
after the wizard completes and a fresh login has been verified.
"""
import errno
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import termios
import time


TTY = "/dev/ttyGS0"
SYSTEMCTL = "/bin/systemctl"
GETTY = "serial-getty@ttyGS0.service"
OEM_UNIT = "nv-oem-config-debconf@ttyGS0.service"
OOBE_TARGET = "nv-oobe.target"
WAIT_SECONDS = 1200
RETRY_SECONDS = 1.0
PROMPT = b"\rPress ENTER on an empty line to start System Configuration... "
REQUIRED_PROGRAMS = (
    "/usr/sbin/nv-oem-config-firstboot",
    "/usr/sbin/oem-config-wrapper",
    "/usr/sbin/oem-config",
)
DISCONNECT_ERRORS = {
    errno.EIO, errno.ENODEV, errno.ENXIO, errno.ENOENT, errno.EPIPE,
}


class RecoveryError(Exception):
    """A safe, fixed diagnostic; never include received input or credentials."""


def run_systemctl(*arguments, timeout=30):
    try:
        return subprocess.run(
            [SYSTEMCTL, *arguments], check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=timeout,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise RecoveryError(
            "A required systemd operation failed. Inspect the OEM service journal locally; "
            "this helper has not changed any account or password."
        ) from None


def normal_accounts_exist():
    """Read local account metadata only; do not invoke network-backed NSS."""
    saw_root = False
    try:
        with Path("/etc/passwd").open(encoding="utf-8", errors="strict") as stream:
            for line in stream:
                if not line.strip():
                    continue
                fields = line.rstrip("\n").split(":")
                if (len(fields) != 7 or not fields[2].isascii() or not fields[2].isdecimal()
                        or not fields[3].isascii() or not fields[3].isdecimal()):
                    raise RecoveryError("The local account database is malformed; refusing to restart setup.")
                saw_root |= fields[0] == "root" and int(fields[2]) == 0
                if 1000 <= int(fields[2]) <= 65533:
                    return True
    except (OSError, UnicodeError):
        raise RecoveryError("Cannot inspect local accounts; refusing to restart setup.") from None
    if not saw_root:
        raise RecoveryError("The local account database lacks its root record; refusing to restart setup.")
    return False


def check_preconditions():
    if os.geteuid() != 0:
        raise RecoveryError("This recovery helper must run as its root-owned systemd service.")
    if run_systemctl("get-default") != OOBE_TARGET:
        raise RecoveryError("The default target is no longer nv-oobe.target; leaving the existing setup unchanged.")
    if normal_accounts_exist():
        raise RecoveryError("A regular user account already exists; refusing to rerun the first-time wizard.")
    if any(not Path(program).is_file() or not os.access(program, os.X_OK)
           for program in REQUIRED_PROGRAMS):
        raise RecoveryError("A required NVIDIA/Ubuntu OEM program is unavailable; repair package state before retrying.")


def open_console():
    return os.open(TTY, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC)


def restore_and_close(fd, original):
    restored = True
    try:
        if original is not None:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, original)
            except (OSError, termios.error):
                restored = False
    finally:
        os.close(fd)
    return restored


def wait_for_enter(timeout=WAIT_SECONDS):
    """Use only ttyGS0; tests replace open_console with a private PTY."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fd = None
        original = None
        accepted = False
        restored = False
        try:
            fd = open_console()
            original = termios.tcgetattr(fd)
            settings = original.copy()
            settings[0] = (settings[0] | termios.ICRNL) & ~(termios.IGNCR | termios.INLCR)
            settings[3] = (settings[3] | termios.ICANON) & ~(termios.ECHO | termios.ECHONL)
            termios.tcsetattr(fd, termios.TCSANOW, settings)
            termios.tcflush(fd, termios.TCIFLUSH)
            next_prompt = 0.0
            line_has_content = False
            while time.monotonic() < deadline:
                now = time.monotonic()
                if now >= next_prompt:
                    try:
                        os.write(fd, PROMPT)
                    except BlockingIOError:
                        pass
                    next_prompt = now + 5.0
                readable, _, _ = select.select([fd], [], [], min(1.0, max(0.0, deadline - now)))
                if not readable:
                    continue
                try:
                    data = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                if not data:
                    break
                for byte in data:
                    if byte == 10:
                        if not line_has_content:
                            accepted = True
                            break
                        line_has_content = False
                    else:
                        line_has_content = True
                if accepted:
                    break
        except (OSError, termios.error) as error:
            code = getattr(error, "errno", None)
            if code is None and error.args:
                code = error.args[0]
            if code not in DISCONNECT_ERRORS:
                raise RecoveryError("The USB console could not be prepared safely; inspect its device state locally.") from None
        finally:
            if fd is not None:
                restored = restore_and_close(fd, original)
        if accepted and restored:
            return
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(RETRY_SECONDS, remaining))
    raise RecoveryError(
        "Timed out waiting for an empty Enter on the USB console. "
        "No account or password was changed by this helper; reconnect the console and retry setup."
    )


def interrupted(_signum, _frame):
    raise RecoveryError("Headless setup entry was interrupted; no account or password was changed by this helper.")


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        print("Headless setup entry accepts no arguments or alternate devices.", file=sys.stderr)
        return 2
    try:
        check_preconditions()
        run_systemctl("stop", GETTY)
        print("Waiting for the USB console and an empty Enter (up to 20 minutes).", flush=True)
        wait_for_enter()
        # The terminal is restored and its descriptor closed before systemd grants
        # terminal ownership to the vendor service. Recheck the account gate too.
        check_preconditions()
        run_systemctl("start", OEM_UNIT, timeout=None)
        if not normal_accounts_exist() or run_systemctl("get-default") == OOBE_TARGET:
            raise RecoveryError(
                "The OEM service ended without a confirmed account/setup transition. "
                "Do not assume setup succeeded; inspect its journal locally."
            )
        print("The OEM service finished. Verify a fresh login before declaring setup complete.", flush=True)
        return 0
    except (RecoveryError, KeyboardInterrupt) as error:
        message = str(error) if isinstance(error, RecoveryError) else "Headless setup entry was interrupted."
        print("Headless setup entry stopped: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    for signal_number in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signal_number, interrupted)
    raise SystemExit(main())
