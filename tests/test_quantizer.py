# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Synthetic CPU regression for INT4 export metadata; not an inference-quality test."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

spec = importlib.util.spec_from_file_location('quantizer', Path(__file__).resolve().parents[1] / 'vendor/quantize_cosmos3_rtn.py')
quantizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quantizer)


class QuantizerRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'raw'
        self.source.mkdir()
        names = ['lm_head.weight']
        for layer in range(28):
            names += [f'layers.{layer}.self_attn.{part}.weight' for part in ['to_q', 'to_k', 'to_v', 'to_out']]
            names += [f'layers.{layer}.mlp.{part}.weight' for part in ['up_proj', 'down_proj']]
        names += [f'other.{i}.weight' for i in range(698-len(names))]
        header, payload, weight_map = {}, bytearray(), {}
        for name in names:
            shape = [8, 128] if quantizer.is_quantized(name) else [8]
            values = np.linspace(-1, 1, int(np.prod(shape)), dtype=np.float32)
            raw = (values.view(np.uint32) >> 16).astype('<u2').tobytes()
            header[name] = {'dtype': 'BF16', 'shape': shape, 'data_offsets': [len(payload), len(payload)+len(raw)]}
            payload.extend(raw)
            weight_map[name] = 'model.safetensors'
        encoded = json.dumps(header).encode()
        (self.source / 'model.safetensors').write_bytes(struct.pack('<Q', len(encoded))+encoded+payload)
        (self.source / 'config.json').write_text(json.dumps({'model_type': 'cosmos3_edge'}))
        (self.source / 'model.safetensors.index.json').write_text(json.dumps({'weight_map': weight_map}))
        self.hashes = {p.name: quantizer.digest_file(p) for p in self.source.iterdir()}
        receipt = {'revision': quantizer.SOURCE_REVISION, 'complete': True,
                   'files': {p.name: {'sha256': self.hashes[p.name], 'size_bytes': p.stat().st_size}
                             for p in self.source.iterdir()}}
        (self.root / 'results').mkdir()
        (self.root / 'results/model-download.json').write_text(json.dumps(receipt))

    def test_embedded_gptq_metadata_and_packed_weights(self):
        output = self.root / 'quantized'
        with patch.object(quantizer, 'ROOT', self.root), contextlib.redirect_stdout(io.StringIO()):
            quantizer.main(['--source', str(self.source), '--output', str(output), '--apply'])
        config = json.loads((output / 'config.json').read_text())
        self.assertEqual(config['quantization_config'], {'quant_method': 'gptq', 'group_size': 128})
        self.assertFalse((output / 'hf_quant_config.json').exists(), 'This sidecar takes precedence and can silently dispatch FP16')
        with (output / 'model.safetensors').open('rb') as source:
            header = json.loads(source.read(struct.unpack('<Q', source.read(8))[0]))
        self.assertEqual(header['lm_head.qweight']['dtype'], 'I32')
        self.assertEqual(header['lm_head.qweight']['shape'], [16, 8])
        self.assertNotIn('lm_head.weight', header)
        self.assertEqual(self.hashes, {p.name: quantizer.digest_file(p) for p in self.source.iterdir()})

    def test_corrupt_source_stops_before_conversion(self):
        with (self.source / 'model.safetensors').open('r+b') as stream:
            stream.seek(-1, 2)
            stream.write(b'X')
        output = self.root / 'quantized'
        with patch.object(quantizer, 'ROOT', self.root), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'SHA256 differs'):
                quantizer.main(['--source', str(self.source), '--output', str(output), '--apply'])
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
