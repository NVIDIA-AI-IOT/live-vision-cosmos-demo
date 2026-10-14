# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Exercise bootstrap against a fake SSH executable; never contacts a Jetson."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        app = self.root / 'app'
        (app / 'scripts').mkdir(parents=True)
        for name in ('bootstrap.sh', 'archive-source.sh'):
            shutil.copyfile(ROOT / 'scripts' / name, app / 'scripts' / name)
        (app / 'scripts/setup-orin.sh').write_text('# mock placeholder; never executed\n')
        self.script = app / 'scripts/bootstrap.sh'
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        fake = bin_dir / 'ssh'
        fake.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
from pathlib import Path
root = Path(os.environ['BOOTSTRAP_FIXTURE'])
with (root / 'argv.jsonl').open('a') as out: out.write(json.dumps(sys.argv[1:]) + '\\n')
command = sys.argv[-1]
if command == 'uname -sm': print('Linux aarch64')
elif 'dpkg-query' in command: print('39.2.1-test')
elif command == 'whoami': print('fixture-user')
elif command.startswith('umask 077; mktemp'): print('/tmp/cosmos-env.Fixture123')
elif command.startswith('cat > '): (root / 'payload').write_bytes(sys.stdin.buffer.read())
elif command.startswith('tar -x'): sys.stdin.buffer.read()
elif command.startswith('sudo bash -c'): sys.exit(int(os.environ.get('BOOTSTRAP_REMOTE_EXIT', '0')))
''')
        fake.chmod(0o755)
        self.env = dict(os.environ, PATH=str(bin_dir)+os.pathsep+os.environ['PATH'],
                        BOOTSTRAP_FIXTURE=str(self.root))
        self.env.pop('HF_TOKEN', None)
        self.env.pop('REACHY_SETUP_SKIP', None)

    def run_bootstrap(self):
        return subprocess.run(['bash', str(self.script), 'fixture-host'], env=self.env,
                              text=True, capture_output=True, timeout=15)

    def test_ungated_download_needs_no_token(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('export HF_TOKEN=', (self.root / 'payload').read_text())

    def test_explicit_robot_skip_is_passed_to_remote_setup(self):
        self.env['REACHY_SETUP_SKIP'] = '1'
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('export REACHY_SETUP_SKIP=1\n', (self.root / 'payload').read_text())

    def test_token_not_in_ssh_argv_and_failed_setup_cleans_temporary_file(self):
        value = 'synthetic-credential-for-test'
        self.env.update(HF_TOKEN=value, BOOTSTRAP_REMOTE_EXIT='23')
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 23)
        log = (self.root / 'argv.jsonl').read_text()
        self.assertNotIn(value, log + result.stdout + result.stderr)
        self.assertIn(value, (self.root / 'payload').read_text())
        commands = [json.loads(line)[-1] for line in log.splitlines()]
        self.assertEqual(commands[-1], "rm -f '/tmp/cosmos-env.Fixture123'")


if __name__ == '__main__':
    unittest.main()
