---
name: flash-jetpack-sd-mac
description: Prepare a clean JetPack 7.2.1 SD card for an Orin Nano Super developer kit from an Apple Silicon Mac before Live Vision demo setup.
---

Read [the Mac workflow](../../../docs/jetpack-sd-mac.md) for host requirements,
the supported module/revision, QSPI prerequisite, and current validation scope.
Commands below run at this repository's root.

Keep an existing booting JetPack 7.2.1 / L4T 39.2.1 installation unless the user asks
to replace it. Missing CUDA or TensorRT is handled by demo setup. Flashing the SD
must not install Cosmos3-Edge, Live Vision UI, model weights, users, or SSH keys.

1. Check the Mac requirements and `diskutil list` / `diskutil info /dev/diskN`.
   Identify the user's intended whole SD card by reader, capacity, and serial when
   available. A built-in reader can report **Internal: Yes**; its protocol is
   **Secure Digital**. Never infer the target from the highest disk number.
2. Run `./scripts/flash-jetpack-sd-mac.sh --disk /dev/diskN --dry-run` to build/cache
   and validate the image. The first build can take an hour or more. It creates an
   isolated Ubuntu amd64 VM; no Jetson USB connection is needed.
3. If flashing this identified card is already authorized, run the same command
   with `--erase` instead of `--dry-run`. Otherwise explain the selected card and
   obtain authorization before erasing. Run in Terminal and let the user authenticate with `sudo`;
   never collect their password in chat. Keep the card inserted through readback.
   For an agent without an interactive terminal, prepare the exact authorized command
   for the user to run in Terminal. Do not use AppleScript administrator elevation:
   its helper does not inherit the app's disk permission.
4. A successful run writes `flash-receipt.json` under the reported image directory
   and ejects the card. Confirm its status and SHA-256. On any error, inspect the
   reported log and resolve the cause; do not retry a write automatically.
   If macOS denies `/dev/rdiskN` despite administrator authentication, follow the
   workflow's disk-access guidance and wait for the user to grant that permission.
   Do not change privacy settings yourself. Staged files and logs are retained on
   failure; the helper stages outside Documents to avoid privacy-folder failures.
5. Identify the intended new board through USB or its local console before access.
   Do not probe a historical Orin IP, alter another board's SSH host key, or touch
   an existing deployment as a discovery shortcut. Ask the user to boot the SD
   normally with FC REC unjumpered. Read
   [First-boot setup and recovery](../../../docs/first-boot-recovery.md) if discovery,
   the wizard or login does not proceed. The read-only
   `python3 scripts/jetpack/first_boot.py probe --json` command inventories USB/serial
   devices without network discovery. If USB is absent, check power and reconnect
   the data cable once; absence alone does not diagnose firmware incompatibility.
6. Have the user complete visible first-boot setup and verify a fresh login. A login
   prompt does not prove account creation succeeded; do not assume Cancel or Ctrl-C
   safely finishes the wizard. Prefer passwords entered directly in Terminal; if
   credentials are explicitly supplied for a controlled test, make at most one
   attempt. If rejected, stop guessing and follow the read-only offline card audit.
   Recheck reader, capacity and available serial before any repair, preserve a
   recoverable copy, and diagnose before changing accounts or setup state.
7. On the identified board, check `whoami`, `findmnt -n -o SOURCE /`,
   `cat /etc/nv_tegra_release`, and `df -h /`, then establish its network identity
   and SSH access. Do not claim account repair until a fresh on-board login succeeds.
   Writing and readback alone do not prove SD boot, filesystem expansion, GPU
   execution, or Live Vision inference.

Return to [AGENTS.md](../../../AGENTS.md) for demo setup, which installs the
missing compute components and then the application. This skill does not update
QSPI or touch the Jetson's NVMe. For other boards or firmware, use NVIDIA's
documented flashing path rather than changing the pinned board values by guesswork.
