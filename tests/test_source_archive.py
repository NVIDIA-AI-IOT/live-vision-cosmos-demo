# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
import io
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/archive-source.sh'


class SourceArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / 'public/code-samples/demo'
        (self.app / 'scripts').mkdir(parents=True)
        shutil.copyfile(SCRIPT, self.app / 'scripts/archive-source.sh')
        (self.app / 'README.md').write_text('fixture source')

    def members(self):
        data = subprocess.check_output(['bash', str(self.app / 'scripts/archive-source.sh')])
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            return {m.name.removeprefix('./') for m in archive}

    def test_git_subtree_does_not_ship_parent_repository(self):
        (self.root / 'unrelated-website.txt').write_text('must stay outside the archive')
        for args in (['init', '-q'], ['add', '.'], ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                                'commit', '-qm', 'fixture']):
            subprocess.run(['git', '-C', str(self.root), *args], check=True)
        members = self.members()
        self.assertIn('README.md', members)
        self.assertIn('scripts/archive-source.sh', members)
        self.assertNotIn('unrelated-website.txt', members)
        self.assertFalse(any(m.startswith('public/') for m in members))

    def test_plain_download_excludes_credentials_and_engines(self):
        (self.app / '.env.local').write_text('synthetic configuration')
        (self.app / 'reachy.env').write_text('REACHY_MINI_IP=192.0.2.50')
        for name in ('reachy-setup.json', 'reachy-setup.lock', '.reachy-private'):
            (self.app / name).write_text('synthetic device settings')
        (self.app / 'engines').mkdir()
        (self.app / 'engines/llm.engine').write_text('synthetic engine')
        members = self.members()
        self.assertIn('README.md', members)
        self.assertFalse(any('.env' in m or 'engines' in m or 'reachy-setup' in m or '.reachy-' in m for m in members))


if __name__ == '__main__':
    unittest.main()
