# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Offline installer failures: temporary fixtures, no root, devices or downloads."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SDK = "95515c2f87fba8982db5a519f9022277667b3cc9"
VISUAL_REVISION = SDK + ":a5804ab79e6210972c6be3ef2c98beee663da0bf0661510d42219d6e109531ed"
HEAVY_STAGES = (
    "fetch_edgellm", "edgellm_venv", "build_edgellm_runtime", "quantize",
    "export_onnx_llm", "export_onnx_visual", "validate_chat_template", "build_engine", "build_visual_layout",
)
ARTIFACTS = (
    "sdk/.venv/bin/python", "sdk/build/examples/llm/llm_build",
    "sdk/build/examples/multimodal/visual_build", "sdk/build/libNvInfer_edgellm_plugin.so",
    "onnx/llm/model.onnx", "onnx/visual/model.onnx",
    "engines/llm.engine", "engines/visual/visual.engine", "engines/chat_template.jinja",
)
ENGINE_READY_STAGES = (
    "system_packages", "pin_clocks", "fetch_repo_self", "jetpack_compute", "fetch_edgellm",
    "edgellm_venv", "build_edgellm_runtime", "download_checkpoint", "fix_raw_config",
    "quantize", "export_onnx_llm", "export_onnx_visual", "validate_chat_template",
)

COMMON = r'''
STATE_DIR="$FIXTURE/state"
EDGELLM_DIR="$FIXTURE/sdk"
EDGELLM_PY="$EDGELLM_DIR/.venv/bin/python"
ONNX_DIR="$FIXTURE/onnx"
RAW_DIR="$FIXTURE/raw"
ENGINE_DIR="$FIXTURE/engines"
BUILD_SWAPFILE="$FIXTURE/build-swap.img"
'''

# Minimal ONNX metadata fixture, not an inference/serialization implementation. The
# real helper reads its objects and validates actual on-disk external tensor ranges.
ONNX_FAKE = r'''
import json
from types import SimpleNamespace

class TensorProto:
    EXTERNAL = 1
    def __init__(self, metadata):
        self.data_location = self.EXTERNAL
        self.external_data = [SimpleNamespace(key=k, value=v) for k, v in metadata.items()]

class Model:
    def __init__(self, data):
        self.graph = SimpleNamespace(node=data["nodes"])
        self.tensors = [TensorProto(item) for item in data["tensors"]]
    def ListFields(self):
        field = SimpleNamespace(type=11, TYPE_MESSAGE=11, is_repeated=True)
        return [(field, self.tensors)]

def load(path, *, load_external_data):
    assert load_external_data is False, "preflight must not load external tensor data"
    with open(path) as handle:
        return Model(json.load(handle))
'''


def revision(stage):
    return VISUAL_REVISION if stage in ("export_onnx_visual", "build_visual_layout") else SDK

# These stand in for all privileged/Linux-specific operations. Sparse fixture files
# use logical 4 GiB sizes without allocating that much disk space or memory.
SWAP_FAKE = r'''
stat() {
  "$TEST_PYTHON" - "$2" "${@: -1}" <<'PY'
import os, sys
s = os.stat(sys.argv[2])
print(s.st_size if sys.argv[1] == "%s" else f"{s.st_dev}:{s.st_ino}")
PY
}
fallocate() {
  echo allocate >> "$FIXTURE/events"
  [[ "${FAIL_ALLOCATE:-0}" == 0 ]] || return 1
  "$TEST_PYTHON" - "$BUILD_SWAPFILE" "$BUILD_SWAP_BYTES" <<'PY'
import sys
with open(sys.argv[1], "r+b") as f:
    f.truncate(int(sys.argv[2]))
PY
}
dd() { echo allocation-fallback >> "$FIXTURE/events"; return 1; }
mkswap() {
  echo initialize >> "$FIXTURE/events"
  [[ "${FAIL_INITIALIZE:-0}" == 0 ]]
}
swapon() {
  if [[ "$1" == --show || "$1" == --show=* ]]; then
    [[ "${FAIL_INVENTORY:-0}" == 0 ]] || return 1
    [[ -f "$FIXTURE/active" ]] || return 0
    if [[ "$1" == --show=NAME,SIZE ]]; then
      cat "$FIXTURE/active"
    elif [[ "$1" == --show && " $* " == *" --output NAME,SIZE "* ]]; then
      # util-linux 2.39.3 has no --output: it abbreviates --options instead.
      # Real Orin output therefore includes TYPE before SIZE, even with --bytes.
      awk '{print $1, "file", $2, "0", "-2", "fixture-uuid"}' "$FIXTURE/active"
    else
      echo 'fixture: unsupported swapon inventory arguments' >&2
      return 2
    fi
    return 0
  fi
  echo activate >> "$FIXTURE/events"
  [[ "${FAIL_ACTIVATE:-0}" == 0 ]] || { echo 'fixture: swapon rejected file' >&2; return 1; }
  [[ "${LIE_ACTIVATE:-0}" == 0 ]] || return 0
  printf '%s %s\n' "$BUILD_SWAPFILE" "$((BUILD_SWAP_BYTES - $(getconf PAGESIZE)))" >> "$FIXTURE/active"
}
swapoff() {
  echo deactivate >> "$FIXTURE/events"
  [[ "${FAIL_DEACTIVATE:-0}" == 0 ]] || return 1
  [[ "${LIE_DEACTIVATE:-0}" == 0 ]] || return 0
  awk -v path="$BUILD_SWAPFILE" '$1 != path' "$FIXTURE/active" > "$FIXTURE/remaining"
  mv "$FIXTURE/remaining" "$FIXTURE/active"
  [[ -s "$FIXTURE/active" ]] || rm -f "$FIXTURE/active"
}
trap 'setup_exit_cleanup "$?"' EXIT
'''


class SetupFailureGuards(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Path(self.temporary.name)
        (self.fixture / "state").mkdir()

    def run_shell(self, command, *, swap=False):
        env = {
            **os.environ, "FIXTURE": str(self.fixture), "TEST_PYTHON": sys.executable,
            "REACHY_MINI_IP": "192.0.2.50", "HF_TOKEN_FILE": "", "HF_TOKEN": "",
            "PYTHONPATH": str(self.fixture / "fake-modules"),
        }
        # Sourcing defines functions but main() is guarded by BASH_SOURCE == $0.
        return subprocess.run(
            ["bash", "-c", 'source "$1"\n' + COMMON + (SWAP_FAKE if swap else "") + command,
             "test", str(ROOT / "scripts/setup-orin.sh")],
            env=env, text=True, capture_output=True, timeout=15,
        )

    def populate_completed_install(self):
        for stage in HEAVY_STAGES:
            (self.fixture / "state" / stage).write_text(revision(stage) + "\n")
        for relative in ARTIFACTS:
            path = self.fixture / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture artifact\n")

    def populate_engine_ready(self):
        for stage in ENGINE_READY_STAGES:
            (self.fixture / "state" / stage).write_text(revision(stage) + "\n")
        for relative in ARTIFACTS[:4]:
            path = self.fixture / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture artifact\n")
            path.chmod(0o755)
        (self.fixture / "sdk/.venv/bin/python").write_text('#!/bin/sh\nexec "$TEST_PYTHON" "$@"\n')
        module_dir = self.fixture / "fake-modules"
        module_dir.mkdir()
        (module_dir / "onnx.py").write_text(ONNX_FAKE)
        raw = self.fixture / "raw"
        raw.mkdir()
        (raw / "chat_template.jinja").write_text("original provider template\n")
        for tower in ("llm", "visual"):
            directory = self.fixture / "onnx" / tower
            directory.mkdir(parents=True)
            (directory / "config.json").write_text('{"model_type": "fixture"}')
            (directory / "model.onnx").write_text(json.dumps({
                "nodes": ["fixture node"],
                "tensors": [{"location": "weights.bin", "offset": "16", "length": "4096"}],
            }))
            (directory / "weights.bin").write_bytes(b"x" * 4112)
        (self.fixture / "onnx/llm/chat_template.jinja").write_bytes((raw / "chat_template.jinja").read_bytes())

    def swap_fixture(self):
        path = self.fixture / "build-swap.img"
        with path.open("wb") as handle:
            handle.truncate(4 * 1024**3)
        return path

    def events(self):
        path = self.fixture / "events"
        return path.read_text().splitlines() if path.exists() else []

    def test_early_or_unrelated_marker_does_not_lower_space_requirement(self):
        for marker in ("system_packages", "unrelated", "build_edgellm_runtime"):
            (self.fixture / "state" / marker).write_text(SDK)
            result = self.run_shell("minimum_free_space_gib")
            self.assertEqual((result.returncode, result.stdout.strip()), (0, "25"), result.stderr)

    def test_current_completed_install_keeps_maintenance_allowance(self):
        self.populate_completed_install()
        result = self.run_shell("minimum_free_space_gib")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "8"), result.stderr)
        # No quantized checkpoint exists: valid for a completed host-export installation.
        self.assertFalse((self.fixture / "checkpoints").exists())

    def test_verified_exports_allow_engine_only_resume_with_12_gib(self):
        self.populate_engine_ready()
        result = self.run_shell("minimum_free_space_gib")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "12"), result.stderr)
        self.assertFalse((self.fixture / "state/build_engine").exists())
        self.assertFalse((self.fixture / "checkpoints").exists())

    def test_engine_resume_requires_every_preceding_stage(self):
        self.populate_engine_ready()
        for stage in ENGINE_READY_STAGES:
            with self.subTest(stage=stage):
                marker = self.fixture / "state" / stage
                marker.unlink()
                self.assertEqual(self.run_shell("minimum_free_space_gib").stdout.strip(), "25")
                marker.write_text(revision(stage) + "\n")
        for stage in (*(s for s in HEAVY_STAGES if s != "build_visual_layout"), "fetch_repo_self"):
            with self.subTest(stale=stage):
                marker = self.fixture / "state" / stage
                marker.write_text("old-sdk\n")
                self.assertEqual(self.run_shell("minimum_free_space_gib").stdout.strip(), "25")
                if stage == "build_engine":
                    marker.unlink()
                else:
                    marker.write_text(revision(stage) + "\n")

    def test_same_sdk_visual_repair_allows_12_gib_without_invalidating_language(self):
        self.populate_engine_ready()
        engine = self.fixture / "engines"
        engine.mkdir()
        (engine / "llm.engine").write_bytes(b"unchanged language engine")
        (engine / "chat_template.jinja").write_bytes((self.fixture / "raw/chat_template.jinja").read_bytes())
        (self.fixture / "state/build_engine").write_text(SDK + "\n")
        (self.fixture / "state/export_onnx_visual").write_text(SDK + "\n")
        result = self.run_shell("minimum_free_space_gib; stage_done build_engine; if stage_done export_onnx_visual; then exit 42; fi")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "12"), result.stderr)
        self.assertEqual((engine / "llm.engine").read_bytes(), b"unchanged language engine")
        (self.fixture / "state/export_onnx_visual").write_text("unrecognized-old-vision\n")
        self.assertEqual(self.run_shell("minimum_free_space_gib").stdout.strip(), "25")

    def test_visual_repair_builds_only_vision_and_keeps_language_engine(self):
        self.populate_engine_ready()
        engine = self.fixture / "engines"
        engine.mkdir()
        (engine / "llm.engine").write_bytes(b"unchanged language engine")
        (self.fixture / "state/build_engine").write_text(SDK + "\n")
        result = self.run_shell('''
pause_inference() { :; }
sudo() {
  [[ "$*" == *"/visual_build "* ]] || { echo UNEXPECTED_BUILD; return 1; }
  echo visual-builder >> "$FIXTURE/events"
  mkdir -p "$ENGINE_DIR/visual"
  printf 'corrected visual engine' > "$ENGINE_DIR/visual/visual.engine"
}
stage build_engine do_build_engine
stage build_visual_layout do_build_visual_layout
''', swap=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("UNEXPECTED_BUILD", result.stdout)
        self.assertEqual((engine / "llm.engine").read_bytes(), b"unchanged language engine")
        self.assertEqual(self.events(), ["allocate", "initialize", "activate", "visual-builder", "deactivate"])
        self.assertEqual((self.fixture / "state/build_visual_layout").read_text().strip(), VISUAL_REVISION)

    def test_fresh_combined_build_records_vision_receipt_without_double_build(self):
        self.populate_engine_ready()
        result = self.run_shell('''
pause_inference() { :; }
sudo() {
  case "$*" in
    *" mkdir -p "*) mkdir -p "$ENGINE_DIR" ;;
    *"/llm_build "*)
      echo language-builder >> "$FIXTURE/events"
      printf 'language engine' > "$ENGINE_DIR/llm.engine"
      cp "$RAW_DIR/chat_template.jinja" "$ENGINE_DIR/chat_template.jinja" ;;
    *"/visual_build "*)
      echo visual-builder >> "$FIXTURE/events"
      mkdir -p "$ENGINE_DIR/visual"
      printf 'visual engine' > "$ENGINE_DIR/visual/visual.engine" ;;
    *) echo UNEXPECTED_BUILD; return 1 ;;
  esac
}
stage build_engine do_build_engine
stage build_visual_layout do_build_visual_layout
''', swap=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events().count("language-builder"), 1)
        self.assertEqual(self.events().count("visual-builder"), 1)
        self.assertEqual((self.fixture / "state/build_visual_layout").read_text().strip(), VISUAL_REVISION)

    def test_visual_export_patch_failure_does_not_export_or_reuse_vision_receipt(self):
        self.populate_engine_ready()
        exporter = self.fixture / "sdk/.venv/bin/tensorrt-edgellm-export"
        exporter.write_text("fixture exporter")
        exporter.chmod(0o755)
        (self.fixture / "state/build_visual_layout").write_text(VISUAL_REVISION)
        for stage in ("enable_services", "smoke_test"):
            (self.fixture / "state" / stage).write_text(SDK)
        (self.fixture / "state/export_onnx_visual").write_text(SDK)
        result = self.run_shell('''
pause_inference() { :; }
sudo() {
  case "$*" in
    *" mkdir -p "*) return 0 ;;
    *"fix_cosmos_visual_layout.py"*) echo fixture-patch-rejected >&2; return 2 ;;
    *) echo MUST_NOT_EXPORT; return 1 ;;
  esac
}
stage export_onnx_visual do_export_onnx_visual
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("MUST_NOT_EXPORT", result.stdout)
        self.assertIn("fixture-patch-rejected", result.stderr)
        self.assertFalse((self.fixture / "state/build_visual_layout").exists())
        for stage in ("enable_services", "smoke_test"):
            self.assertFalse((self.fixture / "state" / stage).exists())
        self.assertEqual((self.fixture / "state/export_onnx_visual").read_text(), SDK)

    def test_failed_visual_repair_retry_restarts_services_and_checks_readiness(self):
        self.populate_engine_ready()
        for stage in ("build_engine", "enable_services", "smoke_test"):
            (self.fixture / "state" / stage).write_text(SDK)
        first = self.run_shell('''
pause_inference() { SHIM_PAUSED=1; }
build_visual_engine_files() { return 42; }
stage build_visual_layout do_build_visual_layout
''', swap=True)
        self.assertEqual(first.returncode, 42, first.stderr)
        self.assertIn("remains stopped", first.stderr)
        for stage in ("build_visual_layout", "enable_services", "smoke_test"):
            self.assertFalse((self.fixture / "state" / stage).exists())
        self.assertEqual((self.fixture / "state/build_engine").read_text(), SDK)
        second = self.run_shell('''
pause_inference() { :; }  # The service is still stopped from the previous process.
build_visual_engine_files() { :; }
do_enable_services() { echo SERVICES_RESTARTED; }
do_smoke_test() { echo READINESS_CHECKED; }
[[ "$SHIM_PAUSED" -eq 0 ]]
stage build_visual_layout do_build_visual_layout
stage enable_services do_enable_services
stage smoke_test do_smoke_test
''', swap=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("SERVICES_RESTARTED", second.stdout)
        self.assertIn("READINESS_CHECKED", second.stdout)

    def test_engine_resume_rejects_missing_or_truncated_external_data(self):
        self.populate_engine_ready()
        for tower in ("llm", "visual"):
            with self.subTest(tower=tower):
                payload = self.fixture / "onnx" / tower / "weights.bin"
                payload.unlink()
                self.assertEqual(self.run_shell("minimum_free_space_gib").stdout.strip(), "25")
                payload.write_bytes(b"x" * 4096)  # length fits only if the offset is ignored
                self.assertEqual(self.run_shell("minimum_free_space_gib").stdout.strip(), "25")
                payload.write_bytes(b"x" * 4112)

    def test_engine_resume_rejects_external_data_outside_export(self):
        self.populate_engine_ready()
        model_path = self.fixture / "onnx/llm/model.onnx"
        model = json.loads(model_path.read_text())
        for location in ("../visual/weights.bin", str(self.fixture / "onnx/visual/weights.bin")):
            model["tensors"][0]["location"] = location
            model_path.write_text(json.dumps(model))
            self.assertEqual(self.run_shell("minimum_free_space_gib").stdout.strip(), "25")

    def test_engine_resume_requires_unchanged_provider_template(self):
        self.populate_engine_ready()
        exported = self.fixture / "onnx/llm/chat_template.jinja"
        original = exported.read_bytes()
        for changed in (b"", b"rewritten template"):
            exported.write_bytes(changed)
            self.assertEqual(self.run_shell("minimum_free_space_gib").stdout.strip(), "25")
        exported.write_bytes(original)
        conflict = self.fixture / "onnx/llm/processed_chat_template.json"
        conflict.write_text("{}")
        self.assertEqual(self.run_shell("minimum_free_space_gib").stdout.strip(), "25")

    def test_engine_resume_rejects_missing_or_invalid_runtime_and_exports(self):
        self.populate_engine_ready()
        for relative in (*ARTIFACTS[:6], "onnx/llm/config.json", "onnx/visual/config.json"):
            with self.subTest(relative=relative):
                path = self.fixture / relative
                original = path.read_bytes()
                path.write_bytes(b"")
                self.assertEqual(self.run_shell("minimum_free_space_gib").stdout.strip(), "25")
                path.write_bytes(original)
        config = self.fixture / "onnx/visual/config.json"
        config.write_text("not json")
        self.assertEqual(self.run_shell("minimum_free_space_gib").stdout.strip(), "25")

    def test_stale_markers_and_missing_artifacts_require_build_headroom(self):
        self.populate_completed_install()
        marker = self.fixture / "state/build_engine"
        for stale in ("", "old-sdk"):
            marker.write_text(stale)
            self.assertEqual(self.run_shell("minimum_free_space_gib").stdout.strip(), "25")
        marker.write_text(SDK)
        for relative in ARTIFACTS:
            artifact = self.fixture / relative
            artifact.write_text("")
            self.assertEqual(self.run_shell("minimum_free_space_gib").stdout.strip(), "25", relative)
            artifact.write_text("fixture artifact\n")

    def test_new_swap_is_verified_then_removed_on_success(self):
        result = self.run_shell("enable_build_swap; echo BUILD; disable_build_swap", swap=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BUILD", result.stdout)
        self.assertEqual(self.events(), ["allocate", "initialize", "activate", "deactivate"])
        self.assertFalse((self.fixture / "build-swap.img").exists())

    def test_swap_inventory_selects_columns_with_show_not_output(self):
        path = self.swap_fixture()
        # Observed on the new Orin: 4 GiB file minus a 4096-byte header page.
        active_bytes = 4294963200
        (self.fixture / "active").write_text(f"{path} {active_bytes}\n")
        result = self.run_shell("build_swap_size_bytes", swap=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(active_bytes), result.stderr)
        self.assertEqual(self.events(), [])
        self.assertTrue(path.exists())

    def test_rejected_or_unverified_activation_stops_before_build(self):
        for flag in ("FAIL_ACTIVATE", "LIE_ACTIVATE"):
            with self.subTest(flag=flag):
                result = self.run_shell(f"{flag}=1; enable_build_swap; echo MUST_NOT_BUILD", swap=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("MUST_NOT_BUILD", result.stdout)
                self.assertIn("no engine build was started", result.stderr)
                self.assertFalse((self.fixture / "build-swap.img").exists())

    def test_real_build_stage_cannot_start_or_mark_complete_after_swap_failure(self):
        result = self.run_shell('''
pause_inference() { SHIM_PAUSED=1; }
sudo() { echo MUST_NOT_INVOKE_BUILD; return 0; }
FAIL_ACTIVATE=1
stage build_engine do_build_engine
''', swap=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("MUST_NOT_INVOKE_BUILD", result.stdout)
        self.assertFalse((self.fixture / "state/build_engine").exists())
        self.assertIn("remains stopped", result.stderr)

    def test_allocation_and_initialization_errors_clean_only_created_file(self):
        for flag in ("FAIL_ALLOCATE", "FAIL_INITIALIZE"):
            with self.subTest(flag=flag):
                result = self.run_shell(f"{flag}=1; enable_build_swap; echo MUST_NOT_BUILD", swap=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("MUST_NOT_BUILD", result.stdout)
                self.assertFalse((self.fixture / "build-swap.img").exists())

    def test_build_failure_cleans_owned_swap_and_keeps_original_status(self):
        result = self.run_shell("enable_build_swap; SHIM_PAUSED=1; exit 42", swap=True)
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("remains stopped", result.stderr)
        self.assertFalse((self.fixture / "build-swap.img").exists())
        self.assertIn("deactivate", self.events())

    def test_swapoff_failure_or_false_success_preserves_active_file(self):
        for flag in ("FAIL_DEACTIVATE", "LIE_DEACTIVATE"):
            with self.subTest(flag=flag):
                # Exercise exit cleanup once, after a failed builder.
                result = self.run_shell(f"enable_build_swap; {flag}=1; exit 42", swap=True)
                self.assertEqual(result.returncode, 42)
                self.assertIn("preserving", result.stderr)
                self.assertTrue((self.fixture / "build-swap.img").exists())
                self.assertTrue((self.fixture / "active").exists())
                (self.fixture / "build-swap.img").unlink()
                (self.fixture / "active").unlink()

    def test_preexisting_active_swap_is_never_disabled_or_deleted(self):
        path = self.swap_fixture()
        (self.fixture / "active").write_text(f"{path} {4 * 1024**3}\n")
        result = self.run_shell("enable_build_swap; disable_build_swap", swap=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), [])
        self.assertTrue(path.exists())
        self.assertTrue((self.fixture / "active").exists())

    def test_preexisting_inactive_file_is_preserved_after_use(self):
        path = self.swap_fixture()
        result = self.run_shell("enable_build_swap; disable_build_swap", swap=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), ["activate", "deactivate"])
        self.assertTrue(path.exists())
        self.assertFalse((self.fixture / "active").exists())

    def test_other_active_swap_is_untouched(self):
        other = "/dev/zram0 1073741824\n"
        (self.fixture / "active").write_text(other)
        result = self.run_shell("enable_build_swap; disable_build_swap", swap=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.fixture / "active").read_text(), other)
        self.assertFalse((self.fixture / "build-swap.img").exists())

    def test_existing_undersized_active_swap_blocks_build_without_changes(self):
        path = self.swap_fixture()
        (self.fixture / "active").write_text(f"{path} 1048576\n")
        result = self.run_shell("enable_build_swap; echo MUST_NOT_BUILD", swap=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("MUST_NOT_BUILD", result.stdout)
        self.assertIn("smaller than the required", result.stderr)
        self.assertTrue(path.exists())
        self.assertEqual(self.events(), [])

    def test_symlink_and_small_existing_file_are_not_rewritten(self):
        path = self.fixture / "build-swap.img"
        target = self.fixture / "unrelated"
        target.write_text("leave me alone")
        path.symlink_to(target)
        result = self.run_shell("enable_build_swap", swap=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_text(), "leave me alone")
        path.unlink()
        path.write_text("partial swap file")
        result = self.run_shell("enable_build_swap", swap=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_text(), "partial swap file")
        self.assertEqual(self.events(), [])

    def test_unavailable_inventory_does_not_create_or_delete_swap(self):
        result = self.run_shell("FAIL_INVENTORY=1; enable_build_swap", swap=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot inspect active swap", result.stderr)
        self.assertEqual(self.events(), [])

    def test_unavailable_inventory_during_cleanup_preserves_owned_file(self):
        result = self.run_shell("enable_build_swap; FAIL_INVENTORY=1; exit 42", swap=True)
        self.assertEqual(result.returncode, 42)
        self.assertIn("Cannot inspect swap during cleanup", result.stderr)
        self.assertTrue((self.fixture / "build-swap.img").exists())
        self.assertTrue((self.fixture / "active").exists())

    def test_replaced_path_is_not_cleaned_up(self):
        result = self.run_shell('''
enable_build_swap
mv "$BUILD_SWAPFILE" "$FIXTURE/owned-original"
printf 'unrelated replacement' > "$BUILD_SWAPFILE"
exit 42
''', swap=True)
        self.assertEqual(result.returncode, 42)
        self.assertIn("path changed", result.stderr)
        self.assertEqual((self.fixture / "build-swap.img").read_text(), "unrelated replacement")
        self.assertNotIn("deactivate", self.events())


if __name__ == "__main__":
    unittest.main()
