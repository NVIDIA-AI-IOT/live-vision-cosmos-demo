# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pinned visual-layout compatibility patch: original fixtures, no GPU or SDK install."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("visual_layout_fix", ROOT / "scripts/fix_cosmos_visual_layout.py")
layout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(layout)

SOURCE = '''# Original fixture notice must remain intact.
# SPDX-License-Identifier: Apache-2.0
"""Synthetic visual module.
``input`` carries a fixture's obsolete layout description.

``fast_pos_embed_idx`` is another fixture description.
"""

def untouched(value):
    return value

def load_cosmos3_reasoner_visual_weights(model, weights):
    def _remap(key):
        return key
    load_submodule_weights(model,
                           weights,
                           _remap,
                           label="fixture")
    return weights
'''


class CosmosVisualLayoutFix(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.sdk = Path(temporary.name)
        self.module = self.sdk / layout.MODULE
        self.module.parent.mkdir(parents=True)
        self.module.write_text(SOURCE)
        self.module.chmod(0o640)
        self.patched = layout.corrected_source(SOURCE)
        for name, value in (
            ("ORIGINAL_SHA256", hashlib.sha256(SOURCE.encode()).hexdigest()),
            ("PATCHED_SHA256", hashlib.sha256(self.patched.encode()).hexdigest()),
        ):
            context = patch.object(layout, name, value)
            context.start()
            self.addCleanup(context.stop)
        git = patch.object(layout.subprocess, "check_output", return_value=layout.SDK_COMMIT + "\n")
        self.git = git.start()
        self.addCleanup(git.stop)

    def test_patch_is_idempotent_and_retains_notices_and_mode(self):
        self.assertEqual(layout.apply_fix(self.sdk), "corrected")
        self.assertEqual(self.module.read_text(), self.patched)
        self.assertEqual(self.module.stat().st_mode & 0o777, 0o640)
        self.assertTrue(self.patched.startswith(SOURCE.split('"""')[0]))
        self.assertEqual(layout.apply_fix(self.sdk), "already corrected")
        self.assertEqual(list(self.module.parent.glob(".cosmos-layout-*")), [])

    def test_loader_reorders_only_patch_weights_and_keeps_original_mapping(self):
        import numpy as np
        class Tensor:
            def __init__(self, values):
                self.values = np.asarray(values)
                self.shape, self.ndim = self.values.shape, self.values.ndim
            def reshape(self, *shape): return Tensor(self.values.reshape(*shape))
            def permute(self, *order): return Tensor(self.values.transpose(*order))
            def contiguous(self): return self
            def reshape_as(self, other): return self.reshape(*other.shape)
        observed = {}
        def load_weights(model, weights, remap, **kwargs):
            observed.update(weights=weights, kwargs=kwargs)
        namespace = {"load_submodule_weights": load_weights}
        exec(self.patched, namespace)
        key = "model.visual.embeddings.patch_embedding.weight"
        original = Tensor(np.arange(24).reshape(2, 12))
        bias = object()
        weights = {key: original, "bias": bias}
        model = type("Model", (), {"patch_size": 2})()
        namespace["load_cosmos3_reasoner_visual_weights"](model, weights)
        converted = observed["weights"][key].values
        expected = original.values.reshape(2, 2, 2, 3).transpose(0, 3, 1, 2).reshape(2, 12)
        np.testing.assert_array_equal(converted, expected)
        hwc = np.arange(12)
        chw = hwc.reshape(2, 2, 3).transpose(2, 0, 1).reshape(12)
        np.testing.assert_array_equal(converted @ chw, original.values @ hwc)
        self.assertIs(weights[key], original)
        self.assertIs(observed["weights"]["bias"], bias)
        self.assertEqual(observed["kwargs"]["label"], "fixture")

    def test_check_mode_does_not_write(self):
        self.assertEqual(layout.apply_fix(self.sdk, check=True), "correction required")
        self.assertEqual(self.module.read_text(), SOURCE)

    def test_other_sdk_revision_is_rejected_without_changes(self):
        self.git.return_value = "unexpected-sdk\n"
        with self.assertRaisesRegex(ValueError, "requires SDK"):
            layout.apply_fix(self.sdk)
        self.assertEqual(self.module.read_text(), SOURCE)

    def test_unexpected_source_changes_are_rejected_without_changes(self):
        changed = SOURCE + "\n# local modification\n"
        self.module.write_text(changed)
        with self.assertRaisesRegex(ValueError, "Unexpected.*SHA-256"):
            layout.apply_fix(self.sdk)
        self.assertEqual(self.module.read_text(), changed)

    def test_symlink_source_is_not_modified(self):
        other = self.sdk / "unrelated.py"
        other.write_text(SOURCE)
        self.module.unlink()
        self.module.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "regular"):
            layout.apply_fix(self.sdk)
        self.assertEqual(other.read_text(), SOURCE)

    def test_installer_receipt_matches_applier_patch_digest(self):
        # The patched digest is mocked for synthetic fixtures; read its pinned
        # declaration separately to catch installer/applier version drift.
        applier = (ROOT / "scripts/fix_cosmos_visual_layout.py").read_text()
        digest = applier.split('PATCHED_SHA256 = "', 1)[1].split('"', 1)[0]
        self.assertIn(f'COSMOS_VISUAL_LAYOUT_SHA256="{digest}"',
                      (ROOT / "scripts/setup-orin.sh").read_text())


if __name__ == "__main__":
    unittest.main()
