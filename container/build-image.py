#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Build locally from an approved base digest; deliberately has no push operation."""
import argparse
from pathlib import Path
import re
import subprocess

from bundle import external_output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", required=True, help="Approved NVIDIA JetPack development image @sha256 digest")
    parser.add_argument("--image-id-file", type=Path, required=True, help="Write immutable local image ID outside this source tree")
    parser.add_argument("--candidate-commit", required=True, help="Git commit of this preparation branch used for image and engine")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if not re.fullmatch(r"nvcr\.io/[A-Za-z0-9_./:-]+@sha256:[0-9a-f]{64}", args.base_image):
        parser.error("Supply an approved nvcr.io image pinned by @sha256 digest")
    if not re.fullmatch(r"[0-9a-f]{40}", args.candidate_commit):
        parser.error("Candidate commit must be a full Git SHA")
    try:
        output = external_output(args.image_id_file)
        if output.exists():
            raise ValueError("Choose a new image ID file")
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["docker", "build", "--platform", "linux/arm64", "--build-arg",
                        "BASE_IMAGE=" + args.base_image, "--iidfile", str(output),
                        "--build-arg", "CANDIDATE_COMMIT=" + args.candidate_commit,
                        "-f", str(root / "container/Dockerfile"), str(root)], check=True)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(2, f"Build failed: {exc}\n")


if __name__ == "__main__":
    main()
