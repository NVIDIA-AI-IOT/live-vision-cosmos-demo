# Optional JetPack SD preparation from a Mac

Keep a working **JetPack 7.2.1 / L4T 39.2.1** installation. This step is only for a
fresh card. After boot, `bootstrap.sh` installs missing CUDA 13.2 / TensorRT 10.16.2
components before the demo build; OS-only media is intentionally not a full compute SDK.

## Requirements and scope

- Apple Silicon MacBook, macOS **15+**, **16 GB RAM**, **60 GiB free** on the build
  cache volume, internet, Apple Command Line Tools (`xcode-select --install`),
  [Homebrew](https://docs.brew.sh/Installation), administrator access, and macOS
  disk-access permission for the terminal/app running the command. QEMU uses 8 GiB
  and four virtual CPUs. No laptop GPU or CUDA is required.
- Writable **64 GB+ microSD**, built-in SD reader or a USB reader that reports removable
  media. The selected card is erased. Tested nominal 64 GB cards report
  62,883,102,720 and 63,281,561,600 bytes.
- **Orin Nano Super 8 GB developer kit P3767-0005**, FAB 300, board revision T.1,
  chip SKU D5, RAMCODE 2. This recipe is pinned to that hardware; it is not a generic Orin flasher.
- Compatible **R39.2.1 QSPI firmware already installed**. The Mac card writer cannot
  update the Jetson's QSPI. For an unprepared board or another module, follow
  [NVIDIA's setup instructions](https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/setup_bsp.html).

## Run on the Mac, then boot the Orin

```bash
brew install python qemu zstd
diskutil list
./scripts/flash-jetpack-sd-mac.sh --disk /dev/diskN --erase
```

Replace `diskN` with the identified **whole SD disk**. `--erase` is explicit
authorization; run in Terminal and enter your Mac password at the `sudo` prompt.
Allow removable-volume access if macOS requests it. Use `--dry-run`
instead to build and validate without writing, or `--build-only` without `--disk`.

The script downloads hash-pinned NVIDIA BSP/rootfs and Ubuntu 22.04 amd64 media,
runs NVIDIA's SD image creator in a disposable QEMU VM, checks GPT/ext4 and the clean
rootfs, then writes the card, verifies the entire image by SHA-256, and ejects it.
The image contains no Cosmos3-Edge, Live Vision UI, model weights, precreated user,
or deployment SSH keys. Nothing is passed through to the Jetson or its NVMe.
This is a project integration of [NVIDIA's image-creation tools](https://docs.nvidia.com/jetson/archives/r39.2.1/DeveloperGuide/SD/FlashingSupport.html#flashing-to-an-sd-card), not an NVIDIA-supported Mac recovery-flash path.

The first build can take an hour or more. Downloads and validated output are cached
in `~/Library/Caches/live-vision-jetpack/7.2.1`; successful builds remove their VM.
Failed builds retain a stopped VM and logs for diagnosis. `--cache-dir` selects another
volume; `--image-dir` reuses a directory containing the generated `.img.zst` and `image.json`.
Only the final writer uses `sudo`, with an expanded image staged outside protected
Documents/Desktop. The Mac's temporary volume also needs 11 GiB free for that image.
If macOS reports `Operation not permitted` for `/dev/rdiskN` after authentication,
check **System Settings → Privacy & Security → Files and Folders** for your terminal's
removable-volume permission (Full Disk Access may be needed). Use normal Terminal
`sudo`: AppleScript's administrator helper does not inherit an app's disk permission.
Administrator authentication and disk access are separate permissions
([Apple](https://support.apple.com/guide/security/controlling-app-access-to-files-secddd1d86a6/web)).
Failed writes retain their staging directory and log; resolve the reported cause before retrying.

After ejection, move the card to the powered-off Orin, select SD in its boot menu if
needed, complete first-boot setup, and enable SSH. Confirm `/` is on `mmcblk0p1`,
L4T reports R39 revision 2.1, and `df -h /` shows the expanded filesystem. Then
return to the [demo Quickstart](source-installation.md#quickstart-automated-setup).
If USB discovery, the setup wizard or account login fails, use
[First-boot setup and recovery](first-boot-recovery.md) before retrying or reflashing.
Identify the new board through its USB/local console; do not use an existing Orin's
address as a discovery shortcut.

### USB-C setup without a monitor

Boot the prepared SD normally with **FC REC unjumpered** and the usual barrel-jack
power supply. USB-C is a data connection, not the board's power input. If an existing
NVMe installation boots instead, select the SD in the UEFI boot menu.

With no display connected, the pinned `nvidia-l4t-oobe` 39.2.1 package selects its
headless setup wizard on USB gadget serial (`ttyGS0`). Connect a USB-C data cable to
the Mac, identify the newly appearing `/dev/cu.usbmodem*` port, open that serial port
at 115200 baud, and press Enter. Create the initial user before attempting SSH. After
setup, USB device mode provides serial login and USB networking; the Orin also needs
internet access for the subsequent dependency/model downloads.
**Prefer wired Ethernet directly to the router for first-time setup.** If Wi-Fi
is required, stay near the router with minimal obstructions and clear line of sight
where practical; a stable signal reduces interruptions and incomplete setup.
Wi-Fi causing SD/filesystem corruption has not been established here.
Follow the [wired setup checklist](first-boot-recovery.md#complete-setup-over-ethernet):
select the displayed wired interface if offered, finish all setup pages, and verify
a fresh login, SD expansion and this board's SSH identity. Account entry alone is
not completion; the pinned completion script may restart the USB connection.

**Recommended hostname: `orin-testbench`.** The hostname names the device; the
login username is separate. Choose another unique hostname if this one is already
in use, using lowercase letters, numbers and hyphens with no spaces. Follow the
[hostname and network checks](first-boot-recovery.md#complete-setup-over-ethernet);
`.local` name resolution is not guaranteed.

If no USB device appears, check power and reconnect the data cable before concluding
that firmware is incompatible. If setup is interrupted or canceled, a subsequent
login prompt is only a prompt: verify that the selected account can actually log in.
The [recovery guide](first-boot-recovery.md) includes bounded checks for both cases.

This route is verified from the pinned package's `nv-oobe.service`, `nv-oobe.sh`,
`nv-oem-config.conf` and `nv-oobe-post.sh`. A physical first-boot trial also reached
the wizard over USB after the data cable was reinserted; account validation remains
a separate checkpoint, described in the recovery guide.
USB gadget serial starts after Linux boots and cannot act as the preboot UEFI/ISO
console. Use a monitor and keyboard or the documented header UART for those menus.
The ISO's minimum firmware requirement to start is not proof that the standalone SD
can boot: complete the ISO's QSPI firmware update before switching to this image.
See NVIDIA's [installation guide](https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/latest/quick_start.html)
and [USB interface documentation](https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/latest/hardware_layout.html).

## Validation

**Validated 2026-09-29 on a fresh nominal 64 GB SD card**, using the supplied script
and [agent skill](../.agents/skills/flash-jetpack-sd-mac/SKILL.md) on an Apple Silicon
MacBook (macOS 26.7, 36 GiB RAM, built-in reader). The `--image-dir` route wrote and
read back all **10,181,672,960 image bytes**, matched SHA-256, and safely ejected the
card. See the [validation record](validation/jetpack-sd-mac-2026-09-29.json).

**Cold build, preflight and physical flash validated 2026-10-05**, using the current wrapper
on an Apple Silicon MacBook (macOS 26.7.1, 36 GiB RAM). A fresh disposable QEMU VM
built a new image from the pinned NVIDIA BSP/rootfs archives. GPT, ext4, clean-rootfs
checks, transfer back to the Mac, and full uncompressed-image SHA-256 validation
passed. The raw image is **10,181,672,960 bytes**; its compressed archive is
**2,871,407,646 bytes**. After the successful dry run, the writer flashed a nominal
64 GB card, read back all image bytes, matched SHA-256
`960b6b55645257441b1fc860179f185b08c63d815390fd73499880693c17d39a`, and ejected
the card. See the [2026-10-05 validation record](validation/jetpack-sd-mac-2026-10-05.json).
The date is Pacific time; the receipt records UTC timestamps on 2026-10-06.

The first cold-build attempt exposed a missing `bzip2` prerequisite in the minimal
Ubuntu builder. Installing the archive prerequisites before extracting the BSP fixed
that failure; the subsequent complete build passed. The earlier 2026-09-29 record
is retained as historical evidence for its separately flashed image.

A subsequent board trial reached USB first-time setup and a login prompt. The reported
account did not pass the controlled login check. A read-only card audit, confirmed
after journal replay into a disposable copy-on-write overlay, found no saved regular
user, first-time setup still enabled, and the approximately 8 GiB root partition
not yet expanded. OEM setup had logged terminal-discovery input/output errors; their
underlying cause remains unknown. That inspection left the physical card unchanged.
A subsequent full power-off/normal-boot retry with SD and Ethernet again required a
USB cable reconnect and reached only a login prompt; the setup wizard did not appear
and another user login failed. Normal reboot alone was insufficient. A second
read-only audit matched the same card and confirmed an increased mount counter;
after journal replay in a disposable copy, it again found no regular account,
the setup target enabled, and OEM programs present. Saved terminal-error logs were
unchanged, so the underlying cause remains unknown. A complete checksummed
root-partition backup was preserved. A temporary headless entry override passed
checks on a repaired disposable copy, including a clean full filesystem check and
write preflight. The physical writer then exited with a generic `RuntimeError`
after beginning full pre-write verification. The visible log proves neither zero
writes nor a specific failure cause; whether any bytes changed remains unknown
pending inspection of the privileged report's phase and counters, which requires
renewed Mac administrator authentication. The card's identity was revalidated,
no writer or open card handles were visible, and ejection succeeded with the
device disappearing. The checksum-verified backup remains in private durable
storage. Retain it and inspect the report before any retry; never retry
automatically. The repair was not successful. This
[opt-in recovery](first-boot-recovery.md#temporary-headless-entry-recovery-candidate)
is separate from the normal installer.

A later connection matched the same new board's USB identity. With one serial
session open, the vendor's System Configuration prompt appeared; one Enter opened
the actual wizard at the UTC-clock question. It subsequently reached hostname
entry and “Installing system” at 0%. USB then disconnected and the same board
re-enumerated. The
prompt used the vendor wording, not the custom helper's wording. Whether the
patch was applied remains unknown; the failed repair cannot be credited with this
result. The console then reached the chosen user's shell. Checks confirmed
`graphical.target`, ext4 root on `/dev/mmcblk0p1`, L4T 39.2.1, wired Ethernet with
a default route, and active SSH; its host-key fingerprint was obtained through the
local console. This first shell established account access; fresh credential
verification was performed after the reboot described below.

Expansion initially blocked installation: the approximately 58.9 GiB card retained an
approximately 8 GiB root partition. Its roughly 7.8 GiB filesystem reported 100%
usage, with about 31 MiB free. Completing the wizard did not establish expansion.

**Guarded manual expansion then succeeded on the new Orin.** After verified GPT
backups and layout/dry-run checks, `growpart` increased only the physically last
APP partition's size. Its start and partition metadata were preserved, the kernel
reported the new size, and online `resize2fs` preserved the filesystem UUID. GPT
verification found no problems. The filesystem now reports about **57 GiB total,
47 GiB available and 14% used**, passing the 25 GiB free-space requirement. See the
[layout-guarded workflow](first-boot-recovery.md#if-app-did-not-expand) and
[NVIDIA's resizing guidance](https://docs.nvidia.com/jetson/archives/r39.2.1/DeveloperGuide/SD/FlashingSupport.html#resizing-the-root-partition-to-fill-the-available-sd-card-space).
A reboot was initiated for the pending NVIDIA bootloader-capsule/system update.
The same board returned on USB with its chosen hostname at the login prompt.
Fresh login then succeeded. The changed boot ID, same SD root and SSH identity,
47 GiB free, working wired route and cleared reboot-required flag were verified.
Compute installation, native runtime compilation, INT4 quantization and both
ONNX exports subsequently completed on the new board. A swap-status parsing error
stopped engine construction; its correction passed a hardware activation/cleanup
check. The resumed installer then **built both engines, enabled the shim and UI,
passed readiness and exited successfully**, with temporary build swap off. The
filesystem reported approximately 57 GiB total, 38 GiB used and 17 GiB available
after installation. The guarded 12 GiB engine-build resume path worked in this
trial; it remains a provisional allowance rather than a peak-space guarantee.
See the [installer recovery guidance](first-boot-recovery.md#if-the-demo-installer-stops).
The subsequent exact-response text test passed, but the first image-quality check
failed. Inspection proved an HWC/CHW patch-layout mismatch in the pinned ONNX
exporter. After correcting it and rebuilding only vision, **the same three images
produced scene-grounded captions through the API and browser UI**; the language
engine's hash was unchanged. This establishes bounded signs of life, with minor
caption errors recorded in the recovery guide, not general accuracy or speed.
Wi-Fi has not been tested. Test later network changes with an authenticated USB console, using
[the Wi-Fi switching guide](network-switching.md). Host image validation and
flash verification do not establish those results. See
[the first-boot observations](first-boot-recovery.md#what-has-been-observed).

The NVIDIA archive hashes in `scripts/jetpack/release.json` pin the official-download
bytes the validated image was built from; they are not claimed as publisher-signed checksums.
Ubuntu's pinned hash matches its published SHA256SUMS. Upstream licenses and
acknowledgements are in [NOTICE.md](../NOTICE.md#mac-jetpack-sd-preparation).
