#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Fetch the optional source-install speech assets by checksum (Python 3.12+)."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'container'))
from download import download


def fetch(manifest, output, voice):
    if manifest.get('schema_version') != 1 or manifest['voice']['name'] != voice:
        raise ValueError('Voice differs from the pinned manifest; provide a reviewed custom manifest')
    output = Path(output).resolve()
    if any(p.name == 'public' for p in output.parents):
        raise ValueError('Speech assets must not be installed into website static assets')
    with tempfile.TemporaryDirectory(prefix='cosmos-piper-') as scratch:
        scratch = Path(scratch)
        archive = download(manifest['piper']['url'], manifest['piper']['sha256'], scratch / 'piper.tar.gz')
        with tarfile.open(archive) as source:
            source.extractall(scratch / 'unpacked', filter='data')
        tree = scratch / 'unpacked/piper'
        if not (tree / 'piper').is_file():
            raise ValueError('Piper archive lacks its expected executable')
        # Stage all files before touching an existing installation.
        for group in (manifest['voice']['files'], manifest['notices']):
            for name, item in group.items():
                if Path(name).name != name or name.startswith('.'):
                    raise ValueError('Unsafe download filename')
                download(item['url'], item['sha256'], tree / name)
        shutil.copytree(tree, output, dirs_exist_ok=True, symlinks=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=Path(__file__).with_name('piper-downloads.json'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--voice', default='en_US-ljspeech-medium')
    args = parser.parse_args()
    try:
        fetch(json.loads(args.manifest.read_text()), args.output, args.voice)
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        parser.exit(2, f'Piper download failed: {exc}\n')


if __name__ == '__main__':
    main()
