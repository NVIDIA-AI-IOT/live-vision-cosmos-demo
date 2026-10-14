#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Let this Orin restart the Reachy Mini's daemon, and nothing else, when the daemon wedges.
#
# Run ON THE ORIN, once per robot (and again after the robot's OS is reinstalled), as the account
# the bridge runs as (User= in reachy-mjpeg-bridge.service) - not root: the key and the robot's
# known_hosts entry must be that account's. Needs sshpass (sudo apt install sshpass).
#
#   bash /opt/live-vision-cosmos-demo/reachy/setup_robot_recovery.sh [pollen@<robot-address>] [orin-address-the-robot-sees]
#
# The robot defaults to pollen@ the address this Orin is configured for (REACHY_MINI_IP, kept in
# /opt/live-vision-cosmos-demo/reachy.env by setup-orin.sh and scripts/set-reachy-ip.sh). After
# switching robots, run it again for the new one: the key is installed per robot.
#
# Installing the key does nothing by itself: the bridge only uses it when started with
# --recover-ssh and --recover-key, which the shipped unit doesn't pass. This script prints the
# systemd drop-in that turns it on when it finishes (a drop-in, because setup-orin.sh rewrites
# the main unit). Until that's in place a wedged daemon still needs a power-cycle.
#
# Why this exists. Daemon 1.11 leaks sockets on every WebRTC session (TURN refreshes libnice never
# closes) and runs under the default soft limit of 1024 descriptors. Once it reaches the limit
# ("Too many open files") every new viewer fails, each failed setup stops the robot's camera
# pipeline, and neither the daemon's restart API nor a media release helps: both run inside the
# same process. Measured 2026-09-24: 1014 of 1024 descriptors in use, 778 of them sockets. Only a
# new process recovers, and a person was needed to power-cycle the robot.
#
# What it changes:
#   Orin   ~/.ssh/reachy_recover_ed25519 (created if missing, in the account running this
#          script), the robot's host key in known_hosts.
#   Robot  one line in ~pollen/.ssh/authorized_keys for that key: `restrict` (no pty, forwarding,
#          agent, X11 or user rc), `from=` the Orin's address only, and a forced command - whatever
#          the client asks for, the key runs `sudo -n systemctl restart --no-block
#          reachy-mini-daemon` and nothing else. --no-block returns once systemd has queued it; the
#          bridge then watches for the new daemon pid. pollen has passwordless sudo on the stock
#          image. Re-running replaces this line (matched by its comment), so it also repairs it.
#          /etc/systemd/system/reachy-mini-daemon.service.d/live-vision-cosmos-demo-nofile.conf
#          raising the daemon's descriptor limit to 16384, so the leak takes 16x longer to wedge
#          it. Takes effect at the daemon's next restart.
#
# It prompts for pollen's password once (the stock image uses "root") and never stores it.
#
# After reinstalling the robot's OS its host key changes, and both this script (accept-new) and the
# bridge (StrictHostKeyChecking=yes) will refuse it - as they should. Read the new fingerprint at
# the robot itself (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`), then on the Orin run
# `ssh-keygen -R <robot-address>`, re-run this script and compare the fingerprint it shows first.
set -euo pipefail

configured="$(sed -n 's/^REACHY_MINI_IP=//p' /opt/live-vision-cosmos-demo/reachy.env 2>/dev/null | tail -1 || true)"
address="${REACHY_MINI_IP:-$configured}"
ROBOT="${1:-${address:+pollen@$address}}"
[ -n "$ROBOT" ] || { echo "no robot given and none configured on this Orin - pass pollen@<robot-address>" >&2; exit 1; }
command -v sshpass >/dev/null || { echo "sshpass is needed to log in to the robot once with its password: sudo apt install sshpass" >&2; exit 1; }
[ "$(id -u)" -ne 0 ] || { echo "run this as the bridge's service account, not root - the key and known_hosts entry must be that account's" >&2; exit 1; }
# The address the robot sees this Orin connect from, for the key's from= restriction.
FROM="${2:-$(ip -4 route get "${ROBOT#*@}" | sed -n 's/.* src \([0-9.]*\).*/\1/p')}"
[ -n "$FROM" ] || { echo "cannot tell which address reaches ${ROBOT#*@}; pass it as argument 2" >&2; exit 1; }
TAG=live-vision-cosmos-demo-reachy-recover
KEY="$HOME/.ssh/reachy_recover_ed25519"
UNIT=reachy-mini-daemon.service
NOFILE=16384

[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C "$TAG" -f "$KEY"
PUB=$(cat "$KEY.pub")
ENTRY="restrict,from=\"$FROM\",command=\"sudo -n /usr/bin/systemctl restart --no-block $UNIT\" $PUB"

read -rsp "password for $ROBOT: " SSHPASS; echo
export SSHPASS
sshpass -e ssh -o StrictHostKeyChecking=accept-new -o PubkeyAuthentication=no "$ROBOT" bash -s <<REMOTE
set -euo pipefail
install -d -m 700 ~/.ssh
ak=~/.ssh/authorized_keys
touch "\$ak"
# A file without a final newline would glue the new entry onto the last key, where sshd reads it as
# that key's comment and never authorizes it. Terminate it first.
[ -s "\$ak" ] && [ -n "\$(tail -c1 "\$ak")" ] && echo >> "\$ak"
# Rewrite rather than append: replaces an earlier entry from this script (old options or an old
# key), so a re-run repairs it. Only lines that START with this script's own options and end in
# its tag go: a line where our entry was once glued onto someone else's key starts with that key,
# and stays.
tmp=\$(mktemp ~/.ssh/ak.XXXXXX)
grep -vE "^(restrict,|command=).* $TAG\$" "\$ak" > "\$tmp" || true
printf '%s\n' '$ENTRY' >> "\$tmp"
chmod 600 "\$tmp" && mv "\$tmp" "\$ak"
sudo -n install -d /etc/systemd/system/$UNIT.d
printf '[Service]\n# The daemon leaks sockets per WebRTC session; 1024 wedged it (see\n# reachy/setup_robot_recovery.sh in this repo).\nLimitNOFILE=$NOFILE\n' \
  | sudo -n tee /etc/systemd/system/$UNIT.d/live-vision-cosmos-demo-nofile.conf >/dev/null
sudo -n systemctl daemon-reload
echo "robot: key installed (from=$FROM), LimitNOFILE=$NOFILE staged (applies at the next daemon restart)"
REMOTE
unset SSHPASS
echo "orin: key $KEY. Test (restarts the robot daemon):  ssh -i $KEY $ROBOT"
# ${REACHY_MINI_IP} below is systemd's expansion from reachy.env, not this shell's, so the
# recovery target follows scripts/set-reachy-ip.sh - after a switch, re-run this script for the
# new robot. StateDirectory= keeps the bridge's 15-minute restart limit across bridge restarts.
cat <<EOF

Now turn recovery on in the bridge (a drop-in, so setup-orin.sh re-runs keep it):

sudo mkdir -p /etc/systemd/system/reachy-mjpeg-bridge.service.d
sudo tee /etc/systemd/system/reachy-mjpeg-bridge.service.d/10-recover.conf >/dev/null <<'UNIT'
[Service]
StateDirectory=reachy-mjpeg-bridge
ExecStart=
ExecStart=/opt/live-vision-cosmos-demo/reachy_env/bin/python3 /opt/live-vision-cosmos-demo/reachy/reachy_mjpeg_bridge.py --robot-host \${REACHY_MINI_IP} --listen 127.0.0.1 --listen-port 8099 --fps 5 --recover-ssh pollen@\${REACHY_MINI_IP} --recover-key $KEY
UNIT
sudo systemctl daemon-reload && sudo systemctl restart reachy-mjpeg-bridge
EOF
