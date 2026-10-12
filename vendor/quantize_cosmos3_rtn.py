#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# NVIDIA-authored quantizer distributed under Apache-2.0
# (not part of NVIDIA's public TensorRT-Edge-LLM SDK - there is no upstream RTN quantizer in
# that pin). Apache-2.0; see NOTICE.md. Expects to live at
# <install-dir>/vendor/quantize_cosmos3_rtn.py - it resolves its own project root from
# __file__ two directories up.
#
# One deliberate change from the version it was vendored from: its hf_quant_config.json output
# (quant_algo: "RTN_GPTQ") is NOT a string TensorRT-Edge-LLM's config parser recognizes -
# _algo_to_quant_type() in tensorrt_edgellm/config.py checks for FP8/NVFP4/W4A16_AWQ/AWQ/INT8
# substrings only, no "GPTQ" branch exists there at all, so it silently fell through to
# QUANT_FP16. Worse, that sidecar file's mere EXISTENCE short-circuits detection before the
# parser ever reaches the *correct* GPTQ path (quantization_config.quant_method == "gptq"
# embedded directly in config.json), which does exist and does work. The actual bit-packing
# and zero-point convention this quantizer produces are correct (verified against
# repack_gptq_to_plugin's exact unpacking math) - only the metadata was undiscoverable.
"""Create a CPU-only, group-128 RTN candidate for the pinned Edge-LLM builder.

The algorithm is symmetric round-to-nearest, NOT AWQ or GPTQ optimization.
RTN_GPTQ denotes RTN values in GPTQ-compatible storage. Vision, projector,
embedding and other unquantized tensors are converted to FP16. No calibration,
Torch, CUDA or inference is used. Source files are opened read-only. Without
--apply, print the exact plan. Output must be a new directory.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import resource
import shutil
import struct
import sys
import tempfile
import time

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SOURCE_REVISION = "344d602b128d1bbdacb43b08d0a3626f46343e29"
BACKEND_REVISION = "95515c2f87fba8982db5a519f9022277667b3cc9"
GROUP_SIZE = 128
BLOCK_ELEMENTS = 1 << 20
ALIASES = {"to_q": "q_proj", "to_k": "k_proj", "to_v": "v_proj", "to_out": "o_proj"}
QUANTIZED = re.compile(r"layers\.\d+\.(?:self_attn\.(?:to_q|to_k|to_v|to_out)|mlp\.(?:up_proj|down_proj))\.weight$")
MLP_QUANTIZED = re.compile(r"layers\.\d+\.mlp\.(?:up_proj|down_proj)\.weight$")
SCOPE_COUNTS = {"all-linears": 169, "mlp-only": 56}
ELEMENT_BYTES = {"F16": 2, "I32": 4}


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def save_json(path, value):
    with path.open("x") as output:
        json.dump(value, output, indent=2, allow_nan=False)
        output.write("\n")


def is_quantized(name, quantization_scope="all-linears"):
    if quantization_scope not in SCOPE_COUNTS:
        raise ValueError("Unsupported quantization scope: " + str(quantization_scope))
    if quantization_scope == "mlp-only":
        return bool(MLP_QUANTIZED.fullmatch(name))
    return name == "lm_head.weight" or bool(QUANTIZED.fullmatch(name))


def output_name(name):
    for old, new in ALIASES.items():
        name = name.replace(".self_attn." + old + ".", ".self_attn." + new + ".")
    return name


def excluded_modules(catalog, quantization_scope="all-linears"):
    # Builder graph attention names use q_proj/k_proj/v_proj/o_proj even when
    # plain FP16 checkpoint tensors retain their official to_q/to_k aliases.
    return sorted({output_name(name)[:-7].removeprefix("model.") for name in catalog
                   if name.endswith(".weight") and not is_quantized(name, quantization_scope)})


def quantization_metadata(catalog, quantization_scope="all-linears"):
    return {"producer": {"name": "quantize_cosmos3_rtn", "version": "1"},
            "quantization": {"quant_algo": "RTN_GPTQ", "quantization_algorithm": "round_to_nearest",
                             "quantization_scope": quantization_scope,
                             "checkpoint_format": "gptq", "calibration": None, "group_size": GROUP_SIZE,
                             "sym": True, "zero_point_offset": 1, "kv_cache_quant_algo": None,
                             "exclude_modules": excluded_modules(catalog, quantization_scope)}}


def source_catalog(source, manifest):
    if manifest.get("revision") != SOURCE_REVISION or not manifest.get("complete"):
        raise ValueError("Require the verified official model-download receipt")
    config = json.loads((source / "config.json").read_text())
    if config.get("model_type") != "cosmos3_edge":
        raise ValueError("Expected the original cosmos3_edge model")
    if (source / "hf_quant_config.json").exists():
        raise ValueError("Source must be unquantized")
    for name in ("config.json", "model.safetensors.index.json"):
        if digest_file(source / name) != manifest["files"][name]["sha256"]:
            raise ValueError("Source hash differs: " + name)
    index = json.loads((source / "model.safetensors.index.json").read_text())["weight_map"]
    if len(index) != 698 or sum(is_quantized(name) for name in index) != 169:
        raise ValueError("Unexpected reasoner tensor/linear count")
    catalog, headers, snapshots = {}, {}, {}
    for shard in sorted(set(index.values())):
        path = (source / shard).resolve()
        if not path.is_relative_to(source) or not path.is_file():
            raise ValueError("Invalid source shard: " + shard)
        expected = manifest["files"][shard]
        if path.stat().st_size != expected["size_bytes"]:
            raise ValueError("Source shard size differs: " + shard)
        with path.open("rb") as stream:
            length = struct.unpack("<Q", stream.read(8))[0]
            if length > 4 << 20:
                raise ValueError("Unexpected safetensors header size")
            headers[shard] = (json.loads(stream.read(length)), 8 + length)
        snapshots[shard] = (path.stat().st_size, path.stat().st_mtime_ns)
    for name, shard in sorted(index.items()):
        header, start = headers[shard]
        entry = header[name]
        shape = tuple(entry["shape"])
        begin, end = entry["data_offsets"]
        if (entry["dtype"] != "BF16" or begin < 0 or end - begin != 2 * math.prod(shape)
                or start + end > (source / shard).stat().st_size):
            raise ValueError("Invalid source tensor: " + name)
        if is_quantized(name) and (len(shape) != 2 or shape[0] % 8 or shape[1] % GROUP_SIZE):
            raise ValueError("Unsupported groupwise shape: " + name)
        catalog[name] = {"shard": shard, "offset": start + begin, "shape": shape}
    return catalog, snapshots


def output_catalog(catalog, quantization_scope="all-linears"):
    tensors = {}
    for name, source in catalog.items():
        shape = source["shape"]
        if is_quantized(name, quantization_scope):
            n, k = shape
            prefix = output_name(name)[:-7]
            tensors[prefix + ".qweight"] = {"dtype": "I32", "shape": [k // 8, n]}
            tensors[prefix + ".qzeros"] = {"dtype": "I32", "shape": [k // GROUP_SIZE, n // 8]}
            tensors[prefix + ".scales"] = {"dtype": "F16", "shape": [k // GROUP_SIZE, n]}
        else:
            tensors[name] = {"dtype": "F16", "shape": list(shape)}
    offset = 0
    for name in sorted(tensors):
        entry = tensors[name]
        size = math.prod(entry["shape"]) * ELEMENT_BYTES[entry["dtype"]]
        entry["data_offsets"] = [offset, offset + size]
        offset += size
    return tensors, offset


def bf16_to_f32(raw):
    return (np.asarray(raw, dtype=np.uint32) << np.uint32(16)).view(np.float32)


def quantize_block(values):
    """RTN using the actual stored FP16 scale; bounded group error is checked."""
    values = np.asarray(values, dtype=np.float32)
    if values.ndim != 2 or values.shape[1] % GROUP_SIZE or not np.isfinite(values).all():
        raise ValueError("RTN requires finite [N,K] values with K divisible by 128")
    groups = values.reshape(values.shape[0], -1, GROUP_SIZE)
    maxima = np.max(np.abs(groups), axis=2)
    scales = np.maximum(maxima / np.float32(7), np.float32(2**-24)).astype(np.float16)
    scales[maxima == 0] = np.float16(1)
    if not np.isfinite(scales).all() or not (scales > 0).all():
        raise ValueError("FP16 scale overflow or invalid scale")
    stored_scales = scales.astype(np.float32)[..., None]
    signed = np.clip(np.rint(groups / stored_scales), -7, 7).astype(np.int8)
    reconstructed = signed.astype(np.float32) * stored_scales
    error = groups - reconstructed
    maximum_scaled_error = float(np.max(np.abs(error) / stored_scales))
    if maximum_scaled_error > 0.501:
        raise ValueError("RTN reconstruction exceeds half a quantization step")
    metrics = {"elements": values.size,
               "squared_error": float(np.sum(error.astype(np.float64)**2)),
               "squared_weight": float(np.sum(groups.astype(np.float64)**2)),
               "max_absolute_error": float(np.max(np.abs(error))),
               "max_error_in_scale_units": maximum_scaled_error}
    return (signed.reshape(values.shape).astype(np.int16) + 8).astype(np.uint8), scales, metrics


def pack_rows(unsigned):
    """Unsigned [N,K] nibbles -> little-endian GPTQ int32 [K/8,N]."""
    n, k = unsigned.shape
    packed = np.zeros((k // 8, n), dtype=np.uint32)
    for lane in range(8):
        packed |= unsigned[:, lane::8].T.astype(np.uint32) << np.uint32(lane * 4)
    return packed.view(np.int32)


def merge_metrics(total, current):
    for name in ("elements", "squared_error", "squared_weight"):
        total[name] = total.get(name, 0) + current[name]
    for name in ("max_absolute_error", "max_error_in_scale_units"):
        total[name] = max(total.get(name, 0), current[name])


def mapped_output(path, start, metadata):
    dtype = "<i4" if metadata["dtype"] == "I32" else "<f2"
    return np.memmap(path, dtype=dtype, mode="r+", offset=start + metadata["data_offsets"][0],
                     shape=tuple(metadata["shape"]))


def convert_tensor(source, name, item, destination, data_start, tensors, quantization_scope="all-linears"):
    raw = np.memmap(source / item["shard"], dtype="<u2", mode="r", offset=item["offset"], shape=item["shape"])
    outputs = []
    metrics = {}
    try:
        if is_quantized(name, quantization_scope):
            prefix = output_name(name)[:-7]
            weight, zeros, scales = [mapped_output(destination, data_start, tensors[prefix + suffix])
                                      for suffix in (".qweight", ".qzeros", ".scales")]
            outputs = [weight, zeros, scales]
            zeros[:] = np.int32(0x77777777)  # Stored zero 7 + loader offset 1 -> unsigned zero 8.
            n, k = raw.shape
            rows = max(8, min(256, BLOCK_ELEMENTS // k // 8 * 8))
            for begin in range(0, n, rows):
                end = min(n, begin + rows)
                values = bf16_to_f32(raw[begin:end])
                unsigned, scale, block_metrics = quantize_block(values)
                weight[:, begin:end] = pack_rows(unsigned)
                scales[:, begin:end] = scale.T
                merge_metrics(metrics, block_metrics)
            metrics["relative_l2_error"] = math.sqrt(metrics["squared_error"] / metrics["squared_weight"]) if metrics["squared_weight"] else 0.0
            metrics["rmse"] = math.sqrt(metrics["squared_error"] / metrics["elements"])
        else:
            target = mapped_output(destination, data_start, tensors[name])
            outputs = [target]
            flat_source, flat_target = raw.reshape(-1), target.reshape(-1)
            for begin in range(0, raw.size, BLOCK_ELEMENTS):
                values = bf16_to_f32(flat_source[begin:begin + BLOCK_ELEMENTS]).astype(np.float16)
                if not np.isfinite(values).all():
                    raise ValueError("FP16 conversion overflow/nonfinite: " + name)
                flat_target[begin:begin + BLOCK_ELEMENTS] = values
            del flat_source, flat_target
    finally:
        for target in outputs:
            target.flush()
            target._mmap.close()
        raw._mmap.close()
    return metrics


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "models/cosmos3-edge-reasoner")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quantization-scope", choices=tuple(SCOPE_COUNTS), default="all-linears",
                        help="Quantize all language linears and LM head, or only the two MLP linears per layer")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    source, output = args.source.resolve(), args.output.resolve()
    if not output.is_relative_to(ROOT) or output == ROOT or output == source or source in output.parents:
        raise ValueError("Output must be a separate new directory inside this project")
    if output.exists():
        raise FileExistsError("Output already exists; nothing overwritten: " + str(output))
    manifest = json.loads((ROOT / "results/model-download.json").read_text())
    catalog, snapshots = source_catalog(source, manifest)
    scope = args.quantization_scope
    quantized_count = sum(is_quantized(name, scope) for name in catalog)
    if quantized_count != SCOPE_COUNTS[scope]:
        raise ValueError("Unexpected linear count for quantization scope: " + scope)
    tensors, total_bytes = output_catalog(catalog, scope)
    record = {"algorithm": "round_to_nearest", "dispatch_identifier": "RTN_GPTQ", "checkpoint_format": "gptq",
              "calibration": None, "group_size": GROUP_SIZE, "rounding": "nearest_even", "signed_range": [-7, 7],
              "source": str(source), "output": str(output), "source_revision": SOURCE_REVISION,
              "source_shards": {name: manifest["files"][name] for name in snapshots},
              "backend_revision": BACKEND_REVISION, "numpy_version": np.__version__,
              "source_tensor_count": len(catalog), "quantization_scope": scope,
              "quantized_linear_count": quantized_count,
              "output_tensor_count": len(tensors), "output_tensor_payload_bytes": total_bytes,
              "block_elements_limit": BLOCK_ELEMENTS, "complete": False, "inference_validated": False,
              "note": "Weight reconstruction error is not model answer quality. GPU fit and latency are unmeasured."}
    print(json.dumps(record, indent=2), flush=True)
    if not args.apply:
        return 0
    if shutil.disk_usage(output.parent).free < total_bytes + (512 << 20):
        raise ValueError("Insufficient disk headroom for a separate candidate")
    # Verify all immutable input shards before reading any weights for conversion.
    for shard in snapshots:
        if digest_file(source / shard) != manifest["files"][shard]["sha256"]:
            raise ValueError("Source shard SHA256 differs: " + shard)
    started = time.monotonic()
    temporary = Path(tempfile.mkdtemp(prefix=output.name + ".building-", dir=output.parent))
    record["started_utc"] = datetime.now(timezone.utc).isoformat()
    try:
        header = dict(tensors)
        header["__metadata__"] = {"format": "pt", "algorithm": "round_to_nearest", "checkpoint_format": "gptq",
                                  "source_revision": SOURCE_REVISION}
        encoded = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
        encoded += b" " * (-len(encoded) % 8)
        destination = temporary / "model.safetensors"
        with destination.open("xb") as stream:
            stream.write(struct.pack("<Q", len(encoded)))
            stream.write(encoded)
            stream.truncate(8 + len(encoded) + total_bytes)
        quant_errors = {}
        for number, (name, item) in enumerate(catalog.items(), 1):
            metrics = convert_tensor(source, name, item, destination, 8 + len(encoded), tensors, scope)
            if metrics:
                quant_errors[output_name(name)[:-7]] = metrics
            if number % 25 == 0:
                print(f"Converted {number}/{len(catalog)} tensors", flush=True)
        for shard, before in snapshots.items():
            current = (source / shard).stat()
            if (current.st_size, current.st_mtime_ns) != before:
                raise ValueError("Source changed during conversion: " + shard)
        for name, item in manifest["files"].items():
            if "/" in name or name == "model.safetensors.index.json" or Path(name).suffix not in {".json", ".jinja", ".md"}:
                continue
            if digest_file(source / name) != item["sha256"]:
                raise ValueError("Source metadata SHA256 differs: " + name)
            shutil.copyfile(source / name, temporary / ("SOURCE_MODEL_CARD.md" if name == "README.md" else name))
        # Embed quantization_config directly in config.json with quant_method="gptq" - the
        # ONLY code path TensorRT-Edge-LLM's config parser (_algo_to_quant_type) recognizes
        # for GPTQ-packed weights. Deliberately NOT writing a sidecar hf_quant_config.json:
        # its quant_algo field goes through a different dispatch with no GPTQ branch at all,
        # and checking that file's mere existence short-circuits detection before the parser
        # would ever reach this correct path. group_size matches GROUP_SIZE above; excluded
        # modules are auto-detected by the loader from the checkpoint itself, not declared
        # here, so nothing else is needed for correct dequantization.
        target_config_path = temporary / "config.json"
        target_config = json.loads(target_config_path.read_text())
        target_config["quantization_config"] = {"quant_method": "gptq", "group_size": GROUP_SIZE}
        target_config_path.write_text(json.dumps(target_config, indent=2))
        save_json(temporary / "rtn-quant-provenance.json", quantization_metadata(catalog, scope))
        save_json(temporary / "model.safetensors.index.json", {"metadata": {"total_size": total_bytes},
                  "weight_map": {name: "model.safetensors" for name in sorted(tensors)}})
        record.update(complete=True, completed_utc=datetime.now(timezone.utc).isoformat(),
                      elapsed_seconds=time.monotonic() - started, quantization_errors=quant_errors,
                      model_safetensors_sha256=digest_file(destination),
                      model_safetensors_bytes=destination.stat().st_size,
                      source_config_sha256=digest_file(source / "config.json"),
                      peak_process_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024))
        save_json(temporary / "rtn-conversion.json", record)
        precision = ("Language linears and LM head are INT4; vision, projector, embedding and remaining tensors are FP16."
                     if scope == "all-linears" else
                     "Only language MLP up/down projections are INT4; attention, LM head, vision, projector, embedding and remaining tensors are FP16.")
        card_note = (" SOURCE_MODEL_CARD.md retains the original NVIDIA card and OpenMDW 1.1 license reference."
                     if (temporary / "SOURCE_MODEL_CARD.md").exists() else
                     " The source model card was not downloaded; the model is licensed under OpenMDW 1.1 - see https://huggingface.co/nvidia/Cosmos3-Edge.")
        (temporary / "README.md").write_text("# Cosmos3-Edge CPU RTN candidate\n\nOriginal group-128 symmetric round-to-nearest weights in GPTQ storage (config.json quantization_config: quant_method \"gptq\", group_size 128). No AWQ/GPTQ optimization or calibration. " + precision + " config.json gains that quantization_config and is otherwise copied from the source; tokenizer metadata is unchanged. Intended only for the pinned TensorRT-Edge-LLM direct builder. Inference, answer quality, GPU memory and latency remain unvalidated. See rtn-conversion.json for measured CPU weight errors and source provenance." + card_note + "\n")
        if output.exists():
            raise FileExistsError("Output appeared during conversion; candidate preserved at " + str(temporary))
        temporary.rename(output)
        print(f"CPU conversion complete: {output}; GPU inference remains unvalidated.", flush=True)
        return 0
    except BaseException:
        print("Incomplete candidate preserved at " + str(temporary), file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
