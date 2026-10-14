#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Create and verify a separate engine bundle. Hash checks do not prove model accuracy."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import tempfile

APP_ROOT = Path(__file__).resolve().parents[1]
SPEC = Path(__file__).with_name("runtime-spec.json")
REQUIRED = {
    "engines/reasoning/llm.engine",
    "engines/reasoning/config.json",
    "engines/reasoning/embedding.safetensors",
    "engines/reasoning/tokenizer.json",
    "engines/reasoning/tokenizer_config.json",
    "engines/reasoning/visual/visual.engine",
    "engines/reasoning/visual/config.json",
    "engines/reasoning/visual/preprocessor_config.json",
    "engines/reasoning/processed_chat_template.json",
    "model/config.json",
    "model/tokenizer.json",
    "model/processed_chat_template.json",
    "notices/LICENSE.OpenMDW",
    "notices/MODEL_CARD.md",
}


def external_output(path):
    """Keep generated artifacts outside this application and the website's public tree."""
    path = Path(path).expanduser().resolve()
    roots = [APP_ROOT] + [p for p in APP_ROOT.parents if p.name == "public"]
    if any(path.is_relative_to(root) for root in roots):
        raise ValueError("Artifacts must be outside the application and website static assets")
    return path


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inventory(root):
    """Reject links and special files instead of following them out of the bundle."""
    files = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"Symlinks are not allowed: {name}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"Not a regular file: {name}")
        if name == "manifest.json":
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            raise ValueError(f"Hidden files are not engine artifacts: {name}")
        if path.suffix.lower() in {".pem", ".key", ".env", ".sh", ".py"}:
            raise ValueError(f"Unexpected engine artifact: {name}")
        files[name] = {"sha256": digest(path), "size_bytes": path.stat().st_size}
    return files


def verify(root, spec=None, candidate_commit=None):
    root = Path(root)
    manifest_path = root / "manifest.json"
    if root.is_symlink() or manifest_path.is_symlink():
        raise ValueError("Bundle and manifest must not be symlinks")
    manifest = json.loads(manifest_path.read_text())
    expected = json.loads(SPEC.read_text()) if spec is None else spec
    if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("candidate_commit", ""))):
        raise ValueError("Bundle must identify its candidate Git commit")
    if candidate_commit is not None and manifest["candidate_commit"] != candidate_commit:
        raise ValueError("Bundle/image candidate commit mismatch")
    for key in ("schema_version", "sdk_commit", "model", "target", "engine_config", "export_compatibility"):
        if manifest.get(key) != expected[key]:
            raise ValueError(f"Bundle/runtime mismatch: {key}")
    if type(manifest.get("int4_plugin_version")) is not int or manifest["int4_plugin_version"] not in (1, 2):
        raise ValueError("Expected an explicitly selected INT4 plugin version (1 or 2)")
    files = manifest.get("files")
    if not isinstance(files, dict) or not REQUIRED.issubset(files):
        raise ValueError("Bundle lacks required engine, visual engine or checkpoint files")
    for name in files:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or str(path) != name:
            raise ValueError(f"Unsafe manifest path: {name}")
    actual = inventory(root)
    if actual != files:
        changed = sorted(k for k in actual.keys() | files.keys() if actual.get(k) != files.get(k))
        raise ValueError("Bundle checksum/file-set mismatch: " + ", ".join(changed))
    if any((root / name).stat().st_size == 0 for name in REQUIRED):
        raise ValueError("Required engine, template and checkpoint files must be nonempty")
    engine_root = root / "engines/reasoning"
    processed = root / "model/processed_chat_template.json"
    if (engine_root / "processed_chat_template.json").read_bytes() != processed.read_bytes():
        raise ValueError("Engine must retain the exported processed chat template unchanged")
    data = json.loads(processed.read_text())
    if (data.get("content_types", {}).get("image", {}).get("format") != "<|vision_start|><|image_pad|><|vision_end|>"
            or not data.get("generation_prompt")
            or not all(data.get("roles", {}).get(role, {}).get("prefix")
                       and data.get("roles", {}).get(role, {}).get("suffix")
                       for role in ("system", "user", "assistant"))):
        raise ValueError("Incomplete processed chat template")
    roles = data["roles"]
    if roles["user"]["prefix"].startswith(roles["system"]["prefix"] + roles["system"]["suffix"]):
        raise ValueError("Processed chat template repeats the empty system block; run export validation")
    config = json.loads((root / "engines/reasoning/config.json").read_text())
    builder = config.get("builder_config", {})
    if any(builder.get(key) != value for key, value in expected["engine_config"].items()):
        raise ValueError("Engine builder capacities do not match the packaged configuration")
    return manifest


def create(engine, checkpoint, output, plugin_version, notices, candidate_commit):
    engine, checkpoint = map(lambda p: Path(p).expanduser().resolve(), (engine, checkpoint))
    output = external_output(output)
    if output.exists():
        raise ValueError("Output already exists; choose a new directory")
    notices = Path(notices).expanduser().resolve()
    for source in (engine, checkpoint, notices):
        if not source.is_dir() or output.is_relative_to(source):
            raise ValueError("Inputs must be directories separate from the output")
        inventory(source)  # Verify before copying; never dereference a symlink.
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=".engine-bundle-", dir=output.parent))
    try:
        shutil.copytree(engine, scratch / "engines/reasoning", symlinks=True)
        shutil.copytree(checkpoint, scratch / "model", symlinks=True)
        shutil.copytree(notices, scratch / "notices", symlinks=True)
        manifest = json.loads(SPEC.read_text())
        manifest.update({
            "candidate_commit": candidate_commit,
            "int4_plugin_version": plugin_version,
            "files": inventory(scratch),
        })
        (scratch / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        verify(scratch)
        os.rename(scratch, output)
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("create")
    make.add_argument("--engine-dir", required=True)
    make.add_argument("--checkpoint-dir", required=True)
    make.add_argument("--output", required=True)
    make.add_argument("--plugin-version", type=int, choices=(1, 2), required=True)
    make.add_argument("--notices-dir", required=True, help="Verified model license and pinned model card")
    make.add_argument("--candidate-commit", required=True, help="Exact 40-character Git commit used for image and engine build")
    check = commands.add_parser("verify")
    check.add_argument("bundle", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "create":
            print(create(args.engine_dir, args.checkpoint_dir, args.output, args.plugin_version,
                         args.notices_dir, args.candidate_commit))
        else:
            verify(args.bundle)
            print("Checksums and declared compatibility match; accuracy and release approval are separate.")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"Bundle error: {exc}\n")


if __name__ == "__main__":
    main()
