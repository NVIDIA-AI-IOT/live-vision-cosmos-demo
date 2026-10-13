# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import signal
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bundle
import download
import launch
import runtime


def fixture(root):
    for name in bundle.REQUIRED:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}' if path.suffix == '.json' else 'synthetic test bytes')
    template = json.dumps({
        "roles": {role: {"prefix": role, "suffix": "end"} for role in ("system", "user", "assistant")},
        "generation_prompt": "assistant",
        "content_types": {"image": {"format": "<|vision_start|><|image_pad|><|vision_end|>"}},
    })
    for name in ('engines/reasoning/processed_chat_template.json', 'model/processed_chat_template.json'):
        (root / name).write_text(template)
    manifest = json.loads(bundle.SPEC.read_text())
    (root / 'engines/reasoning/config.json').write_text(json.dumps({'builder_config': manifest['engine_config']}))
    manifest.update(candidate_commit='a' * 40, int4_plugin_version=1, files=bundle.inventory(root))
    (root / 'manifest.json').write_text(json.dumps(manifest))
    return root


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = fixture(Path(self.temp.name) / 'bundle')

    def test_complete_bundle(self):
        self.assertEqual(bundle.verify(self.root)['int4_plugin_version'], 1)
        with self.assertRaisesRegex(ValueError, 'candidate commit mismatch'):
            bundle.verify(self.root, candidate_commit='b' * 40)

    def test_corrupt_and_extra_files(self):
        for filename in ('model/tokenizer.json', 'extra.bin'):
            with self.subTest(filename=filename):
                path = self.root / filename
                original = path.read_bytes() if path.exists() else None
                path.write_text('changed')
                with self.assertRaisesRegex(ValueError, 'checksum/file-set'):
                    bundle.verify(self.root)
                path.unlink() if original is None else path.write_bytes(original)

    def test_missing_engine(self):
        (self.root / 'engines/reasoning/llm.engine').unlink()
        with self.assertRaises(ValueError):
            bundle.verify(self.root)

    def test_wrong_capacities_even_with_fresh_checksums(self):
        path = self.root / 'engines/reasoning/config.json'
        config = json.loads(path.read_text())
        config['builder_config']['max_input_len'] = 4096
        path.write_text(json.dumps(config))
        manifest_path = self.root / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files'] = bundle.inventory(self.root)
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'capacities'):
            bundle.verify(self.root)

    def test_incompatible_identity(self):
        path = self.root / 'manifest.json'
        good = json.loads(path.read_text())
        for key in ('schema_version', 'model', 'target', 'sdk_commit', 'int4_plugin_version', 'export_compatibility'):
            with self.subTest(key=key):
                bad = dict(good)
                bad[key] = True if key == 'int4_plugin_version' else 'wrong'
                path.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    bundle.verify(self.root)

    def refresh_checksums(self):
        path = self.root / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest['files'] = bundle.inventory(self.root)
        path.write_text(json.dumps(manifest))

    def test_missing_provider_template_rejected(self):
        (self.root / 'engines/reasoning/processed_chat_template.json').unlink()
        self.refresh_checksums()
        with self.assertRaisesRegex(ValueError, 'lacks required'):
            bundle.verify(self.root)

    def test_incomplete_native_runtime_artifacts_rejected(self):
        # The SDK loads these from the engine tree, not the separate checkpoint.
        for name in ('embedding.safetensors', 'tokenizer.json', 'tokenizer_config.json',
                     'visual/preprocessor_config.json'):
            with self.subTest(name=name):
                path = self.root / 'engines/reasoning' / name
                original = path.read_bytes()
                path.unlink()
                self.refresh_checksums()
                with self.assertRaisesRegex(ValueError, 'lacks required'):
                    bundle.verify(self.root)
                path.write_bytes(original)

    def test_modified_or_empty_provider_template_rejected(self):
        for text in ('', 'different template'):
            with self.subTest(text=text):
                (self.root / 'engines/reasoning/processed_chat_template.json').write_text(text)
                self.refresh_checksums()
                with self.assertRaises(ValueError):
                    bundle.verify(self.root)

    def test_incomplete_processed_template_rejected_with_valid_checksums(self):
        for name in ('engines/reasoning/processed_chat_template.json', 'model/processed_chat_template.json'):
            (self.root / name).write_text('{}')
        self.refresh_checksums()
        with self.assertRaisesRegex(ValueError, 'Incomplete processed'):
            bundle.verify(self.root)

    def test_old_flat_visual_layout_rejected(self):
        (self.root / 'engines/reasoning/visual/visual.engine').rename(
            self.root / 'engines/reasoning/visual.engine')
        self.refresh_checksums()
        with self.assertRaisesRegex(ValueError, 'lacks required'):
            bundle.verify(self.root)

    def test_manifest_traversal(self):
        path = self.root / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest['files']['../escape'] = {'sha256': '0' * 64, 'size_bytes': 1}
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'Unsafe manifest'):
            bundle.verify(self.root)

    def test_links_and_secrets(self):
        (self.root / 'linked').symlink_to('/etc/hosts')
        with self.assertRaisesRegex(ValueError, 'Symlinks'):
            bundle.verify(self.root)
        (self.root / 'linked').unlink()
        (self.root / '.env').write_text('synthetic')
        with self.assertRaisesRegex(ValueError, 'Hidden'):
            bundle.verify(self.root)

    def test_create_preserves_inputs(self):
        output = Path(self.temp.name) / 'created'
        bundle.create(self.root / 'engines/reasoning', self.root / 'model', output, 1, self.root / 'notices', 'a' * 40)
        self.assertEqual(bundle.verify(output)['files'], bundle.verify(self.root)['files'])
        with self.assertRaises(ValueError):
            bundle.create(self.root / 'engines/reasoning', self.root / 'model', output, 1, self.root / 'notices', 'a' * 40)

    def test_static_output_rejected(self):
        with self.assertRaises(ValueError):
            bundle.external_output(bundle.APP_ROOT / 'engine.tar')
        public = next((p for p in bundle.APP_ROOT.parents if p.name == 'public'), None)
        if public:
            with self.assertRaises(ValueError):
                bundle.external_output(public / 'other' / 'engine.tar')

    def test_archive_install_and_unsafe_members(self):
        archive = Path(self.temp.name) / 'bundle.tar'
        with tarfile.open(archive, 'w') as tar:
            tar.add(self.root, arcname='.')
        output = Path(self.temp.name) / 'installed'
        download.install(archive, output)
        bundle.verify(output)
        for name, kind in (('../escape', tarfile.REGTYPE), ('/escape', tarfile.REGTYPE),
                           ('link', tarfile.SYMTYPE), ('hardlink', tarfile.LNKTYPE)):
            with self.subTest(name=name):
                with tarfile.open(archive, 'w') as tar:
                    item = tarfile.TarInfo(name)
                    item.type = kind
                    item.linkname = '/etc/hosts'
                    tar.addfile(item)
                rejected = Path(self.temp.name) / 'rejected'
                with self.assertRaises(ValueError):
                    download.install(archive, rejected)
                self.assertFalse(rejected.exists())


@contextlib.contextmanager
def server(payload, mode='range'):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            header = self.headers.get('Range')
            requests.append(header)
            offset = int(header.split('=')[1].split('-')[0]) if header and mode != 'ignore' else 0
            if offset >= len(payload):
                self.send_response(416)
                self.end_headers()
                return
            self.send_response(206 if offset else 200)
            if offset:
                self.send_header('Content-Range', f'bytes {offset}-{len(payload)-1}/{len(payload)}')
            self.send_header('Content-Length', str(len(payload)-offset))
            self.end_headers()
            data = payload[offset:]
            if mode == 'interrupt' and not header:
                data = data[:len(data)//2]
            self.wfile.write(data)
            self.close_connection = True

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{httpd.server_port}/bundle', requests
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join()


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'engine.tar'
        self.data = b'fixture' * 10000
        self.sha = hashlib.sha256(self.data).hexdigest()

    def test_interrupted_resume(self):
        with server(self.data, 'interrupt') as (url, requests):
            with self.assertRaisesRegex(ValueError, 'interrupted'):
                download.download(url, self.sha, self.output)
            self.assertFalse(self.output.exists())
            download.download(url, self.sha, self.output)
        self.assertEqual(self.output.read_bytes(), self.data)
        self.assertEqual(requests, [None, f'bytes={len(self.data)//2}-'])

    def test_range_ignored_restarts(self):
        self.output.with_suffix('.tar.part').write_bytes(b'stale')
        with server(self.data, 'ignore') as (url, requests):
            download.download(url, self.sha, self.output)
        self.assertEqual(self.output.read_bytes(), self.data)
        self.assertEqual(requests, ['bytes=5-'])

    def test_checksum_failure_never_installs(self):
        with server(self.data) as (url, _):
            with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
                download.download(url, 'a' * 64, self.output)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.with_suffix('.tar.part').exists())

    def test_stale_complete_partial_restarts_after_416(self):
        self.output.with_suffix('.tar.part').write_bytes(b'x' * len(self.data))
        with server(self.data) as (url, requests):
            download.download(url, self.sha, self.output)
        self.assertEqual(self.output.read_bytes(), self.data)
        self.assertEqual(requests, [f'bytes={len(self.data)}-', None])

    def test_completed_partial_and_existing_file(self):
        self.output.with_suffix('.tar.part').write_bytes(self.data)
        url = 'https://example.invalid/bundle'
        download.download(url, self.sha, self.output)
        download.download(url, self.sha, self.output)
        self.assertEqual(self.output.read_bytes(), self.data)

    def test_size_limit(self):
        with server(self.data) as (url, _):
            with self.assertRaisesRegex(ValueError, 'size limit'):
                download.download(url, self.sha, self.output, max_bytes=100)
        self.assertFalse(self.output.exists())

    def test_untrusted_transport_and_partial_link(self):
        with self.assertRaises(ValueError):
            download.download('http://example.com/engine', self.sha, self.output)
        self.output.with_suffix('.tar.part').symlink_to('/etc/hosts')
        with self.assertRaises(ValueError):
            download.download('https://example.invalid/engine', self.sha, self.output)


class LifecycleTests(unittest.TestCase):
    def test_signal_during_startup_cleans_up_without_starting_ui(self):
        child = MagicMock(pid=999003)
        child.poll.return_value = None
        handlers = {}

        def register(sig, handler):
            handlers[sig] = handler
            return signal.SIG_DFL

        def terminate(*args, **kwargs):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return io.BytesIO(b'{"status":"ready"}')

        with patch.object(runtime.subprocess, 'Popen', return_value=child) as spawn, \
             patch.object(runtime.os, 'killpg') as kill, \
             patch.object(runtime.signal, 'signal', side_effect=register), \
             patch.object(runtime.urllib.request, 'urlopen', side_effect=terminate):
            self.assertEqual(runtime.supervise([['shim'], ['ui']], 'http://127.0.0.1/health/ready'), 0)
            spawn.assert_called_once()
            kill.assert_called_once_with(child.pid, signal.SIGTERM)
            child.wait.assert_called_once_with(timeout=5)

    def run_case(self, first_poll, second=None, ready=True, timeout=0):
        first = MagicMock(pid=999001)
        first.poll.side_effect = first_poll
        children = [first, second] if second else [first]
        with patch.object(runtime.subprocess, 'Popen', side_effect=children) as spawn, \
             patch.object(runtime.os, 'killpg') as kill, \
             patch.object(runtime.signal, 'signal'), \
             patch.object(runtime.time, 'sleep'), \
             patch.object(runtime.urllib.request, 'urlopen') as open_url:
            open_url.side_effect = (lambda *a, **k: io.BytesIO(b'{"status":"ready"}')) if ready else OSError('not ready')
            with self.assertRaises(RuntimeError):
                runtime.supervise([['shim'], ['ui']], 'http://127.0.0.1/health/ready', timeout)
            self.assertEqual(spawn.call_count, len(children))
            first.wait.assert_called_once_with(timeout=5)
            kill.assert_any_call(first.pid, signal.SIGTERM)
            if second:
                second.wait.assert_called_once_with(timeout=5)

    def test_shim_dies_before_ready(self):
        self.run_case([1])

    def test_readiness_timeout_stops_shim(self):
        self.run_case([None], ready=False)

    def test_ui_failure_stops_both_children(self):
        ui = MagicMock(pid=999002, returncode=1)
        ui.poll.return_value = 1
        self.run_case([None, None], second=ui)

    def test_launch_has_fixed_read_only_mounts(self):
        command = launch.docker_command('sha256:' + 'a' * 64, '/bundle', '/tls', 8443, '::1')
        self.assertIn('[::1]:8443:8443', command)
        self.assertIn('--read-only', command)
        self.assertIn('--runtime=nvidia', command)
        self.assertNotIn('--privileged', command)
        self.assertNotIn('docker.sock', ' '.join(command))
        self.assertNotIn('systemd', ' '.join(command))
        with self.assertRaises(ValueError):
            launch.docker_command('image:latest', '/bundle', '/tls', 8443, '127.0.0.1')


if __name__ == '__main__':
    unittest.main()
