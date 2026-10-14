# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Regression checks for preserving failed privileged-write diagnostics."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import mac


class MacDiagnosticsTests(unittest.TestCase):
    def test_atomic_output_replaces_a_readonly_previous_log(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            old, new = directory / "write.log", directory / "new.log"
            old.write_text("old failure")
            old.chmod(0o444)
            new.write_text("current failure")
            mac.atomic_copy(new, old)
            self.assertEqual(old.read_text(), "current failure")
            self.assertEqual(sorted(path.name for path in directory.iterdir()), ["new.log", "write.log"])

    def test_failed_helper_retains_stage_and_copies_error_log(self):
        stages = []

        def expand_image(*args, **kwargs):
            if args[0] == "/unused/zstd":
                Path(args[-1]).write_bytes(b"test")

        class FailedHelper:
            returncode = 1

            def __init__(self, command, **kwargs):
                self.assert_sudo(command)
                assert "preexec_fn" not in kwargs
                assert not kwargs.get("start_new_session", False)
                stage = Path(command[3]).parent
                stages.append(stage)
                kwargs["stdout"].write("Operation not permitted: /dev/rdisk4\n")

            @staticmethod
            def assert_sudo(command):
                assert command[:3] == ["/usr/bin/sudo", "/usr/bin/python3", "-u"]

            def poll(self):
                return 1

        try:
            with tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                (directory / "image.json").write_text(json.dumps({"size_bytes": 4}))
                with patch.object(mac, "run", side_effect=expand_image), \
                     patch.object(mac.sys.stdin, "isatty", return_value=True), \
                     patch.object(mac.subprocess, "Popen", FailedHelper), redirect_stdout(io.StringIO()), \
                     self.assertRaisesRegex(RuntimeError, "Flash did not complete"):
                    mac.write_card(directory, "/dev/disk4", {}, "/unused/zstd")
                self.assertTrue(stages[0].is_dir())
                self.assertIn("Operation not permitted", (directory / "write.log").read_text())
                self.assertTrue((stages[0] / "write.log").exists())
                self.assertFalse((directory / "flash-receipt.json").exists())
        finally:
            for stage in stages:
                shutil.rmtree(stage)

    def test_noninteractive_write_stops_before_staging_or_authentication(self):
        with patch.object(mac.sys.stdin, "isatty", return_value=False), \
             patch.object(mac.tempfile, "mkdtemp") as stage, \
             patch.object(mac, "run") as run, \
             self.assertRaisesRegex(RuntimeError, "Run the flash command in Terminal"):
            mac.write_card(Path("unused"), "/dev/disk4", {}, "/unused/zstd")
        stage.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
