#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Preserve Cosmos HWC patch weights for the pinned Edge-LLM 0.11 HWC runtime.

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


SDK_COMMIT = "95515c2f87fba8982db5a519f9022277667b3cc9"
MODULE = Path("tensorrt_edgellm/models/cosmos3_reasoner/modeling_cosmos3_reasoner_visual.py")
ORIGINAL_SHA256 = "59cff8a4450dbd3f26c625236a184005d359a05ff89ee21225e652ab5564e898"
PATCHED_SHA256 = "a5804ab79e6210972c6be3ef2c98beee663da0bf0661510d42219d6e109531ed"


def corrected_source(source):
    """Remove the obsolete loader transform without changing any other weights."""
    tree = ast.parse(source)
    helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef)
               and node.name == "_adapt_patch_embedding_weight_for_chw_input"]
    loaders = [node for node in tree.body if isinstance(node, ast.FunctionDef)
               and node.name == "load_cosmos3_reasoner_visual_weights"]
    if len(helpers) != 1 or len(loaders) != 1:
        raise ValueError("Unexpected Cosmos visual helper/loader structure")
    transforms = [node for node in loaders[0].body if isinstance(node, ast.FunctionDef)
                  and node.name == "_transform"]
    arguments = [kw for node in ast.walk(loaders[0]) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "load_submodule_weights"
                 for kw in node.keywords if kw.arg == "transform"
                 and isinstance(kw.value, ast.Name) and kw.value.id == "_transform"]
    if len(transforms) != 1 or len(arguments) != 1:
        raise ValueError("Unexpected Cosmos visual transform call")
    lines = source.splitlines(keepends=True)
    for node in sorted([helpers[0], transforms[0], arguments[0]],
                       key=lambda item: item.lineno, reverse=True):
        del lines[node.lineno - 1:node.end_lineno]
    result = "".join(lines)
    start = result.index("``input`` carries")
    end = result.index("``fast_pos_embed_idx``", start)
    result = (result[:start]
              + "``input`` contains patches in spatial merge-block order. The Cosmos3\n"
                "runtime flattens each patch in HWC (channel-last) order, matching the\n"
                "checkpoint processor. Keep the original patch-embedding weight columns;\n"
                "do not apply a channel-first permutation during checkpoint loading.\n\n"
              + result[end:])
    ast.parse(result)
    if "_adapt_patch_embedding_weight_for_chw_input" in result:
        raise ValueError("Obsolete patch transform remains referenced")
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
    descriptor, temporary = tempfile.mkstemp(prefix=".cosmos-hwc-", dir=module.parent)
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
    print(f"Cosmos visual layout: {state}; expected HWC source {PATCHED_SHA256}")


if __name__ == "__main__":
    main()
