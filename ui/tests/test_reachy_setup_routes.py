# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Reachy setup HTTP boundaries and shared runtime; no robot or system services."""
import http.client
import importlib.util
import json
from pathlib import Path
import threading
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('reachy_setup_ui', Path(__file__).resolve().parents[1] / 'scripts/serve_ui.py')
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


class SetupRoutes(unittest.TestCase):
    def setUp(self):
        class Quiet(ui.Handler):
            def log_message(self, *args): pass
        telemetry = mock.Mock()
        self.server = ui.Server(('127.0.0.1', 0), Quiet, telemetry=telemetry)
        self.setup = mock.Mock()
        self.state = dict(available=True, bridge_prepared=True, configured=False, skipped=False, address='', busy=False)
        self.setup.snapshot.side_effect = lambda: dict(self.state)
        self.setup.discover.return_value = {'devices': [], 'status': 'empty', 'message': 'Enter an address or skip.'}
        self.client_factory = mock.Mock()
        self.piper_factory = mock.Mock()
        self.runtime = ui.ReachyRuntime(self.setup, self.piper_factory, self.client_factory)
        self.runtime.refresh()
        self.server.reachy_setup = self.setup
        self.server.reachy_runtime = self.runtime
        self.server.engine_switcher = mock.Mock()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.runtime.close()

    def request(self, method='POST', action='discover', payload=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        defaults = {'Content-Type': 'application/json', 'X-Reachy-Token': ui.RELAY_TOKEN}
        defaults.update(headers or {})
        path = '/api/reachy/setup' + ('/' + action if method == 'POST' else '')
        connection.request(method, path, json.dumps(payload or {}) if method == 'POST' else None, defaults)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, json.loads(data)

    def test_discovery_requires_token_origin_host_and_fetch_metadata(self):
        for headers, code in [({'X-Reachy-Token': ''}, 401),
                              ({'Origin': 'https://unrelated.example'}, 403),
                              ({'Host': 'rebound.example'}, 421),
                              ({'Sec-Fetch-Site': 'cross-site'}, 403)]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], code)
        self.setup.discover.assert_not_called()
        self.assertEqual(self.request()[0], 200)
        self.setup.discover.assert_called_once_with()

    def test_status_cross_site_refused_and_no_discovery_from_get(self):
        self.assertEqual(self.request(method='GET', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        code, body = self.request(method='GET')
        self.assertEqual(code, 200)
        self.assertFalse(body['configured'])
        self.setup.discover.assert_not_called()

    def test_reject_arbitrary_scan_range_and_invalid_config_body(self):
        self.assertEqual(self.request(payload={'network': '10.0.0.0/8'})[0], 400)
        self.assertEqual(self.request(action='configure', payload={'address': ['10.0.0.1']})[0], 400)
        self.assertEqual(self.request(action='configure', payload={'address': '10.0.0.1', 'command': 'anything'})[0], 400)
        self.assertEqual(self.request(action='skip', payload={'address': '10.0.0.1'})[0], 400)
        self.setup.configure.assert_not_called()
        self.setup.skip.assert_not_called()
        self.setup.discover.assert_not_called()

    def test_configuration_changes_runtime_but_never_engine(self):
        def configure(address):
            self.state.update(configured=True, address=address)
            return dict(self.state)
        self.setup.configure.side_effect = configure
        code, body = self.request(action='configure', payload={'address': '192.168.10.20'})
        self.assertEqual(code, 200, body)
        self.client_factory.assert_called_once_with('http://192.168.10.20:8000')
        self.assertIs(self.server.reachy_client, self.client_factory.return_value)
        self.server.engine_switcher.switch.assert_not_called()
        self.assertFalse(self.piper_factory.return_value.synth.called)

    def test_configuration_failure_leaves_runtime_unchanged(self):
        self.setup.configure.side_effect = RuntimeError('Bridge failed; restored previous configuration.')
        code, body = self.request(action='configure', payload={'address': '192.168.10.20'})
        self.assertEqual(code, 503)
        self.assertIn('restored', body['error']['message'])
        self.assertIsNone(self.server.reachy_client)
        self.client_factory.assert_not_called()

    def test_shared_runtime_reads_cli_changes_and_closes_optional_workers(self):
        other = ui.Server(('127.0.0.1', 0), ui.Handler, telemetry=mock.Mock())
        try:
            other.reachy_runtime = self.runtime
            self.state.update(configured=True, address='192.168.10.20')
            self.runtime.refresh()
            self.assertIs(other.reachy_client, self.server.reachy_client)
            piper, client = self.server.piper, self.server.reachy_client
            self.runtime.refresh()
            self.client_factory.assert_called_once()
            self.state.update(configured=False, address='', skipped=True)
            self.runtime.refresh()
            self.assertIsNone(other.reachy_client)
            self.assertIsNone(self.server.piper)
            piper.close.assert_called_once()
            client.session.close.assert_called_once()
        finally:
            other.server_close()

    def test_installed_optional_services_stay_visible_while_skipped(self):
        piper = mock.Mock(proc=None, model=Path('/tmp/synthetic-voice.onnx'))
        self.server.piper = piper
        self.server.engine_switcher = None
        self.server.services_config = {'Reachy Mini bridge': {'cmdline_match': 'reachy_mjpeg_bridge.py'}}
        with mock.patch.object(ui, 'find_pid_by_cmdline', return_value=None), \
             mock.patch.object(ui, 'dir_size_mb', return_value=0), \
             mock.patch.object(ui, 'disk_for_path', return_value='test'):
            rows = {row['name']: row for row in self.server.service_list()}
        self.assertFalse(rows['TTS (Piper)']['running'])
        self.assertEqual(rows['TTS (Piper)']['state_detail'], 'Idle · connect Reachy Mini')
        self.assertEqual(rows['Reachy Mini bridge']['state_detail'], 'Not configured')
        piper.synth.assert_not_called()

    def test_cli_transaction_is_not_applied_before_it_finishes(self):
        self.state.update(configured=True, address='192.168.10.20', busy=True)
        self.runtime.refresh()
        self.client_factory.assert_not_called()
        self.state['busy'] = False
        self.runtime.refresh()
        self.client_factory.assert_called_once_with('http://192.168.10.20:8000')

    def test_missing_optional_client_does_not_prevent_ui_status(self):
        self.state.update(configured=True, address='192.168.10.20')
        with mock.patch.object(ui, 'Reachy', None):
            runtime = ui.ReachyRuntime(self.setup)
            runtime.refresh()
        self.server.reachy_runtime = runtime
        code, body = self.request(method='GET')
        self.assertEqual(code, 200)
        self.assertEqual(body['status'], 'error')
        self.assertFalse(body['configured'])
        self.assertIsNone(runtime.client)
        self.assertIn('--reachy-only', body['message'])

    def test_stale_tab_cannot_control_a_newly_selected_robot(self):
        self.state.update(configured=True, address='192.168.10.20')
        self.runtime.refresh()
        for seen_address in ('', '192.168.10.19'):
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
            connection.request('POST', '/api/reachy/action/wake', '{}',
                               {'Content-Type': 'application/json', 'X-Reachy-Address': seen_address})
            response = connection.getresponse()
            self.assertEqual(response.status, 409)
            response.read()
            connection.close()
        self.client_factory.return_value.wake.assert_not_called()

    def test_reconfigure_busy_control_does_not_start_service_operation(self):
        acquired, release = threading.Event(), threading.Event()
        def operation():
            with self.runtime.lock:
                acquired.set()
                release.wait(timeout=4)
        worker = threading.Thread(target=operation)
        worker.start()
        acquired.wait(timeout=2)
        try:
            self.assertEqual(self.request(action='configure', payload={'address': '192.168.10.20'})[0], 409)
            self.setup.configure.assert_not_called()
        finally:
            release.set()
            worker.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
