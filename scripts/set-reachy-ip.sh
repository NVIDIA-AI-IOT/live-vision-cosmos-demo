#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Shared Reachy Mini setup CLI. Changes only robot configuration/bridge state, never
# the model engine. Run on the Orin as the deployment owner (sudo is also accepted).
set -euo pipefail

INSTALL_DIR=/opt/live-vision-cosmos-demo
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/reachy-address.sh"

die() { printf 'set-reachy-ip: %s\n' "$*" >&2; exit 1; }
usage() {
  echo "Usage: bash $0 [<robot-address> | --discover | --skip]"
  echo "No argument shows status. Use the UI's Reachy Mini setup for the same actions."
}

main() {
  local action=status address="" owner
  [[ "$#" -le 1 ]] || { usage >&2; return 2; }
  case "${1:-}" in
    '') ;;
    --discover) action=discover ;;
    --skip) action=skip ;;
    -h|--help) usage; return ;;
    --force) die "Validation cannot be bypassed. Verify the robot address or use --skip." ;;
    -*) die "Unknown option: $1" ;;
    *) address="$1"; action=connect
       reachy_address_valid "$address" || die "Use a robot IP address or hostname, without scheme, port or path." ;;
  esac
  local manager="$INSTALL_DIR/ui/scripts/reachy_setup.py"
  [[ -f "$manager" ]] || die "Refresh this project, then prepare robot support: sudo -E bash $INSTALL_DIR/scripts/setup-orin.sh --reachy-only"
  if [[ "$action" == connect || "$action" == skip ]]; then
    [[ -x "$INSTALL_DIR/reachy_env/bin/python3" \
        && -f /etc/systemd/system/reachy-mjpeg-bridge.service ]] \
      && grep -qs -- '--reachy-config' /etc/systemd/system/live-vision-cosmos-demo-ui.service \
      || die "Prepare optional robot support first: sudo -E bash $INSTALL_DIR/scripts/setup-orin.sh --reachy-only"
  fi
  local command=(/usr/bin/python3 "$manager" --config "$REACHY_ENV_FILE" "$action")
  [[ -z "$address" ]] || command+=("$address")
  if [[ "$EUID" -eq 0 ]]; then
    owner="$(stat -c %U "$INSTALL_DIR")"
    [[ "$owner" != root ]] || die "The deployment must be owned by its non-root service account."
    exec runuser -u "$owner" -- "${command[@]}"
  fi
  exec "${command[@]}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
