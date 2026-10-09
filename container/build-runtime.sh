#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
[[ $(uname -m) == aarch64 ]] || { echo 'Build on native ARM64 Linux.' >&2; exit 2; }
python3 - <<'PY'
import json, os, pathlib, re, tensorrt
s = json.loads(pathlib.Path('container/runtime-spec.json').read_text())
assert re.fullmatch(r'[0-9a-f]{40}', os.environ.get('CANDIDATE_COMMIT', '')), 'Candidate commit required'
assert tensorrt.__version__ == s['target']['tensorrt'], 'Incompatible base image TensorRT'
assert pathlib.Path('/usr/local/cuda-' + s['target']['cuda'] + '/bin/nvcc').is_file(), 'CUDA development toolkit required'
PY
apt-get update -o Acquire::Retries=3 -o APT::Update::Error-Mode=any
apt-get install -y --no-install-recommends git ca-certificates build-essential cmake ninja-build python3-dev python3-venv
SDK_COMMIT=$(python3 -c 'import json; print(json.load(open("container/runtime-spec.json"))["sdk_commit"])')
git init "$EDGELLM_ROOT"
git -C "$EDGELLM_ROOT" remote add origin https://github.com/NVIDIA/TensorRT-Edge-LLM.git
git -c credential.helper= -C "$EDGELLM_ROOT" fetch --depth 1 origin "$SDK_COMMIT"
git -C "$EDGELLM_ROOT" checkout --detach FETCH_HEAD
git -c credential.helper= -C "$EDGELLM_ROOT" submodule update --init --recursive
python3 -m venv --system-site-packages "$EDGELLM_ROOT/.venv"
python -m pip install --no-cache-dir 'pybind11==3.0.4' 'nvidia-cutlass-dsl[cu13]==4.7.0' \
  'cupy-cuda13x==13.6.0' 'cuda-python>=12.6,<14'
export TRT_PACKAGE_DIR=/usr CUDA_PATH=/usr/local/cuda-13.2
python "$EDGELLM_ROOT/kernelSrcs/build_cutedsl.py" --gpu_arch sm_87 --arch aarch64 \
  --cuda-version 13.2 --kernels fmha,int4_fp16_gemm --jobs 1
cmake -S "$EDGELLM_ROOT" -B "$EDGELLM_ROOT/build" -DCMAKE_BUILD_TYPE=Release \
  -DTRT_PACKAGE_DIR=/usr -DCMAKE_TOOLCHAIN_FILE="$EDGELLM_ROOT/cmake/aarch64_linux_toolchain.cmake" \
  -DEMBEDDED_TARGET=jetson-orin -DCUDA_CTK_VERSION=13.2 '-DENABLE_CUTE_DSL=fmha;int4_fp16_gemm' \
  -DBUILD_PYTHON_BINDINGS=ON -DPython_EXECUTABLE="$EDGELLM_ROOT/.venv/bin/python" \
  -Dpybind11_DIR="$(python -m pybind11 --cmakedir)"
cmake --build "$EDGELLM_ROOT/build" --parallel 1 --target _edgellm_runtime NvInfer_edgellm_plugin
# This shim imports the native binding directly. Keep unrelated SDK server/export
# extras (including PyAV/FFmpeg wheels) out of the fixed-engine image.
python -m pip install --no-cache-dir 'fastapi==0.139.2' 'uvicorn==0.51.0' 'requests==2.34.2'
python -m pip check
PYTHONPATH="$EDGELLM_PYBIND_DIR" python -c 'import _edgellm_runtime as rt; assert hasattr(rt, "LLMRuntime")'
# Record the packages and SDK submodules included in this image.
python -m pip inspect > /opt/runtime-python-packages.json
dpkg-query -W -f='${binary:Package}\t${Version}\n' > /opt/runtime-debian-packages.tsv
git -C "$EDGELLM_ROOT" submodule status --recursive > /opt/runtime-sdk-submodules.txt
apt-get clean
rm -rf /var/lib/apt/lists/*
