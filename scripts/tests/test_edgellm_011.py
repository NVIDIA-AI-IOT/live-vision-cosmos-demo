# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Offline migration checks; they do not build or validate an Orin engine."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SDK_COMMIT = "95515c2f87fba8982db5a519f9022277667b3cc9"


class SetupMigrationTests(unittest.TestCase):
    def run_setup_function(self, command, *args):
        env = {**os.environ, "REACHY_MINI_IP": "192.0.2.50", "HF_TOKEN_FILE": ""}
        return subprocess.run(
            ["bash", "-c", 'source "$1"; shift; ' + command,
             "test", str(ROOT / "scripts/setup-orin.sh"), *map(str, args)],
            capture_output=True, text=True, env=env)

    def test_backend_pins_agree(self):
        for relative in ("scripts/setup-orin.sh", "scripts/bootstrap.sh", "vendor/quantize_cosmos3_rtn.py"):
            text = (ROOT / relative).read_text()
            self.assertRegex(text, r'(?:EDGELLM_COMMIT|BACKEND_REVISION)\s*=\s*"' + SDK_COMMIT + '"')

    def test_legacy_markers_cannot_reuse_old_native_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_setup_function('''
STATE_DIR="$1"
for name in fetch_repo_self fetch_edgellm edgellm_venv build_edgellm_runtime quantize export_onnx_llm export_onnx_visual validate_chat_template build_engine link_engine enable_services smoke_test; do
  touch "$STATE_DIR/$name"
  if stage_done "$name"; then exit 21; fi
  stage_mark "$name"
  stage_done "$name" || exit 22
done
touch "$STATE_DIR/pin_clocks" "$STATE_DIR/download_checkpoint"
stage_done pin_clocks && stage_done download_checkpoint
''', directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((Path(directory) / "fetch_repo_self").read_text().strip(), SDK_COMMIT)
            self.assertEqual((Path(directory) / "build_engine").read_text().strip(), SDK_COMMIT)

    def test_different_revision_invalidates_engine_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_setup_function('''
STATE_DIR="$1"
stage_mark fetch_repo_self
stage_mark build_engine
EDGELLM_COMMIT=old-sdk
if stage_done fetch_repo_self; then exit 24; fi
if stage_done build_engine; then exit 23; fi
''', directory)
            self.assertEqual(result.returncode, 0, result.stderr)

    def template_check(self, raw, onnx):
        return self.run_setup_function(
            'RAW_DIR="$1"; ONNX_DIR="$2"; EDGELLM_PY="$3"; do_validate_chat_template',
            raw, onnx, sys.executable)

    def test_export_must_preserve_nonempty_provider_template(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "raw"
            onnx = Path(directory) / "onnx"
            raw.mkdir()
            (onnx / "llm").mkdir(parents=True)
            template = b"{{ messages[0].content }}\r\n"
            (raw / "chat_template.jinja").write_bytes(template)
            exported = onnx / "llm/chat_template.jinja"
            exported.write_bytes(template)
            result = self.template_check(raw, onnx)
            self.assertEqual(result.returncode, 0, result.stderr)
            exported.write_bytes(template.replace(b"\r\n", b"\n"))
            result = self.template_check(raw, onnx)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("byte-for-byte", result.stderr)
            exported.write_bytes(template)
            legacy = onnx / "llm/processed_chat_template.json"
            legacy.write_text('{"content_types": {}}')
            result = self.template_check(raw, onnx)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Stale/conflicting", result.stderr)
            legacy.unlink()
            exported.unlink()
            self.assertNotEqual(self.template_check(raw, onnx).returncode, 0)

    def test_foreign_host_export_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            staged = Path(directory) / "llm.host"
            staged.mkdir()
            (staged / ".complete").touch()
            (staged / "model.onnx").touch()
            (staged / ".edgellm-commit").write_text("old-sdk\n")
            result = self.run_setup_function('ONNX_DIR="$1"; adopt_host_export', directory)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Host export SDK does not match", result.stderr)
            self.assertTrue((staged / "model.onnx").is_file())
            self.assertFalse((Path(directory) / "llm").exists())


class FakeApp:
    def route(self, *args, **kwargs):
        return lambda function: function
    get = post = on_event = route


class FakeResponse:
    def __init__(self, content, status_code=200, **kwargs):
        self.content = content
        self.status_code = status_code
        self.body_iterator = content


class FakeMessageContent:
    def __init__(self, kind, content):
        self.type, self.content = kind, content


class FakeMessage:
    def __init__(self, role, contents):
        self.role, self.contents = role, contents


class FakeRequest:
    def __init__(self, messages):
        self.messages = messages
        self.image_buffers = []


class FakeGenerationRequest:
    # Keep these names in sync with the audited v0.11.0 pybind request surface.
    __slots__ = ("requests", "max_generate_length", "temperature", "top_p", "top_k",
                 "apply_chat_template", "add_generation_prompt", "enable_thinking", "stream_channels")


class FakeChannel:
    def __init__(self):
        self.chunks = [types.SimpleNamespace(text="A camera.", token_ids=[11, 12, 13],
                                            prompt_token_count=200, finished=True, reason="stop")]
        self.finished = False

    def set_stream_interval(self, interval):
        self.interval = interval

    def wait_pop(self, timeout):
        self.finished = True
        return self.chunks.pop(0)

    def is_finished(self):
        return self.finished

    def cancel(self):
        self.finished = True


class ShimMigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = types.SimpleNamespace(
            Message=FakeMessage, MessageContent=FakeMessageContent, Request=FakeRequest,
            LLMGenerationRequest=FakeGenerationRequest,
            load_image_from_bytes=lambda raw: ("decoded", raw),
            load_image_from_path=lambda name: ("decoded-file", name),
            StreamChannel=types.SimpleNamespace(create=FakeChannel),
            FinishReason=types.SimpleNamespace(LENGTH="length"))
        modules = {"_edgellm_runtime": self.runtime,
                   "fastapi": types.SimpleNamespace(FastAPI=FakeApp, Request=object),
                   "fastapi.responses": types.SimpleNamespace(JSONResponse=FakeResponse,
                                                                StreamingResponse=FakeResponse)}
        spec = importlib.util.spec_from_file_location("shim_migration_test", ROOT / "shim/cosmos3_shim_v1.py")
        self.shim = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(self.shim)

    def tearDown(self):
        self.shim._pool.shutdown(wait=True)

    def request(self):
        return self.shim._build_request([{"role": "user", "content": [
            {"type": "text", "text": "Describe this image."},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,YWJj"}},
        ]}], 64, 0.0, 0.95)

    def test_caption_request_uses_provider_template_without_thinking(self):
        request = self.request()
        self.assertEqual(request.requests[0].image_buffers, [("decoded", b"abc")])
        self.assertEqual([part.type for part in request.requests[0].messages[0].contents], ["text", "image"])
        self.assertEqual((request.max_generate_length, request.temperature, request.top_p), (64, 0.0, 0.95))
        self.assertTrue(request.apply_chat_template)
        self.assertTrue(request.add_generation_prompt)
        self.assertFalse(request.enable_thinking)

    def test_legacy_engine_fails_before_runtime_construction(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "processed_chat_template.json").write_text("{}")
            with patch.object(self.shim, "ENGINE_DIR", directory):
                with self.assertRaisesRegex(RuntimeError, "rebuild.*v0.11.0"):
                    self.shim._init_runtime()
            self.assertIsNone(self.shim._runtime)

    async def test_stream_keeps_server_native_timing_and_usage_contract(self):
        # Image decoding has finished before this clock starts. Neither the old
        # wall-clock request start nor a client timestamp enters native metrics.
        request = self.request()
        self.shim._runtime = types.SimpleNamespace(handle_request=lambda req: object())
        clock = types.SimpleNamespace(time=lambda: 1000,
                                      monotonic=Mock(side_effect=[100.0, 100.125, 100.5]))
        with patch.object(self.shim, "time", clock):
            response = await self.shim._stream_completion(request, {"stream_options": {"include_usage": True}}, -1000)
            events = [event async for event in response.body_iterator]
        payloads = [json.loads(event[6:]) for event in events if event.startswith("data: {")]
        metrics = next(payload["cosmos_metrics"] for payload in payloads if "cosmos_metrics" in payload)
        self.assertEqual(metrics["timing_boundary"], "native_inference")
        self.assertEqual(metrics["first_text_timing_boundary"], "native_start_to_server_text")
        self.assertEqual(metrics["timing_source"], "server_monotonic")
        self.assertEqual(metrics["native_inference_ms"], 500.0)
        self.assertEqual(metrics["server_first_text_ms"], 125.0)
        usage = next(payload["usage"] for payload in payloads if "usage" in payload)
        self.assertEqual(usage, {"prompt_tokens": 200, "completion_tokens": 3, "total_tokens": 203})
        self.assertEqual(events[-1], "data: [DONE]\n\n")
        self.assertFalse(self.shim._lock.locked())


if __name__ == "__main__":
    unittest.main()
