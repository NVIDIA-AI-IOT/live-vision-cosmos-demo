# First-boot setup and recovery

After account setup and confirmed image inference, use
[Optional Reachy Mini setup](reachy-setup.md) to discover a robot, enter its address,
or skip it. Reachy and Piper preparation is separate from first boot; a missing
robot is not a reason to reflash or rebuild an otherwise working demo. The same
robot setup controls remain available in Live Vision after installation.

Use this guide after preparing the [JetPack SD card](jetpack-sd-mac.md), before
running the demo installer. A verified card write, a USB console, a login prompt,
and a working user account are separate checkpoints.

## Start with the right board

1. Power off the intended Orin before inserting its SD card. Leave **FC REC
   unjumpered** for normal boot.
2. Connect its barrel-jack power supply, Ethernet, and a USB-C **data** cable to
   the Mac. USB-C is not the board's power input.
3. Observe the USB device and serial port that appear when this board is connected.
   Do not reuse an old Orin's IP address, hostname, credentials, or SSH host key.
4. Open the identified USB serial console at 115200 baud. With no display connected,
   the pinned image can present the first-time setup wizard there after Linux boots.
   Press Enter once when the console asks you to start setup.

The Mac can collect a read-only USB/serial inventory from this repository:

```bash
python3 scripts/jetpack/first_boot.py probe --json
```

This probe does not log into a Jetson or scan the network. Compare observations
before and after reconnecting the intended board. USB discovery alone does not
identify the boot disk or establish which account exists.

## Complete setup over Ethernet

**Prefer wired Ethernet directly to the router for first-time setup.** If Wi-Fi
is required, stay near the router, minimize obstructions and keep a clear line of
sight where practical. Use a stable signal to reduce network interruptions and
the risk of incomplete setup. Wi-Fi causing SD or filesystem corruption has not
been established in this trial.

1. Keep Ethernet and the identified USB data connection attached throughout setup.
2. If the wizard offers network interfaces, select the displayed **wired Ethernet**
   interface. Do not guess an `eth0` name or select the USB gadget interface as the
   Internet connection. Record the displayed choice and whether configuration succeeds.
3. **Recommended hostname: `orin-testbench`.** This names the new device; the login
   username is a separate field. If that hostname is already in use, choose another
   unique name using lowercase letters, numbers and hyphens, with no spaces.
   Use the verified IP address for SSH; `.local` resolution depends on the network.
4. Finish every setup page and its final application step. Entering account details
   is not proof that the account was saved; do not cancel networking on that assumption.
5. Wait for completion. If USB serial disappears, probe again and reopen the
   identified board's console; do not switch to a remembered IP address.
6. Verify a **fresh login**, then run the [pre-install checks](#before-installing-live-vision)
   below before accepting SSH or starting bootstrap.

The pinned source describes the following behavior; trial observations are recorded
[below](#what-has-been-observed):
`ubi-network.py` places networking after the account page, while `debconf_ui.py`
runs the installation components after the page sequence finishes. The pinned
`nv-oobe-post.sh` restarts USB gadget mode and serial login after the default target
leaves `nv-oobe.target`, so a USB reconnection may be needed at completion. The
network wrapper delegates to `netcfg`; automatic Ethernet selection is not established.

## If setup does not proceed

| What you see | What to do next | What it does not prove |
| --- | --- | --- |
| No USB device or serial port | Confirm barrel-jack power, then reconnect the data cable once and repeat the USB probe. Check for a data-capable cable or another direct Mac port. | No USB enumeration alone does not establish incompatible firmware or a failed SD image. |
| A USB device but no serial console | Record the observed device identity and boot state. Wait for Linux to start and probe again. Check whether another terminal already owns the newly appearing serial port. | A USB device is not necessarily a Linux login console. Do not change boot mode based on a guessed device type. |
| First-time setup appears | Complete the wizard in the terminal. Record the chosen username; type the password directly into its password fields. | The account is not verified until a fresh login succeeds. |
| Wireless setup appears while Ethernet is connected | Follow the wizard's displayed options. If leaving that screen appears stuck, inspect the console before making another change. | Canceling a network screen is not evidence that account creation finished or failed. |
| A screen looks frozen or arrow-key escape sequences are printed | Stop sending navigation keys. Capture the current prompt, check whether the serial session is still connected, and reopen that same console if necessary. | A stale terminal display is not proof that the board hung. Reconnecting the console does not complete the wizard. |
| `login:` appears after a setup interruption | Try the exact account you created once. A successful shell prompt is the next checkpoint. | A login prompt does not prove setup saved a usable account or password. |
| `Login incorrect` for the account you created | Stop retrying. Check the exact selected username and whether account creation was completed. If that remains uncertain, use the offline audit below. | Do not infer a default username/password, invent an account, or conclude the password was changed. |
| A normal reboot still gives only `login:` and no verified account or setup wizard | Stop repeating passwords and reboots. Return the card for a read-only audit of the latest startup units and boot evidence; preserve an image before any targeted wizard repair. | An earlier enabled setup target does not prove the wizard started successfully on this boot. |
| An old Orin answers at a familiar address | Stop. Return to the intended board's USB identity and obtain its address from its verified local console. | A responding service or familiar hostname is not permission to access another board. |
| The console works but SSH does not | First verify login, the active root filesystem, and the new board's network address from its local shell. Then inspect SSH on that board. | A fresh OS may need account or network setup; this alone does not call for reflashing. |
| Setup reaches a shell, but the SD root is still approximately 8 GiB or full | Stop before installing the demo. Inspect the partition layout and filesystem size, preserve a backup, and resolve expansion on this verified board. | Completing the wizard does not establish that the root partition or filesystem expanded. |
| No progress after these bounded checks | Preserve observations and identify the remaining uncertainty. Inspect the card offline or use NVIDIA's documented preboot console/firmware path as appropriate. | Do not enter recovery mode, reflash, or alter QSPI solely because USB was initially absent. |

**Do not assume Cancel or Ctrl-C safely completes first-time setup.** A visible
login prompt after either action still requires account validation. Do not issue
blind keystrokes, repeatedly submit credentials, or reset passwords on a reachable
board whose identity has not been established.

## Audit the SD card before repairing it

Use this route when account setup cannot be validated and no authorized local
administrator session is available.

1. Shut down or disconnect power from the **intended new board**, then return its
   SD card to the Mac. Leave any existing working Orin untouched.
2. Re-identify the whole card using its reader, capacity, and serial when available.
   Compare against the flash receipt or locally saved identity. A disk number can
   change after reinsertion. If the identity differs or is ambiguous, stop before
   any write.
3. Inspect a read-only copy or a read-only mount in an isolated Linux repair
   environment. Avoid filesystem journal replay during diagnosis. Keep account
   databases and logs local: they can contain credentials, hashes, device details,
   and personal information. If the filesystem has a pending journal, a
   journal-free inspection is provisional: confirm findings after replaying the
   journal into a disposable copy or copy-on-write overlay whose backing card
   remains read-only. Never replay it onto the physical card just to inspect it.
4. In the Linux repair VM, run the audit against that read-only mounted root, using
   the username chosen during setup. `sudo` permits reading the protected account
   database; it does not make the mount writable:

   ```bash
   sudo python3 scripts/jetpack/first_boot.py audit-root \
     --root /path/to/read-only-mounted-root --user chosen-user --json
   ```

5. Review whether the account exists, whether its authentication record is usable,
   and what setup state was saved. Account presence or an unlocked password record
   does **not** verify that the entered password matches. Missing or unreadable
   evidence must remain unknown. In particular, a permission-denied shadow read
   does not establish that the account or password is missing.
6. Choose a repair only after the evidence identifies the failure. Before modifying
   the card, preserve a recoverable copy and recheck its identity. Limit a repair
   to the diagnosed account/setup problem; do not overwrite the installation with
   the original clean image just to retry login. If no account was saved and the
   first-time setup target remains enabled, try resuming that setup on one normal
   boot before considering an offline account change. If that retry still presents
   only a login prompt, inspect the latest startup-unit state and boot evidence
   offline. Do not assume the earlier audit still describes the current state or
   keep rebooting; preserve an image before a targeted wizard repair.

The audit is diagnostic. It does not reset a password, create an account, mount or
unmount a disk, update firmware, or make the root filesystem writable. Password
changes, if a diagnosis calls for one, belong in an interactive local prompt rather
than chat, shell history, a source file, or a committed report. Do not post raw
`/etc/shadow`, password hashes, private keys, or complete device logs.

After an authorized repair, check the affected files/filesystem, finish any writes,
unmount, and eject safely. Tell the user the card is safe to remove only after
ejection succeeds. Verify a **fresh login on the board** before claiming the account
is repaired.

## Temporary headless-entry recovery candidate

**Physical repair attempt failed; whether any bytes were written remains unknown.
The candidate's hardware effect is unverified.** A later vendor-wizard entry was
observed, but cannot be attributed to this patch. This candidate addresses an enabled setup
target, intact OEM programs and no saved regular user. It is not a general boot
repair or a replacement for NVIDIA's setup wizard. It is an **opt-in recovery**;
the normal image builder and demo installer do not install these overrides.

The entry helper requires root, exactly `nv-oobe.target` as the default, readable
valid local account metadata with no regular user, and the expected executable OEM
programs. It stops only the `ttyGS0` serial getty, waits up to 20 minutes for an
explicit **Enter on an empty line**, and handles USB disconnects while waiting.
It rechecks the prerequisites before handing the console to the vendor wizard.
The vendor child service conflicts with and starts after the `ttyGS0` getty;
the outer service retains NVIDIA's existing `ExecStopPost` behavior. The helper
does not reset wizard answers, precreate an account, set a password or log terminal
input. The user must still complete every setup page and verify a fresh login.

The candidate adds exactly these three files from the linked project sources:

- `/usr/local/lib/live-vision-first-boot/headless_retry.py` — [entry helper](../scripts/jetpack/headless_retry.py).
- `/etc/systemd/system/nv-oobe.service.d/50-live-vision-headless-retry.conf` — [outer service override](../systemd/recovery/nv-oobe.service.d-50-live-vision-headless-retry.conf).
- `/etc/systemd/system/nv-oem-config-debconf@ttyGS0.service.d/50-live-vision-console-owner.conf` — [console ownership override](../systemd/recovery/nv-oem-config-debconf@ttyGS0.service.d-50-live-vision-console-owner.conf).

Seventeen [mocked/PTY helper tests](../scripts/jetpack/test_headless_retry.py),
sixteen patcher fixture tests, and an isolated
Linux VM service-handoff test passed. Full target-unit verification and a physical
helper-driven wizard handoff remain unverified. On the disposable copy, filesystem inspection found
five deleted inodes with zero links, zero length and no extents. After repairing
their orphan-list errors on that copy, a full `e2fsck -f -n` check exited cleanly.
The three added files matched their expected hashes, and no account was precreated.

The original backup and repaired image were verified. A write dry run identified
13 changed 4 MiB chunks: 52 MiB to rewrite, containing 113,655 changed bytes.
The physical writer subsequently exited with a generic `RuntimeError` after
beginning full pre-write verification. The visible log does not establish whether
any bytes were written or identify the failure's cause. Its privileged detailed
report requires renewed Mac administrator authentication to inspect. Copy and
preflight checks remain passed, but the card repair did not succeed.

The whole-card identity was revalidated, no writer or open card handles were
visible, and `diskutil eject` succeeded; the device then disappeared. The
checksum-verified backup remains in a private durable cache. Retain that backup
and inspect the privileged report's phase and write counters before deciding what
to do next. **Never automatically retry this write** or treat ejection as proof of
a successful repair. The subsequent wizard-entry observation below does not verify
this patch, account completion or the active root filesystem.

After successful setup and the [pre-install checks](#before-installing-live-vision),
optional cleanup removes **only those three added files**, after checking they are
the recovery files expected, followed by `sudo systemctl daemon-reload`. Do not
remove an entire drop-in directory, alter vendor files, or remove the helper while
the wizard is active. Retain the pre-repair backup until the board passes a fresh
login and normal boot.

## Before installing Live Vision

From the new board's authenticated shell, collect:

```bash
whoami
systemctl get-default
findmnt -n -o SOURCE /
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS
cat /etc/nv_tegra_release
df -h /
ip -brief address
ip route show default
systemctl is-active ssh
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Confirm the intended user, a default target other than `nv-oobe.target`, SD root on
`mmcblk0p1`, L4T R39 revision 2.1, and an expanded partition and filesystem. Check
that the address and default route belong to this board's wired connection.
Record its SSH host-key fingerprint through the identified console and compare it
when connecting from the Mac. If SSH or its key is unavailable, resolve that on this
board first; do not bypass identity checks or use another Orin's keys.
Only then return to [the demo Quickstart](source-installation.md#quickstart-automated-setup).
Keep network and device identifiers in local deployment notes, not public source.

**Do not start the demo installer while the SD root remains unexpanded or full.**
Verify both partition and filesystem size; the fresh installation needs 25 GiB
free. A working account, Ethernet and SSH do not substitute for this check.

### If APP did not expand

Use NVIDIA's [SD root-partition resizing guidance](https://docs.nvidia.com/jetson/archives/r39.2.1/DeveloperGuide/SD/FlashingSupport.html#resizing-the-root-partition-to-fill-the-available-sd-card-space)
with the **observed layout of the identified board**. Do not copy sector offsets
from another card or assume partition number implies physical order.

1. Confirm the active ext4 root is the intended SD's APP partition. Save a binary
   GPT backup and a readable partition-table dump; keep a verified copy off-device
   alongside the existing filesystem backup.
2. Inspect every partition's start and size. Continue only if APP is physically
   last and the unused space follows it contiguously. Otherwise stop and derive a
   different layout-specific procedure; do not move boot partitions speculatively.
3. Review a `growpart` dry run for that disk and APP partition. Require an unchanged
   APP start and only its size increasing; preserve every partition's identifier,
   type, name and UUID, and all other entries.
4. Apply that reviewed `growpart` change and compare the resulting partition table
   against the saved one. **Before `resize2fs`, verify that the kernel reports APP's
   new size**, using `lsblk` and `blockdev --getsize64` on the identified partition.
   Stop if the kernel still reports the old size or another entry changed.
5. Run `resize2fs` on the confirmed ext4 APP partition. Verify its filesystem UUID
   is unchanged, check the GPT with `sgdisk --verify`, and recheck `findmnt`, `lsblk`
   and `df -h /`. Installation requires at least 25 GiB free. After any required
   reboot, repeat the root/size checks and verify a fresh login before proceeding.

This guarded manual route succeeded on the trial board described below. It does
not establish that every image or partition layout supports the same operation.

## If the demo installer stops

Read the reported stage and error before rerunning setup. Resume only on the same
verified board; never redirect bootstrap to another existing Orin to work around
a failure. Completed current-SDK stages can be reused, but stage markers are not
substitutes for their artifacts.

| Failure or state | Safe next step |
| --- | --- |
| Insufficient storage on a fresh, partial, or stale-SDK installation | Setup requires **25 GiB free**. Check the root device, filesystem expansion and available space. Free identified unrelated files or use larger storage; do not manufacture stage markers to obtain the lower threshold. |
| Both exports are complete and only engine construction remains | A **12 GiB** resume allowance applies only after every preceding stage is complete, native build tools are present, both ONNX exports and their external tensor ranges validate, and the exported Jinja matches the provider template. This budgets 4 GiB for temporary swap plus engine output and margin; it is not a measured peak-space guarantee. Other partial installations retain the 25 GiB requirement. |
| Maintenance of an already completed installation | The **8 GiB** threshold applies only when the heavy stages have current-SDK markers and the checked native runtime, ONNX exports, engines and Jinja template are present and nonempty. |
| Coherent text but incorrect image captions with this pinned SDK | Check the Cosmos HWC/CHW patch-layout contract. The branch's guarded applier fixes the known ONNX exporter mismatch automatically during visual export. The same-SDK repair path validates existing artifacts, allows 12 GiB remaining space, retains the language engine, and rebuilds vision only. Do not switch quantizers or tune image budgets to hide a correctness defect. |
| The visual-layout applier rejects the SDK revision or source digest | Stop and inspect the exact source path and diff. The similarly named experimental checkpoint-direct builder is a different module. Do not relax the digest check or replace unexpected local edits. |
| A vision repair fails after pausing inference | Fix the reported error and rerun the normal installer. Repair invalidates service-start and readiness markers as well as the vision build receipt, so a new process must restart and check the services. Do not serve a partially rebuilt vision engine. |
| Build-swap creation, activation or verification fails | Engine construction requires the equivalent of a **4 GiB active swapfile** (allowing the swap header page). Inspect `df -h`, `free -h` and `swapon --show --bytes`; fix the reported cause before resuming. The installer stops before the engine build if it cannot verify this swap. |
| Swap is active but the installer reports that its size could not be verified | Use `swapon --show=NAME,SIZE --bytes --noheadings --raw`. On util-linux 2.39.3, `--output` is accepted as an abbreviation of `--options`, leaving extra columns in the report. An older installer parsed `file` as the size and rejected valid swap. Update to the corrected installer; do not bypass verification or add swap repeatedly. |
| An old, unfamiliar or replaced `data/build-swap.img` exists | Inspect its identity and active-swap status. Existing files are not reformatted or deleted by this run. Already-active sufficient swap is preserved; if the run activates an existing inactive file, cleanup may deactivate it but preserves the file. A replaced path is left untouched. |
| A build fails normally, or receives an interrupt/termination signal | Cleanup removes only the unchanged swapfile created by that invocation, after verifying it is inactive. If inference was paused, it remains stopped because an engine may have been partially rewritten. Fix the error, rerun setup to finish the build, then verify real model output before serving. |
| `swapoff` or cleanup fails | Preserve the file and inspect available RAM and active swap. Never delete an active swapfile. Setup does not proceed to service startup after its post-build cleanup fails. |
| Power is lost or the process is killed with `SIGKILL` | Cleanup cannot run. Inspect the remaining swapfile, filesystem and partial build before resuming; do not blindly remove a file just because its name looks temporary. |

These guards manage temporary **build** resources. They do not authorize removing
unknown files, stopping unrelated workloads, changing another board, or claiming
that a completed build produces correct captions. Follow the
[application validation checks](../AGENTS.md#verifying-success) after recovery.

## Agent decision tree

```text
Identify the intended board and preserve any existing deployment.
  No USB evidence -> power/data-cable check -> reconnect once -> probe again.
    Still absent -> report evidence; diagnose boot/firmware without guessing.
  USB serial present -> identify/open that console -> inspect the actual prompt.
    Setup wizard -> user completes visible steps; do not send blind cancellation.
    Login prompt -> ask the selected username; prefer user-entered credentials.
      Explicitly supplied credentials -> at most one controlled login attempt.
      Rejected -> stop guessing; preserve evidence -> offline read-only card audit.
      Normal setup retry still shows only login -> inspect latest startup/boot evidence.
      Accepted -> verify user, SD root, release, capacity and board network identity.
        All verified -> bootstrap this board only.
  Repair needed -> establish cause, target identity and recoverable backup first.
    Repair complete -> eject -> require fresh on-board login before declaring success.
  Installer failed -> record stage/error -> inspect storage, swap and partial artifacts.
    Preserve unknown resources; keep paused inference stopped until build completes.
    Resume on the same verified board -> validate actual model output before serving.
```

Do not probe historical IP addresses to find the new board, remove an existing
board's SSH key to suppress a mismatch, or use the existing demo as a repair target.
Do not report an attempted action as successful without its resulting checkpoint.

## What has been observed

During this branch's first-boot trial, USB enumeration was initially absent. After
the data cable was reinserted, a Linux USB console and the first-time setup wizard
appeared. Later, leaving wireless setup was followed by a login prompt; a controlled
login attempt with the reported account was rejected.

At the first offline inspection, the returned SD was identified and examined
read-only. It contained **no saved regular user account**, and `default.target`
still pointed to `nv-oobe.target`:
first-time setup remained enabled. The card had L4T 39.2.1 and an approximately
8 GiB root partition that had not expanded. Because its filesystem had a pending
journal, these findings were confirmed after journal replay in a disposable
copy-on-write overlay backed by the read-only card. The physical SD was unchanged.

OEM setup logs recorded four input/output errors while discovering the terminal
(`os.ttyname(0)`). This establishes a setup failure and explains why a login prompt
was not proof of a saved account. **The cause of the terminal errors remains
unknown**; the evidence does not establish a cable fault, a cancellation bug,
incompatible firmware, or a changed password.

The subsequent retry used a full barrel-power shutdown and normal boot with SD
and Ethernet attached. USB again appeared only after the data cable was reconnected.
The identified board presented an Ubuntu 24.04.4 `ttyGS0` login prompt, without a
setup wizard, and another user login was rejected. **Normal reboot alone did not
restore setup.** This does not identify a hardware fault or establish what changed
on disk during that boot.

The second card audit matched the same physical SD. Its filesystem mount counter
had increased from two to three, confirming another mount since the earlier audit.
The user also confirmed that this boot used the SD alone, with no NVMe or USB
installer attached. A complete 8 GiB root-partition backup was preserved with a
SHA-256 checksum before any repair; the physical card remained read-only.

After journal replay in a disposable copy, the second audit again found no regular
account, `nv-oobe.target` as the default, and the OEM setup programs present. The
saved OEM logs still contained the same four terminal-discovery errors; they did
not establish a new cause for the latest boot's behavior. The
[temporary headless entry override](#temporary-headless-entry-recovery-candidate)
passed its copy checks and write preflight for this diagnosed state, but the
subsequent physical writer failed. Whether it changed any card bytes is unknown
until its privileged phase/counters are inspected; the visible log alone cannot
resolve that. The card was safely ejected after identity and handle checks, and
the verified backup is retained. Those actions did not establish a successful
repair, fresh login, root-device identity or filesystem expansion; do not retry
the write automatically.

On a subsequent connection, the same new board's USB identity was matched and a
single serial session was opened. The console displayed the vendor prompt
“Press ENTER to start System Configuration...”. One Enter opened the actual wizard
at “Is the system clock set to UTC?”. This is a **verified wizard-entry checkpoint**,
not verified setup completion. The wizard subsequently reached hostname entry
and then displayed “Installing system” at 0%. The USB session ended and the same
board re-enumerated. That reconnect alone did not establish installation completion.
The prompt did not contain the custom helper's
“on an empty line” wording; patch application remains unknown, and this observation
does not establish that the failed Mac repair caused recovery.

The console subsequently reached a shell under the chosen user and hostname.
Checks confirmed `graphical.target`, an ext4 root on `/dev/mmcblk0p1`, L4T 39.2.1,
wired Ethernet with a default route, and active SSH. Its SSH host-key fingerprint
was read through the identified local console. **Account/shell access and wired
networking were working at this checkpoint.** The initial post-setup shell alone
did not establish fresh credential verification; that was checked after reboot below.

Filesystem expansion initially blocked installation. On the approximately 58.9 GiB SD card,
the root partition was still about 8 GiB; its roughly 7.8 GiB filesystem had 7.4 GiB
used and only about 31 MiB available, reporting 100% usage. The completed system
wizard did not establish expansion.

**Expansion then succeeded on the actual new Orin.** GPT backups were saved and an
off-device copy verified. The observed layout and dry run established that APP was
physically last. The guarded `growpart` operation changed only APP's size while
preserving its start and all other partition metadata; the kernel reported the new
size before online `resize2fs` ran. The filesystem UUID was unchanged. GPT
verification reported no structural problems and also noted partition-end alignment
cautions. The resulting filesystem reported about **57 GiB total, 7.4 GiB used
and 47 GiB available (14% used)**. The 25 GiB fresh-install free-space gate now passes.

A reboot was initiated for the pending NVIDIA bootloader-capsule/system update.
The same board returned on USB and displayed the chosen hostname at its login
prompt. A fresh login with the chosen account then succeeded. A changed boot ID,
the same SD root and SSH key, 47 GiB available, wired routing, and no remaining
reboot-required flag were verified. No failed systemd units were listed. These
checks establish successful setup and storage persistence; they do not establish
model inference. The pinned demo installer then ran over Ethernet, with its log
retained on the new board. No cause is attributed to the failed Mac repair.

**Build checkpoint, 2026-10-06:** CUDA/TensorRT installation, SDK 0.11 native
compilation and runtime import, checkpoint download, all-linear INT4 quantization,
both ONNX exports, and provider-Jinja validation completed. Header/graph inspection
confirmed 169 packed checkpoint linears and 169 V1 INT4 operators. This is structural
quantization evidence, not a caption-quality result.

Engine construction then stopped at swap verification. The 4 GiB file was actually
active, reporting 4,294,963,200 usable bytes after its 4 KiB header. The original
column-selection command returned all columns, so the verifier read `file` instead
of a byte count. Cleanup successfully deactivated and removed the temporary file.
After the command correction, a separate hardware check passed activation, size
verification and cleanup. Completed exports consumed space that the original
25 GiB restart check still reserved for work already finished; the installer now
has a guarded **12 GiB** allowance for resuming directly at engine construction.
It validates the preceding stages, native tools, both exported graphs and external
tensor-file ranges, and the provider Jinja before selecting that allowance.

The installed PyTorch 2.13.0 CUDA build warns that this GPU is unsupported, and
ModelOpt warns about the installed Transformers version. These warnings did not
prevent the two CPU exports from completing. This route uses NumPy quantization,
CPU ONNX export, and native CUDA/TensorRT engine construction and serving; it does
not establish that PyTorch CUDA inference works on this board.

**Installer completion checkpoint, 2026-10-06:** the resumed run built both the
native LLM engine and visual engine, enabled the shim and UI, and passed readiness.
The supervised installer reported `ActiveState=active`, `SubState=exited` and
`ExecMainStatus=0`; its temporary build swap was off. The filesystem reported
about **57 GiB total, 38 GiB used and 17 GiB available**. An idle system-memory
snapshot reported **4,073 MiB used of 7,546 MiB, with 3,472 MiB available**. These
are post-install snapshots, not peak build-space or peak RAM measurements, and
system memory is not a dedicated GPU allocation. The successful resume validates
this trial's corrected swap check and remaining-work path; it does not establish
that every workload fits the provisional 12 GiB allowance.

**Initial image-quality gate failed.** A bounded text test
returned the requested greeting and count coherently, showing that text generation
works. Image requests also returned captions and streamed native timing/usage
fields, but several captions invented visible details. With the one-sentence scene-description prompt,
a giant-panda image was described as a panda in a basket with a red lid, and a
woman-and-dog beach image as a dog wearing a sweater and holding a camera; those
details were absent. An earlier giant-panda request also invented a building and
awning. A red-panda request recognized the animal but did not establish reliable
accuracy across scenes.

Inspection identified an SDK 0.11 patch-layout mismatch: the Cosmos runtime supplies
channel-last (HWC) pixels, but its exporter still permutes the checkpoint's patch
weights for channel-first (CHW) pixels. A comparison of the deployed ONNX initializer
with the raw checkpoint confirmed that obsolete permutation exactly. This is an
input/weight ordering defect; the checks do not attribute it to INT4 quantization.
The source correction passed a digest check and an idempotence check; the installer
now applies it on fresh builds and supports a vision-only retry of this completed
SDK installation. The old vision engine was preserved privately, and the language
engine's SHA-256 was recorded before repair. Forty-four focused offline tests cover
the applier, installer guards and migration behavior, including service recovery
after a failed repair.

**Corrected-vision checkpoint, 2026-10-06:** the rebuilt ONNX patch initializer
matches the raw checkpoint's HWC weights exactly (maximum absolute error zero)
and no longer matches the obsolete CHW permutation. The normal installer reused
the finished language build, rebuilt only vision, restarted services and passed
readiness. The language-engine SHA-256 is unchanged; the vision-engine hash changed.
Temporary build swap is off. Post-repair snapshots show approximately 39 GiB used
and 16 GiB available on the SD, including the preserved old vision engine, and
4,140 MiB system memory used with 3,405 MiB available. These are snapshots, not peaks.

The exact text test again returned `Hello 1 2 3 4 5`. Repeating the same three
known-image requests with unchanged prompts and generation settings restored
scene-grounded captions. Browser image upload and streamed captions also passed:

| Known image | Before correction | After correction in the UI |
| --- | --- | --- |
| Giant panda among bamboo | Invented a car and plate of food | Recognized a black-and-white panda among bamboo |
| Red panda resting on wood | Described a blurred human face | Recognized the red panda resting its head on a wooden plank |
| Woman and dog on a beach | Invented a person holding a chip bag | Described the woman in a plaid shirt and the dog's high-five action |

**This passes a bounded signs-of-life and scene-grounding check.** It is not a
general accuracy benchmark: “forest” overstates what the panda crop establishes,
eating is uncertain from the still, “both smiling” in one API response attributes
an expression to the dog, and the UI's “golden retriever” breed label is unverified.
Keep these limitations alongside the successful subject/action recognition.
Physical camera capture and Reachy switching were not tested in this check.

The browser showed native server timing separately from transport-inclusive
browser timing, and resource sparklines updated. The previous inline-style CSP
errors were absent. An optional `/api/runtime` request still returned 404; the
existing fallback allowed captions to complete. No network settings changed.
Preserved private request logs and screenshots contain both failed and corrected
examples. Their differing cache conditions and output lengths do not establish
a latency comparison or performance improvement.

Use [the Wi-Fi switching guide](network-switching.md) to test another connection
and restore Ethernet. Wi-Fi changes remain untested:
no target network has been selected, and an authenticated USB console must be
re-established before attempting the handover.

For preboot access and firmware requirements, use NVIDIA's
[Orin Nano setup guide](https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/latest/quick_start.html)
and [USB interface documentation](https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/latest/hardware_layout.html).
Linux USB gadget serial is not a preboot UEFI or ISO-installer console.
