#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Inside the disposable Ubuntu amd64 VM only. No host disks or USB passed through.
set -euo pipefail
[[ $EUID -eq 0 && $(uname -m) == x86_64 ]] || exit 2
work=/home/flash/jetpack-sd
[[ ! -e "$work" ]] || { echo "Build directory already exists; use a fresh build VM." >&2; exit 2; }
mkdir -p "$work" /mnt/downloads /mnt/helpers
mount -t 9p -o trans=virtio,version=9p2000.L,ro downloads /mnt/downloads
mount -t 9p -o trans=virtio,version=9p2000.L,ro helpers /mnt/helpers
cd "$work"
# The minimal cloud image does not include bzip2. Install archive and image-check
# tools before extracting the BSP (whose own prerequisites script is inside it).
export DEBIAN_FRONTEND=noninteractive
apt-get update -o Acquire::Retries=3
apt-get install -y --no-install-recommends bzip2 zstd gdisk
tar -xjf /mnt/downloads/Jetson_Linux_R39.2.1_aarch64.tbz2
tar --numeric-owner -xjpf /mnt/downloads/Tegra_Linux_Sample-Root-Filesystem_R39.2.1_aarch64.tbz2 -C Linux_for_Tegra/rootfs
cd Linux_for_Tegra
./tools/l4t_flash_prerequisites.sh
./apply_binaries.sh
# Adapt only the download-local copy; retain NVIDIA's header and license in full.
python3 - <<'PY'
from pathlib import Path
p = Path('tools/jetson-disk-image-creator.sh')
source = p.read_text()
old = 'sudo dd if="${target_file}" of="${loop_dev}p${part_num}"'
if source.count(old) != 1:
    raise RuntimeError('NVIDIA image creator changed; review before adapting it')
target = p.with_name('jetson-disk-image-creator-local.sh')
target.write_text(source.replace(old, old + ' bs=4M conv=fsync status=progress'))
target.chmod(0o755)
PY
image="$work/jetpack-7.2.1-orin-nano-super-clean.img"
env FUSELEVEL=fuselevel_production BOARDREV=T.1 CHIP_SKU=00:00:00:D5 RAMCODE_ID=2 \
  ./tools/jetson-disk-image-creator-local.sh -o "$image" -b jetson-orin-nano-devkit-super -r 300 -d SD
sgdisk --verify "$image"
loop=$(losetup --find --show --read-only --partscan "$image")
verify="$work/verify"
mkdir "$verify"
cleanup() {
  if mountpoint -q "$verify"; then umount "$verify"; fi
  losetup -d "$loop"
}
trap cleanup EXIT
udevadm settle
[[ $(blkid -s PARTLABEL -o value "${loop}p1") == APP ]]
e2fsck -f -n "${loop}p1"
mount -t ext4 -o ro,noload "${loop}p1" "$verify"
python3 /mnt/helpers/validate_rootfs.py "$verify" > "$work/rootfs-validation.json"
cleanup
trap - EXIT
zstd -T2 -3 "$image" -o "$image.zst"
python3 - "$image" <<'PY'
import hashlib, json, sys
from pathlib import Path
image = Path(sys.argv[1])
def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()
compressed = Path(str(image) + '.zst')
manifest = json.loads((image.parent / 'rootfs-validation.json').read_text())
manifest.update(path=str(compressed), size_bytes=image.stat().st_size, sha256=digest(image),
                compressed_size_bytes=compressed.stat().st_size, compressed_sha256=digest(compressed),
                gpt_verification_passed=True, ext4_verification_passed=True,
                source_release=json.loads(Path('/mnt/helpers/release.json').read_text()))
(image.parent / 'image.json').write_text(json.dumps(manifest, indent=2) + '\n')
PY
chown flash:flash "$work/image.json" "$image.zst"
echo CLEAN_SD_IMAGE_VALIDATED
