#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Adapt Cosmos HWC checkpoint weights to the pinned Edge-LLM CHW runtime.

Original compatibility applier; no vendor function bodies are included here.
Reject other SDK revisions and unexpected source changes instead of guessing.
"""
import argparse
import ast
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile


SDK_COMMIT = "71dd1bae032e70771265917ec74d3ff4cad07a10"
MODULE = Path("tensorrt_edgellm/models/cosmos3_reasoner/modeling_cosmos3_reasoner_visual.py")
ORIGINAL_SHA256 = "ae2325a659f57444cf562407c64bcc5f835b76607c53925c7c8dcc789d120f47"
PATCHED_SHA256 = "cf64ded55bd39cd8a1910b47340f5bab47207f12131a8f362525212e5fd55917"


def corrected_source(source):
    """Adapt HWC checkpoint columns to the pinned native CHW patch input."""
    tree = ast.parse(source)
    loaders = [node for node in tree.body if isinstance(node, ast.FunctionDef)
               and node.name == "load_cosmos3_reasoner_visual_weights"]
    if len(loaders) != 1:
        raise ValueError("Unexpected Cosmos visual loader structure")
    calls = [node for node in loaders[0].body if isinstance(node, ast.Expr)
             and isinstance(node.value, ast.Call)
             and isinstance(node.value.func, ast.Name)
             and node.value.func.id == "load_submodule_weights"]
    if len(calls) != 1:
        raise ValueError("Unexpected Cosmos visual weight-loading call")
    lines = source.splitlines(keepends=True)
    correction = """    # Cosmos checkpoints flatten each patch HWC; the native runner supplies CHW.
    # Copy the mapping so callers retain their original checkpoint tensors.
    weights = dict(weights)
    key = "model.visual.embeddings.patch_embedding.weight"
    if key in weights:
        weight = weights[key]
        patch = model.patch_size
        if weight.ndim != 2 or weight.shape[1] != patch * patch * 3:
            raise ValueError("Unexpected Cosmos patch-embedding weight shape")
        weights[key] = (weight.reshape(weight.shape[0], patch, patch, 3)
                       .permute(0, 3, 1, 2).contiguous().reshape_as(weight))

"""
    lines.insert(calls[0].lineno - 1, correction)
    result = "".join(lines)
    ast.parse(result)
    return result


def apply_fix(sdk, check=False):
    sdk = Path(sdk).resolve(strict=True)
    revision = subprocess.check_output(
        ["git", "-C", str(sdk), "rev-parse", "HEAD"], text=True).strip()
    if revision != SDK_COMMIT:
        raise ValueError(f"Visual compatibility fix requires SDK {SDK_COMMIT}; found {revision}")
    module = sdk / MODULE
    if module.is_symlink() or not module.is_file():
        raise ValueError("Expected a regular Cosmos visual source file")
    original = module.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if digest == PATCHED_SHA256:
        return "already corrected"
    if digest != ORIGINAL_SHA256:
        raise ValueError(f"Unexpected Cosmos visual source SHA-256: {digest}; file left unchanged")
    updated = corrected_source(original.decode("utf-8")).encode("utf-8")
    if hashlib.sha256(updated).hexdigest() != PATCHED_SHA256:
        raise ValueError("Compatibility patch did not produce its expected source digest")
    if check:
        return "correction required"
    # Atomic replacement preserves mode and every untouched original notice/line.
    descriptor, temporary = tempfile.mkstemp(prefix=".cosmos-layout-", dir=module.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(updated)
        os.chmod(temporary, module.stat().st_mode & 0o777)
        os.replace(temporary, module)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return "corrected"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sdk", type=Path)
    parser.add_argument("--check", action="store_true", help="validate without modifying SDK source")
    args = parser.parse_args()
    try:
        state = apply_fix(args.sdk, args.check)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Cosmos visual layout: {error}\n")
    print(f"Cosmos visual layout: {state}; expected CHW source {PATCHED_SHA256}")


if __name__ == "__main__":
    main()
