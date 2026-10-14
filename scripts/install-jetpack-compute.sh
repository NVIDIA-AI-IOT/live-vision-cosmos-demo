#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Minimal, previously exercised R39.2.1 compute set for this demo; no OS upgrade.
set -euo pipefail
[[ $EUID -eq 0 && $(dpkg --print-architecture) == arm64 ]] || { echo 'Run on the Orin with sudo.' >&2; exit 2; }
[[ $(dpkg-query -W -f='${Version}' nvidia-l4t-core) == 39.2.1-* ]] \
  || { echo 'This compute recipe requires JetPack 7.2.1 / L4T 39.2.1. See docs/jetpack-sd-mac.md.' >&2; exit 2; }
. /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 ]] || exit 2
packages=(
  cuda-nvcc-13-2=13.2.86-1
  cuda-cudart-dev-13-2=13.2.86-1
  cuda-driver-dev-13-2=13.2.86-1
  cuda-nvrtc-dev-13-2=13.2.86-1
  libcurand-dev-13-2=10.4.2.66-1
  libcublas-13-2=13.4.1.3-1
  libcufft-13-2=12.2.0.57-1
  libcusolver-13-2=12.2.0.11-1
  libcusparse-13-2=12.7.10.12-1
  libnvjitlink-13-2=13.2.86-1
  libnvinfer-dev=10.16.2.10-1+cuda13.2
  libnvinfer-plugin-dev=10.16.2.10-1+cuda13.2
  libnvonnxparsers-dev=10.16.2.10-1+cuda13.2
  python3-libnvinfer=10.16.2.10-1+cuda13.2
)
missing=0
for spec in "${packages[@]}"; do
  installed=$(dpkg-query -W -f='${Status} ${Version}' "${spec%%=*}" 2>/dev/null || true)
  [[ "$installed" == "install ok installed ${spec#*=}" ]] || missing=1
done
if [[ $missing -eq 1 ]]; then
  apt-get update -o Acquire::Retries=3 -o APT::Update::Error-Mode=any
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}"
fi
/usr/local/cuda-13.2/bin/nvcc --version
python3 -c 'import tensorrt; assert tensorrt.__version__.startswith("10.16.2"), tensorrt.__version__'
