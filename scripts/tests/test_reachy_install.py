# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Optional robot installation paths, with all privileged actions replaced by fixtures."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
COMMON = '''
INSTALL_DIR="$FIXTURE"
STATE_DIR="$FIXTURE/state"
REACHY_ENV_FILE="$FIXTURE/reachy.env"
REACHY_MINI_IP=""
SERVICE_USER=fixture
'''


class ReachyInstall(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = Path(temporary.name)
        (self.fixture / "state").mkdir()

    def run_shell(self, command):
        return subprocess.run(
            ["bash", "-c", 'source "$1"\n' + COMMON + command, "fixture",
             str(ROOT / "scripts/setup-orin.sh")],
            env={**os.environ, "FIXTURE": str(self.fixture), "HF_TOKEN": "", "HF_TOKEN_FILE": "",
                 "REACHY_SETUP_SKIP": "0", "REACHY_MINI_IP": ""},
            capture_output=True, text=True, timeout=15,
        )

    def events(self):
        path = self.fixture / "events"
        return path.read_text().splitlines() if path.exists() else []

    def test_preparation_installs_bridge_dependencies_when_no_robot_is_selected(self):
        (self.fixture / "reachy_env").mkdir()
        result = self.run_shell('''
apt-get() { printf 'apt %s\n' "$*" >> "$FIXTURE/events"; }
sudo() { printf 'sudo %s\n' "$*" >> "$FIXTURE/events"; }
do_install_reachy_units() { echo units >> "$FIXTURE/events"; }
do_install_reachy_sudoers() { echo permissions >> "$FIXTURE/events"; }
do_register_reachy_service() { echo registry >> "$FIXTURE/events"; }
stage reachy_prepare do_prepare_reachy
[[ "$REACHY_PREPARED_THIS_RUN" -eq 1 ]]
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any("avahi-utils" in event for event in self.events()))
        self.assertTrue(any("/reachy_env/bin/pip install -r" in event for event in self.events()))
        self.assertEqual((self.fixture / "state/reachy_prepare").read_text().strip(), "reachy-prepare-v1")

    def test_robot_only_upgrade_never_enters_model_pipeline_or_controls_shim(self):
        (self.fixture / "llm.engine").write_bytes(b"unchanged engine")
        (self.fixture / "state/build_engine").write_text("preserved marker")
        result = self.run_shell('''
reachy_only_preflight() { :; }
do_preflight() { echo UNEXPECTED_FULL_PREFLIGHT; return 88; }
do_build_engine() { echo UNEXPECTED_ENGINE_BUILD; return 88; }
do_setup_piper() { echo piper >> "$FIXTURE/events"; }
do_prepare_reachy() { echo prepare >> "$FIXTURE/events"; REACHY_PREPARED_THIS_RUN=1; }
do_optional_reachy_setup() { echo choice >> "$FIXTURE/events"; }
systemctl() { printf 'systemctl %s\n' "$*" >> "$FIXTURE/events"; }
reachy_only_main
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("UNEXPECTED", result.stdout)
        self.assertEqual(self.events(), ["piper", "prepare", "systemctl restart live-vision-cosmos-demo-ui.service", "choice"])
        self.assertEqual((self.fixture / "llm.engine").read_bytes(), b"unchanged engine")
        self.assertEqual((self.fixture / "state/build_engine").read_text(), "preserved marker")
        unit = (ROOT / "systemd/live-vision-cosmos-demo-ui.service").read_text()
        self.assertNotIn("Wants=live-vision-cosmos-demo-shim", unit)

    def test_explicit_skip_never_runs_discovery_or_connect(self):
        result = self.run_shell('''
reachy_manager() { printf '%s\n' "$*" >> "$FIXTURE/events"; }
REACHY_SETUP_SKIP=1
do_optional_reachy_setup
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), ["skip"])

    def test_optional_preparation_failure_keeps_running_baseline_available(self):
        result = self.run_shell('''
bash() { printf '%s\n' "$*" >> "$FIXTURE/events"; return 42; }
systemctl() { echo UNEXPECTED_SERVICE_CHANGE; return 88; }
run_optional_reachy_preparation
echo BASELINE_AVAILABLE
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BASELINE_AVAILABLE", result.stdout)
        self.assertNotIn("UNEXPECTED", result.stdout)
        self.assertIn("optional Reachy preparation failed", result.stderr)
        self.assertTrue(self.events()[0].endswith("setup-orin.sh --reachy-only"))

    def test_manager_preserves_private_exclusions_without_command_arguments(self):
        result = self.run_shell('''
export REACHY_EXCLUDED_ADDRESSES=private-fixture-value
runuser() {
  [[ "$REACHY_EXCLUDED_ADDRESSES" == private-fixture-value ]] || return 88
  printf '%s\n' "$*" >> "$FIXTURE/events"
}
reachy_manager discover --json
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.events()[0].startswith("-u fixture -- /usr/bin/python3"))
        self.assertNotIn("private-fixture-value", self.events()[0])

    def test_noninteractive_setup_defers_without_picking_robot(self):
        result = self.run_shell('''
reachy_manager() {
  printf '%s\n' "$*" >> "$FIXTURE/events"
  [[ "$1" == status ]] || return 88
  printf '{"configured":false,"skipped":false}\n'
}
do_optional_reachy_setup
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), ["status --json"])
        self.assertIn("No robot was selected", result.stdout)

    def test_saved_skip_stays_skipped_on_next_install(self):
        result = self.run_shell('''
reachy_manager() {
  printf '%s\n' "$*" >> "$FIXTURE/events"
  [[ "$1" == status ]] || return 88
  printf '{"configured":false,"skipped":true}\n'
}
do_optional_reachy_setup
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), ["status --json"])
        self.assertIn("already saved", result.stdout)

    def test_explicit_address_failure_keeps_optional_install_available(self):
        result = self.run_shell('''
reachy_manager() { printf '%s\n' "$*" >> "$FIXTURE/events"; return 1; }
REACHY_MINI_IP=robot.example
do_optional_reachy_setup
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), ["connect robot.example"])
        self.assertIn("could not be connected", result.stderr)

    def test_registry_addition_is_idempotent_and_preserves_other_services(self):
        path = self.fixture / "services.json"
        original = {"Existing": {"cmdline_match": "other_process.py", "path": "/example"}}
        path.write_text(json.dumps(original))
        result = self.run_shell('''
sudo() { shift 2; "$@"; }
do_register_reachy_service
do_register_reachy_service
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(path.read_text())
        self.assertEqual(saved["Existing"], original["Existing"])
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved["Reachy Mini bridge"]["cmdline_match"], "reachy_mjpeg_bridge.py")

    def test_registry_recognizes_existing_bridge_under_another_name(self):
        path = self.fixture / "services.json"
        original = {"Camera": {"cmdline_match": "/example/reachy_mjpeg_bridge.py"}}
        path.write_text(json.dumps(original))
        result = self.run_shell('sudo() { shift 2; "$@"; }; do_register_reachy_service')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(path.read_text()), original)

    def test_fallback_archive_excludes_device_local_robot_configuration(self):
        (self.fixture / "source.py").write_text("print('fixture')")
        for name in ("reachy.env", "reachy-setup.json", "reachy-setup.lock", ".reachy-secret-temp"):
            (self.fixture / name).write_text("private device fixture")
        (self.fixture / "scripts").mkdir()
        archive_script = self.fixture / "scripts/archive-source.sh"
        shutil.copyfile(ROOT / "scripts/archive-source.sh", archive_script)
        result = subprocess.run(["bash", str(archive_script)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            names = archive.getnames()
        self.assertTrue(any(name.endswith("source.py") for name in names))
        self.assertFalse(any("reachy" in name for name in names))


if __name__ == "__main__":
    unittest.main()
