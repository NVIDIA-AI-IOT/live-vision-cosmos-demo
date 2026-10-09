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
  media. The selected card is erased.
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
return to the [demo Quickstart](source-installation.md#advanced-source-installation).
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

USB gadget serial starts after Linux boots and cannot act as the preboot UEFI/ISO
console. Use a monitor and keyboard or the documented header UART for those menus.
The ISO's minimum firmware requirement to start is not proof that the standalone SD
can boot: complete the ISO's QSPI firmware update before switching to this image.
See NVIDIA's [installation guide](https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/latest/quick_start.html)
and [USB interface documentation](https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/latest/hardware_layout.html).

The hashes in `scripts/jetpack/release.json` pin the downloaded inputs. Upstream
licenses and acknowledgements are in [NOTICE.md](../NOTICE.md#mac-jetpack-sd-preparation).
