# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Flite process, WAV, and speech API boundaries without a device or speech engine."""

import http.client
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import wave

spec = importlib.util.spec_from_file_location(
    'flite_ui', Path(__file__).resolve().parents[1] / 'scripts/serve_ui.py')
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


class FliteProcess(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.binary = self.root / 'fake flite'
        self.speech = ui.Flite(self.binary, self.root / 'wav')
        self.program()

    def tearDown(self):
        self.speech.close()
        self.temp.cleanup()

    def program(self, mode='ok'):
        self.binary.write_text(f'''#!{sys.executable}
import json, pathlib, sys, time, wave
args = sys.argv[1:]
path = pathlib.Path(args[args.index('-o') + 1])
path.with_suffix('.args').write_text(json.dumps(args))
mode = {mode!r}
if mode == 'slow':
    path.write_bytes(b'partial output')
    time.sleep(0.4)
if mode == 'timeout':
    path.write_bytes(b'partial output')
    time.sleep(20)
if mode == 'failure':
    path.write_bytes(b'partial output')
    sys.exit(1)
if mode == 'invalid':
    path.write_bytes(b'not a WAV' * 20)
    sys.exit(0)
stretch = float(args[args.index('--setf') + 1].split('=')[1])
with wave.open(str(path), 'wb') as wav:
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(16000)
    wav.writeframes(b'\\x01\\x00' * int(160 * stretch))
if mode == 'truncated':
    path.write_bytes(path.read_bytes()[:-20])
''')
        self.binary.chmod(0o755)

    def test_native_rates_literal_text_and_unique_outputs(self):
        text = '--help; $(touch never-created) "caption"'
        files = []
        for rate in ui.SPEECH_RATES:
            with self.subTest(rate=rate):
                wav = self.speech.synth(text, rate=rate)
                self.assertIsNotNone(wav)
                args = json.loads(wav.with_suffix('.args').read_text())
                self.assertEqual(args[args.index('-t') + 1], text)
                self.assertEqual(args[args.index('-voice') + 1], 'slt')
                self.assertAlmostEqual(float(args[1].split('=')[1]), 1 / rate, places=6)
                self.assertTrue(ui.Flite.valid_wav(wav))
                self.assertIsNone(self.speech.proc)
                files.append(wav)
        self.assertEqual(len(set(files)), len(files))
        self.assertFalse((self.root / 'never-created').exists())

    def test_failure_and_invalid_wav_are_cleaned(self):
        for mode in ('failure', 'invalid', 'truncated'):
            with self.subTest(mode=mode):
                self.program(mode)
                self.assertIsNone(self.speech.synth('Caption.'))
                self.assertEqual(list(self.speech.out_dir.glob('*.wav')), [])
                self.assertIsNone(self.speech.proc)

    def test_missing_binary_does_not_leave_scratch_output(self):
        self.binary.unlink()
        self.assertIsNone(self.speech.synth('Caption.'))
        self.assertEqual(list(self.speech.out_dir.glob('*.wav')), [])

    def test_timeout_reaps_child_cleans_output_and_allows_next_request(self):
        self.program('timeout')
        start = time.monotonic()
        self.assertIsNone(self.speech.synth('Caption.', timeout=0.15))
        self.assertLess(time.monotonic() - start, 2)
        self.assertIsNone(self.speech.proc)
        self.assertEqual(list(self.speech.out_dir.glob('*.wav')), [])
        self.program()
        self.assertIsNotNone(self.speech.synth('Next caption.'))

    def test_slow_file_is_not_returned_early_and_concurrent_call_is_rejected(self):
        self.program('slow')
        results = []
        worker = threading.Thread(target=lambda: results.append(self.speech.synth('First.')))
        worker.start()
        deadline = time.monotonic() + 2
        while not list(self.speech.out_dir.glob('*.args')) and time.monotonic() < deadline:
            time.sleep(0.005)
        try:
            self.assertEqual(results, [])
            self.assertIsNone(self.speech.synth('Second.'))
        finally:
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 1)
        self.assertTrue(ui.Flite.valid_wav(results[0]))

    def test_invalid_rates_do_not_start_a_process(self):
        with mock.patch.object(ui.subprocess, 'Popen') as popen:
            for rate in (True, '1', 0, -1, 1.2499, float('nan'), float('inf'), 10 ** 400):
                self.assertIsNone(self.speech.synth('Caption.', rate=rate))
            popen.assert_not_called()

    def test_summary_keeps_full_first_sentence_and_complete_words(self):
        self.assertEqual(ui.Flite.spoken_summary('First sentence. Second sentence.'), 'First sentence.')
        long_sentence = 'a word ' * 100 + 'ends.'
        self.assertEqual(ui.Flite.spoken_summary(long_sentence), long_sentence)
        self.assertEqual(ui.Flite.spoken_summary('one two three four five', max_chars=12), 'one two…')


class SpeechAPI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.wav = Path(self.temp.name) / 'speech.wav'
        wav = self.wav
        class Quiet(ui.Handler):
            def log_message(self, *args):
                pass
            def reachy_result(self, *args):
                # Observe ordering in the handler, not scheduler-dependent client timing.
                self.server.wav_existed_at_reply = wav.exists()
                return super().reachy_result(*args)
        self.server = ui.Server(('127.0.0.1', 0), Quiet, telemetry=mock.Mock())
        self.server.speech = mock.Mock()
        def synth(*args, **kwargs):
            self.wav.write_bytes(b'fake speech bytes')
            return self.wav
        self.server.speech.synth.side_effect = synth
        self.server.reachy_client = mock.Mock()
        self.server.reachy_client.upload_sound.return_value = (True, 'remote.wav')
        self.server.reachy_client.play_sound.return_value = (True, 'played')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, body):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.request('POST', '/api/reachy/speak', json.dumps(body),
                     {'Content-Type': 'application/json', 'X-Reachy-Token': ui.RELAY_TOKEN})
        res = conn.getresponse()
        data = json.loads(res.read())
        conn.close()
        return res.status, data

    def test_success_preserves_route_summary_rate_and_cleanup(self):
        status, data = self.request({'text': 'First caption. More detail.', 'rate': 1.5})
        self.assertEqual(status, 200, data)
        self.server.speech.synth.assert_called_once_with('First caption.', rate=1.5)
        self.server.reachy_client.upload_sound.assert_called_once_with(b'fake speech bytes', 'speech.wav')
        self.server.reachy_client.play_sound.assert_called_once_with('remote.wav')
        self.assertFalse(self.server.wav_existed_at_reply)
        self.assertFalse(self.wav.exists())

    def test_upload_failure_cleans_output_and_does_not_play(self):
        self.server.reachy_client.upload_sound.return_value = (False, 'unreachable')
        status, _ = self.request({'text': 'Caption.'})
        self.assertEqual(status, 400)
        self.assertFalse(self.server.wav_existed_at_reply)
        self.assertFalse(self.wav.exists())
        self.server.reachy_client.play_sound.assert_not_called()

    def test_synthesis_failure_does_not_upload(self):
        self.server.speech.synth.side_effect = None
        self.server.speech.synth.return_value = None
        self.assertEqual(self.request({'text': 'Caption.'})[0], 502)
        self.server.reachy_client.upload_sound.assert_not_called()

    def test_invalid_rates_and_empty_text_do_not_synthesize(self):
        for rate in (True, '1', 1.2499, 0, float('nan'), float('inf'), 10 ** 400):
            with self.subTest(rate=rate):
                self.assertEqual(self.request({'text': 'Caption.', 'rate': rate})[0], 400)
        self.assertEqual(self.request({'text': ''})[0], 400)
        self.server.speech.synth.assert_not_called()

    def test_speech_and_robot_are_optional(self):
        self.server.reachy_client = None
        self.assertEqual(self.request({'text': 'Caption.'})[0], 503)
        self.server.speech.synth.assert_not_called()
        self.server.speech = None
        self.assertEqual(self.request({'text': 'Caption.'})[0], 503)


if __name__ == '__main__':
    unittest.main()
