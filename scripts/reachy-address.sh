# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Sourced by setup-orin.sh and set-reachy-ip.sh: where this Orin keeps its Reachy Mini's address,
# how to read and write it, and what counts as a valid one.
#
# reachy.env is read dynamically by the UI and at startup by the bridge unit.
# The shared settings manager validates changes and restarts only the bridge.

REACHY_ENV_FILE="${INSTALL_DIR:-/opt/live-vision-cosmos-demo}/reachy.env"

# Prints the configured address, or nothing. Parsed rather than sourced: nothing in an
# EnvironmentFile should ever run as shell.
reachy_address_configured() {
  [[ -r "$REACHY_ENV_FILE" ]] || return 0
  sed -n 's/^REACHY_MINI_IP=//p' "$REACHY_ENV_FILE" | tail -1
}

# An IPv4 address or a hostname (reachy-mini.local) and nothing else - no scheme, port, path,
# whitespace or quotes - because it is written into an EnvironmentFile and then into a URL.
reachy_address_valid() {
  local address="$1" octet
  if [[ "$address" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    for octet in "${BASH_REMATCH[@]:1}"; do
      # No leading zeros: some resolvers read 010 as octal.
      [[ "$octet" =~ ^(0|[1-9][0-9]{0,2})$ ]] && (( octet <= 255 )) || return 1
    done
    return 0
  fi
  # All digits and dots but not a dotted quad (1.2.3, say) is no hostname anyone means.
  [[ "$address" =~ ^[0-9.]+$ ]] && return 1
  local label='[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
  (( ${#address} <= 253 )) && [[ "$address" =~ ^$label(\.$label)*$ ]]
}

# Atomically replaces reachy.env with the given (already validated) address.
reachy_address_write() {
  local tmp
  tmp="$(mktemp "$REACHY_ENV_FILE.XXXXXX")"
  printf '%s\n' \
    "# Reachy Mini LAN address, read dynamically by the UI and at bridge startup." \
    "# Change it in Reachy settings or with: bash ${INSTALL_DIR:-/opt/live-vision-cosmos-demo}/scripts/set-reachy-ip.sh <address>" \
    "REACHY_MINI_IP=$1" > "$tmp"
  chmod 0644 "$tmp"
  mv "$tmp" "$REACHY_ENV_FILE"
}
