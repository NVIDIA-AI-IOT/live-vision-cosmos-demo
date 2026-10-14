#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Keep PyAV out of the native shim's SDK environment without importing it."""

import argparse
import importlib
from importlib import metadata, util
from pathlib import Path
import sys


class DependencyError(RuntimeError):
    pass


def av_distributions():
    return [dist for dist in metadata.distributions()
            if (dist.metadata.get("Name") or "").lower() == "av"]


def require_absent():
    importlib.invalidate_caches()
    problems = []
    if av_distributions():
        problems.append("installed av distribution metadata")
    spec = util.find_spec("av")
    if spec is not None:
        problems.append(f"importable av at {spec.origin or spec.submodule_search_locations}")
    for entry in sys.path:
        bundled = Path(entry or ".") / "av.libs"
        if bundled.exists():
            problems.append(f"bundled libraries at {bundled}")
    if problems:
        raise DependencyError("PyAV remains visible: " + "; ".join(problems)
                              + ". Use a clean TensorRT-Edge-LLM virtual environment with the "
                              "base/tools dependencies, without the server extra; remove inherited "
                              "PyAV paths from PYTHONPATH. This check changes no packages or files.")


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        require_absent()
    except DependencyError as exc:
        sys.exit(f"SDK Python dependency check failed: {exc}")
    print("SDK Python dependency check passed: PyAV and av.libs are absent.")


if __name__ == "__main__":
    main()
