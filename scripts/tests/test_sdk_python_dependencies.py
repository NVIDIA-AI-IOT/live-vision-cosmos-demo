# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Read-only SDK dependency checks using isolated environments and fake packages."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import venv


ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "scripts/check-sdk-python.py"


class SDKPythonDependenciesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        environment = self.directory / "venv"
        venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
        self.python = environment / "bin/python"
        self.env = {**os.environ, "PYTHONPATH": "", "PYTHONNOUSERSITE": "1"}
        self.site = Path(subprocess.check_output(
            [str(self.python), "-c", 'import sysconfig; print(sysconfig.get_path("purelib"))'],
            text=True, env=self.env).strip())

    def check(self, env=None):
        return subprocess.run([str(self.python), str(CHECK)], env=env or self.env,
                              capture_output=True, text=True, timeout=15)

    def test_clean_environment_passes(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installed_av_is_rejected_without_import_or_modification(self):
        package = self.site / "av"
        package.mkdir()
        source = package / "__init__.py"
        contents = 'raise RuntimeError("must not import av")\n'
        source.write_text(contents)
        metadata = self.site / "av-17.1.0.dist-info"
        metadata.mkdir()
        metadata.joinpath("METADATA").write_text("Name: av\nVersion: 17.1.0\n")
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installed av distribution metadata", result.stderr)
        self.assertIn("importable av", result.stderr)
        self.assertNotIn("RuntimeError", result.stderr)
        self.assertEqual(source.read_text(), contents)
        self.assertTrue(metadata.is_dir())
        self.assertFalse(package.joinpath("__pycache__").exists())

    def test_metadata_only_or_bundled_libraries_are_rejected(self):
        for name, expected in (("av-17.1.0.dist-info", "distribution metadata"),
                               ("av.libs", "bundled libraries")):
            with self.subTest(name=name):
                path = self.site / name
                path.mkdir()
                marker = path / "METADATA"
                marker.write_text("Name: av\nVersion: 17.1.0\n")
                result = self.check()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)
                self.assertTrue(marker.is_file())
                marker.unlink()
                path.rmdir()

    def test_inherited_av_is_rejected_and_untouched(self):
        inherited = self.directory / "inherited"
        package = inherited / "av"
        package.mkdir(parents=True)
        source = package / "__init__.py"
        source.write_text("raise RuntimeError('do not import')\n")
        result = self.check(env={**self.env, "PYTHONPATH": str(inherited)})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("importable av", result.stderr)
        self.assertIn("clean TensorRT-Edge-LLM virtual environment", result.stderr)
        self.assertTrue(source.is_file())
        self.assertFalse(package.joinpath("__pycache__").exists())


class SDKDependencyStageTests(unittest.TestCase):
    def run_shell(self, command, directory):
        return subprocess.run(
            ["bash", "-c", 'source "$1"; STATE_DIR="$2"; ' + command,
             "fixture", str(ROOT / "scripts/setup-orin.sh"), str(directory)],
            env={**os.environ, "HF_TOKEN_FILE": "", "HF_TOKEN": "", "REACHY_MINI_IP": "192.0.2.50"},
            capture_output=True, text=True, timeout=15)

    def test_completed_dependency_marker_does_not_bypass_absence_check(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_shell('''
stage_mark sdk_python_dependencies
do_preflight() { :; }
adopt_host_export() { :; }
stage() { :; }
sudo() { echo CHECK_REJECTED; return 43; }
main
echo UNEXPECTED_CONTINUATION
''', directory)
        self.assertEqual(result.returncode, 43, result.stderr)
        self.assertIn("CHECK_REJECTED", result.stdout)
        self.assertNotIn("UNEXPECTED_CONTINUATION", result.stdout)

    def test_rejection_prevents_dependency_install_and_success_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".cuda-version").write_text("13.2")
            result = self.run_shell('''
pause_inference() { :; }
mkdir() { :; }
chown() { :; }
sudo() {
  [[ "$*" == *check-sdk-python.py ]] || exit 44
  return 45
}
stage sdk_python_dependencies do_install_edgellm_python
echo UNEXPECTED_CONTINUATION
''', directory)
            self.assertEqual(result.returncode, 45, result.stderr)
            self.assertFalse(Path(directory, "sdk_python_dependencies").exists())
            self.assertNotIn("UNEXPECTED_CONTINUATION", result.stdout)


if __name__ == "__main__":
    unittest.main()
