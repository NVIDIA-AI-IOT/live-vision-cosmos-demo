#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
DEMO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if git -C "$DEMO_ROOT" rev-parse HEAD >/dev/null 2>&1; then
  # A website checkout contains this app as a subtree; never transfer the entire site.
  SOURCE_PREFIX="$(git -C "$DEMO_ROOT" rev-parse --show-prefix)"
  GIT_ROOT="$(git -C "$DEMO_ROOT" rev-parse --show-toplevel)"
  ARCHIVE_REF=HEAD
  [[ -z "$SOURCE_PREFIX" ]] || ARCHIVE_REF="HEAD:${SOURCE_PREFIX%/}"
  git -C "$GIT_ROOT" archive --format=tar "$ARCHIVE_REF"
else
  # A source download has no Git metadata. Keep generated and private files out.
  tar --exclude=.git --exclude='.env*' --exclude=reachy.env --exclude=reachy-setup.json \
    --exclude=reachy-setup.lock --exclude='.reachy-*' --exclude=.venv --exclude=tls --exclude=engines \
    --exclude=checkpoints --exclude=external --exclude=data --exclude=results \
    --exclude=piper --exclude=reachy_env --exclude='.setup-state' \
    --exclude='*.part' --exclude='*.pem' --exclude='*.key' --exclude='*.engine' \
    --exclude='*.safetensors' --exclude='*.onnx' --exclude='*.onnx.data' \
    --exclude='__pycache__' -cf - -C "$DEMO_ROOT" .
fi
