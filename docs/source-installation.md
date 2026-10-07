# Source Installation and Engineering Reference

[Back to the tutorial](../README.md). All shell commands in this guide run from the
repository root unless stated otherwise. This is the automated source-build path;
for fixed-engine container preparation, see [the runtime guide](../container/README.md).

**Upgrade candidate: TensorRT-Edge-LLM 0.11.0.** This branch requires freshly exported
and rebuilt engines. The source trial records successful builds, structural INT4 checks,
and corrected known-image captions. Packaged-runtime validation, physical camera streaming,
sustained memory and controlled latency measurements remain pending. Measurements elsewhere
in this guide describe the 0.10.1 baseline, not results for this candidate. See the
[upgrade recommendation and validation plan](edgellm-0.11-upgrade.md).

A minimal, low-RAM camera/VLM web UI that runs a locally-hosted **NVIDIA Cosmos3-Edge** model on a
Jetson Orin over TensorRT-Edge-LLM, with live **Reachy Mini** robot control (motors, onboard apps,
pose, text-to-speech) alongside it. No cloud calls, no framework - the web server
(`ui/scripts/serve_ui.py` + `engine_backends.py`) is standard-library Python; only robot control
(`reachy/reachy.py`) also needs `requests`, which `setup-orin.sh` installs as `python3-requests`.

```
USB webcam ──┐
             ├─▶ browser (getUserMedia) ──▶ Live Vision :8091 (http, redirects) / :8443 (https) ──▶ Cosmos3-Edge shim
Reachy Mini ─┘        │                           │
  camera/mic           │                    /api/reachy/* ──▶ Reachy Mini daemon :8000 (on the robot)
  (needs the bridge,    │                           │
   see below)           ▼                           ▼
                   /api/engines/*              motors, apps, speaker (Piper TTS)
                   (TensorRT engine swap)
```

Browser camera access requires a secure context, so :8091 (http) auto-redirects to :8443 (https,
self-signed - accept the browser warning once). Open `https://<orin-ip>:8443/` directly to skip
the redirect hop.

## What's here

| Path | What it is |
|---|---|
| `ui/` | Live Vision: `scripts/serve_ui.py` + `scripts/engine_backends.py` (stdlib-only `http.server`, no framework; robot control imports `reachy/reachy.py`, which needs `requests`) + `web/`. Camera/VLM UI, Reachy motor/app/TTS control, engine switching with a live progress bar. |
| `reachy/reachy.py` | Thin REST client for the Reachy Mini daemon (motors, pose, apps, volume, sound upload/play). Only third-party dependency: `requests`. |
| `reachy/reachy_mjpeg_bridge.py` | WebRTC-to-HTTP bridge for the robot's camera/mic (needed only for "switch to Reachy Mini" as a *video* source - motor/app/TTS control works without it). |
| `shim/cosmos3_shim_v1.py` | The Cosmos3-Edge TensorRT-Edge-LLM serving shim - an OpenAI-compatible `/v1/chat/completions` endpoint in front of the built engine. |
| `systemd/` | Unit templates for the three services (UI, shim, Reachy bridge) and two drop-ins for running a multi-GB resident model on an 8 GB board without swap thrashing. |
| `ui/tests/`, run via `python3 -m unittest discover -s ui/tests` and `node --test ui/tests/test_engine_switch.js ui/tests/test_reachy_setup.js` | Offline tests for inference relay, engine selection and optional Reachy setup; no device or GPU required. |
| `scripts/bootstrap.sh` | Run **from a laptop** (any OS, no GPU required; by default it does no compute itself). Copies this repo to the Orin over SSH and runs `setup-orin.sh` there. Optional `--host-quantize` (Linux host, 12 GB+ available RAM) does the LLM quantize+export step locally instead, for the SDK's primary V2/cuteDSL plugin instead of its legacy fallback - see "Host-accelerated quantization". |
| `scripts/set-reachy-ip.sh` | Run **on the Orin** to point the demo at a (different) Reachy Mini by IP or hostname. Uses the same discovery, verified connection and skip manager as the UI; never rebuilds or restarts the model engine. See "Optional Reachy Mini". |
| `scripts/setup-orin.sh` | Run **on the Orin** (`bootstrap.sh` does this for you; by hand, from `/opt/live-vision-cosmos-demo`: `export REACHY_MINI_IP=...` if you have a robot, then `sudo -E bash scripts/setup-orin.sh` - no Hugging Face token needed). Idempotent, one-shot: builds TensorRT-Edge-LLM, downloads the Cosmos3-Edge checkpoint, quantizes it to INT4 with the vendored RTN pre-quantization pass, exports and builds the engine, installs Piper/TLS/systemd, starts the services. |
| `scripts/flash-jetpack-sd-mac.sh` | Optional Mac one-shot for a clean JetPack 7.2.1 SD card; helpers in `scripts/jetpack/`, agent skill in `.agents/skills/flash-jetpack-sd-mac/`. |
| `AGENTS.md` | Setup/deployment recipe written for an AI coding agent to follow unattended, plus the "don't change these without re-deriving them" list for the pinned build constants below, and why checking generated text (not just health endpoints) matters. `CLAUDE.md` points here. |

## Advanced source installation

**Already running JetPack 7.2.1 / Jetson Linux 39.2.1? Keep it.** A fresh JetPack
installation is not needed; setup installs any missing pinned CUDA/TensorRT components.

**Need a fresh SD card first?** On an Apple Silicon MacBook with macOS 15+, 16 GB RAM,
60 GiB free disk, Apple Command Line Tools, Homebrew, internet, an SD reader, and
administrator/disk-access permission, run in Terminal:

```bash
brew install python qemu zstd
diskutil list                       # identify the 64 GB+ SD card
./scripts/flash-jetpack-sd-mac.sh --disk /dev/diskN --erase
```

This optional route targets the **Orin Nano Super 8 GB developer kit (P3767-0005)**
with compatible R39.2.1 QSPI firmware already installed. It prepares OS and NVIDIA drivers
only. Boot the card, complete first-boot setup, enable SSH, then continue below.
Validated on a **fresh 64 GB SD card** using this script and
[agent skill](../.agents/skills/flash-jetpack-sd-mac/SKILL.md): full image readback,
SD boot, wired first-time setup, a fresh login and guarded filesystem expansion
have passed on the new board. Bounded SDK 0.11 source-engine checks are recorded in
[first-boot recovery](first-boot-recovery.md); final packaged artifacts remain unvalidated.
See the [Mac workflow and validation record](jetpack-sd-mac.md) for scope.

Starting point: a Jetson Orin with **JetPack already flashed and booting**, reachable over
SSH, and nothing else installed - plus any laptop (any OS, no GPU required; by default the
laptop does no compute, it just drives the Orin over SSH). End point: a working
`https://<orin-ip>:8443/` serving live camera captions from a locally-built TensorRT engine.

```bash
# Optional: export REACHY_SETUP_SKIP=1 to skip robot connection explicitly
./scripts/bootstrap.sh jetson-user@orin-ip
```

That's the whole setup - no Hugging Face token needed.
It clones and builds TensorRT-Edge-LLM, downloads and quantizes the
Cosmos3-Edge checkpoint, exports and builds the engine, installs Piper/TLS/systemd, and
starts the model and UI, then offers optional Reachy discovery, a manual address, or skip. All compute happens on the Orin, over SSH from the laptop. It
takes well over an hour on an Orin Nano, dominated by the on-device TensorRT-Edge-LLM native
build; it's idempotent, so if it fails partway (a flaky download, a transient apt mirror),
fix the reported problem and re-run it - completed stages are not repeated. Each completed
stage leaves a root-owned marker in `/opt/live-vision-cosmos-demo/.setup-state/`; `sudo rm`
one to make the next run redo that stage.

`bootstrap.sh` copies this repo to `/opt/live-vision-cosmos-demo` on the Orin - always that
path, which `setup-orin.sh`, the systemd units and the shim hardcode - then runs
`setup-orin.sh` there as root over `ssh -t`. From a git checkout it copies the last commit
through `scripts/archive-source.sh`, not uncommitted edits. The Orin account's sudo password is asked for
twice: once to create and `chown` that directory, then for `setup-orin.sh` itself. Each step
is its own SSH connection, so with password login the SSH password is asked for at every one:
an unattended run needs passwordless sudo on the Orin and SSH key login to it.

**Changing Wi-Fi later:** finish setup over Ethernet and confirm a real image
caption first. Then follow [Change Wi-Fi after the demo works](network-switching.md)
for human and agent instructions, a guarded connection test, and restoration of
Ethernet. The guide clearly marks hardware checks that are still pending.

**Optional Reachy Mini:** the installer prepares the bridge and Piper even if you
skip the robot. When a terminal is interactive, it offers discovered robots, a
manual address, or **Skip**. It never selects an arbitrary robot. Noninteractive
runs defer the choice to **Reachy Mini setup** in the UI; `REACHY_SETUP_SKIP=1` records
an explicit skip. Camera captioning works without a robot.

Open Reachy Mini setup at any time to discover, connect, change or skip a robot.
Discovery uses only the robot's local mDNS service; if it is unavailable, find the
address in the Reachy dashboard or router, or continue without a robot. Connecting
checks the daemon before saving the address. These actions restart or stop only
the camera bridge; they do not rebuild or restart the model engine. See
[Reachy setup for humans and agents](reachy-setup.md).

The same actions are available on the Orin as the deployment account:

```bash
bash /opt/live-vision-cosmos-demo/scripts/set-reachy-ip.sh --discover
bash /opt/live-vision-cosmos-demo/scripts/set-reachy-ip.sh <verified-robot-address>
bash /opt/live-vision-cosmos-demo/scripts/set-reachy-ip.sh --skip
```

No argument shows status. For an older installation, first refresh this committed
project at its existing installation directory, then prepare only robot support:

```bash
sudo -E bash /opt/live-vision-cosmos-demo/scripts/setup-orin.sh --reachy-only
```

This installs missing robot dependencies and refreshes the UI/bridge configuration.
It may briefly restart the UI, but does not invoke model download, quantization,
export, engine building, clock changes or a shim restart. Do not fabricate setup
markers to skip work. Piper is an on-demand UI child process: its first speech
request starts the voice, which stays warm afterward. There is no Piper service
to start manually, and no Home Assistant installation is required.

**Robot-daemon recovery** is opt-in. When the robot's daemon wedges, the bridge can restart it
over SSH instead of someone power-cycling the robot. To set that up, run this on the Orin as
the bridge's service account (not root; it needs `sshpass`):

```bash
bash /opt/live-vision-cosmos-demo/reachy/setup_robot_recovery.sh   # [pollen@<robot-address>] [orin-address-the-robot-sees]
```

With no arguments it logs in to the robot in `reachy.env` as `pollen` and works out which Orin
address the robot sees. It installs a restricted key on the robot that can only restart the
daemon, then prints the systemd drop-in
(`/etc/systemd/system/reachy-mjpeg-bridge.service.d/10-recover.conf`) that turns recovery on in
the bridge. The key is per robot: after switching robots, run it again for the new one.

**Hugging Face access:** the pinned `nvidia/Cosmos3-Edge` revision
`344d602b128d1bbdacb43b08d0a3626f46343e29` is ungated and lists
[OpenMDW 1.1](https://openmdw.ai/license/1-1/). `HF_TOKEN` is optional for authenticated
downloads. Recheck license and access terms when changing the model revision.

If you're an AI coding agent doing this setup for someone, read `AGENTS.md` first - it
covers the one thing setup can need from a person (passwords), and which build parameters are
pinned to a measured working configuration rather than being arbitrary defaults.

The rest of this guide (below) is the manual, step-by-step reference the automation is
built from - useful for understanding what a stage actually does, debugging a failure, or
adapting the pipeline to a different checkpoint revision.

### Host-accelerated quantization (optional, `--host-quantize`)

```bash
./scripts/bootstrap.sh --host-quantize jetson-user@orin-ip
```

By default, the Orin itself quantizes the text tower and exports it with the TensorRT-Edge-LLM
SDK's **legacy V1 INT4 GEMM plugin** (`Int4GroupwiseGemmPlugin`, AWQ-swizzled weights) rather
than its **primary V2 plugin** (`Int4GroupwiseGemmPluginV2`, cuteDSL fragment-layout weights,
the SDK's own default when no `--int4-gemm-plugin-version` is given at all). This isn't a
deliberate quality choice - it's a fallback forced by a confirmed, reproducible constraint:
exporting with V2 **on an 8 GB Orin Nano gets killed by the kernel's OOM killer partway through
the export itself**, not at engine-build time. The source build keeps V1 as the on-device
fallback because V2 requires more export memory. The host path requires at least 12 GiB
available RAM; both paths require fresh validation of the resulting engine.

`--host-quantize` does the quantize+export step on **your laptop/workstation instead** - so
it uses the SDK's primary V2 path. Validate accuracy on the resulting engine. It runs
entirely on CPU; a GPU on the host is not required and is not used for this step. The export
is copied to a staging directory on the Orin
(`/opt/live-vision-cosmos-demo/checkpoints/Cosmos3-Edge/onnx/reasoning/llm.host`), flagged
complete only once the copy has finished, and `setup-orin.sh` adopts it (`adopt_host_export`):
it swaps the export in, marks its own `quantize` and `export_onnx_llm` stages done, and clears
the stages built on the old export (`fix_chat_template`, `build_engine`, `enable_services`,
`smoke_test`), so the engine is rebuilt from it. A copy that never finished is discarded, not
adopted. The vision tower is still exported on the Orin; it isn't the step that runs out of
memory. So `--host-quantize` works on a fresh Orin and also upgrades one already set up with
the default V1 path: re-run `bootstrap.sh` with `--host-quantize`, and completed stages such as
the SDK build and the checkpoint download are skipped.

**Requirements**, all hard gates:

| Requirement | Why |
|---|---|
| **Linux** (not macOS, at any RAM size) | NVIDIA's `cuda-bindings` package - a *base* dependency of `tensorrt-edgellm`, pulled in even just to export a checkpoint - ships `manylinux` and Windows wheels only. There is no macOS build at any Python version. A MacBook Pro or a Mac Studio with 128 GB of RAM cannot run this step; it is excluded on OS grounds, not resources. Windows users: use WSL. |
| **≥ 12 GiB available RAM** (`MemAvailable`; `HOST_QUANTIZE_MIN_RAM_GB` in `bootstrap.sh`) | The configured host preflight minimum. Hosts below it fall back to the on-Orin V1 export. Revalidate memory use for the final artifacts. |
| **`python3`, `git` and `rsync`** on the host | `git` clones TensorRT-Edge-LLM at the pinned commit, `python3` gets a local venv with its `export` extra for the quantize and export, and `rsync` copies the export to the Orin. |

If any check fails, `bootstrap.sh` prints which one, then **falls back to the standard on-Orin
path automatically** - it does not abort the whole setup. A failure later in the host-side step
falls back the same way, and a missing `nvidia-smi` on the host is only a warning. The resulting
engine requires validation for coherent text and accurate captions with the selected plugin.

If you're an AI agent running this setup unattended on behalf of someone with only a
light-RAM or macOS laptop available, do not try to work around these checks (e.g. by lying
about free RAM, or attempting the export on an unsupported OS) - they're hard platform/
resource limits, not conservative guesses. Tell the user plainly that they got the on-Orin
legacy-plugin build, why, and that it still needs device validation.

## Requirements

- **Jetson Orin (Nano or better) booting JetPack 7.2.1 / L4T 39.2.1.** Keep an existing
  compatible installation, or use the optional [Mac SD workflow](jetpack-sd-mac.md)
  for the supported Orin Nano Super developer kit before application setup.
- **A 64 GB SD card or larger is recommended.** The build needs CUDA/TensorRT development
  packages and intermediate model files in addition to the application and engine.
  The setup script checks available build space; measure the final candidate's installed footprint.
- **NVIDIA's [TensorRT-Edge-LLM](https://github.com/NVIDIA/TensorRT-Edge-LLM) SDK**, built on
  device, to both build the Cosmos3-Edge engine and provide the Python venv the shim runs from.
  Not vendored in this repo - it's a substantial NVIDIA SDK with its own license and update cycle.
- Reachy Mini robot on the same LAN, if you want the robot-control features. Everything else
  (webcam + Cosmos3-Edge captioning) works without one.

## Setup

The [source installation](#advanced-source-installation) above automates everything in this section via
`scripts/setup-orin.sh`. What follows is the manual, step-by-step version of the same
recipe - read it to understand what a build stage actually does, to debug a failure, or to
adapt the pipeline to a different checkpoint revision. It is not a second way to set this up;
it's what the automation runs, spelled out.

### 1. Build TensorRT-Edge-LLM and the Cosmos3-Edge engine

Everything in this step lives inside this repo's checkout at `/opt/live-vision-cosmos-demo`, so
make that first - the `mkdir`/`chown` and `git clone` lines of step 2 - and run the commands
below from there. Clone [NVIDIA/TensorRT-Edge-LLM](https://github.com/NVIDIA/TensorRT-Edge-LLM)
at commit `95515c2f87fba8982db5a519f9022277667b3cc9` (tag `v0.11.0`), with its submodules, into
`/opt/live-vision-cosmos-demo/external/TensorRT-Edge-LLM`, give it a Python venv at `.venv`
inside that directory, and build its native runtime per its own instructions
(`kernelSrcs/build_cutedsl.py` for the `fmha` and `int4_fp16_gemm` kernel groups, then a CMake
configure/build for the `_edgellm_runtime`, `NvInfer_edgellm_plugin`, `llm_build`, and
`visual_build` targets), then pip-install the SDK itself into that venv, editable, with its
`server,server-tools,export,tools` extras: that install is what provides the
`.venv/bin/tensorrt-edgellm-export` the export steps below call, and the FastAPI and uvicorn the
shim imports. The build needs the pinned CUDA 13.2 / TensorRT 10.16 packages first -
`sudo bash scripts/install-jetpack-compute.sh` (the `jetpack_compute` stage) - plus the apt
packages in `do_system_packages`. See `scripts/setup-orin.sh`'s `do_fetch_edgellm`,
`do_edgellm_venv` and `do_build_edgellm_runtime` for the exact commands and environment this
repo builds against. The shim unit's `ExecStart` (the venv's python) and the shim's
`EDGELLM_ROOT` default (where it finds the SDK's `build/` outputs) both expect that location;
with the SDK anywhere else, edit `ExecStart` and set `EDGELLM_ROOT` for the shim - and
`SHARED_CHECKPOINT_DIR`, whose default is relative to it. Then:

1. **Download `nvidia/Cosmos3-Edge` at revision `344d602b128d1bbdacb43b08d0a3626f46343e29`**
   from Hugging Face into `checkpoints/Cosmos3-Edge/raw` (see NOTICE.md - the model is under the
   [OpenMDW License 1.1](https://openmdw.ai/license/1-1/); it isn't gated, so no Hugging Face
   token is needed). Pin that revision - the quantizer below refuses a receipt (step 2)
   for any other. The reasoner's LLM weights are indexed directly at the snapshot root by
   `model.safetensors.index.json` (698 tensors); the vision tower is a separate
   `vision_encoder/` subfolder. Neither needs the sibling `vae/` (image/video generation
   decoder) or other omni components this demo never loads - `scripts/setup-orin.sh`'s
   `do_download_checkpoint` reads the root index first to fetch exactly the shards it
   references, plus `vision_encoder/*` and the root-level files in its `allow` list (tokenizer,
   chat template, generation, preprocessor and processor configs, and the `README.md` model
   card).
2. **Check the four multimodal token IDs in the downloaded checkpoint's `config.json`**
   before export - `image_token_id`, `video_token_id`, `vision_start_token_id`,
   `vision_end_token_id`. The exporter needs them and does not derive them on its own. The
   pinned revision's `config.json` already carries them, with the values this repo's deployed
   engine uses (`19`, `18`, `20`, `21` respectively); `do_fix_raw_config` adds any that are
   missing. **Then write the download receipt the quantizer requires**,
   `results/model-download.json` in the repo root, as `do_fix_raw_config` does right after
   that check: `"revision": "344d602b128d1bbdacb43b08d0a3626f46343e29"`,
   `"complete": true`, and under `"files"` a `sha256` for every top-level `.json`, `.jinja` and
   `.md` file of the checkpoint plus both `size_bytes` and `sha256` for every weight shard.
   Write it last: the quantizer checks `config.json` against it, and copies only the metadata
   files it lists into the quantized checkpoint.
3. **Quantize to INT4 with `vendor/quantize_cosmos3_rtn.py`** (CPU-only, symmetric
   round-to-nearest, group-128; not part of NVIDIA's TensorRT-Edge-LLM SDK - see NOTICE.md
   for where this script comes from). It isn't executable: run it with the SDK venv's python
   (as `do_quantize` does), here from the repo root:

   ```
   external/TensorRT-Edge-LLM/.venv/bin/python vendor/quantize_cosmos3_rtn.py \
     --source checkpoints/Cosmos3-Edge/raw --output checkpoints/Cosmos3-Edge/quantized \
     --quantization-scope all-linears --apply
   ```

   `--output` must be inside this repo and must not exist yet - the script never overwrites
   one, so delete it to redo the step. It accepts only a receipt whose `revision` is the pinned
   one and whose `complete` is true, but it never reads the downloaded snapshot's own revision,
   so that check is only as good as the pin in step 1. It then checks the source's
   `config.json`, index, shard sizes and SHA-256 hashes (and those of the metadata files it
   copies) against the receipt, and stops at the first mismatch.

   The exporter's own on-the-fly `--quantization` flag
   does **not** work for this checkpoint - it's gated on Mixture-of-Experts detection in
   `tensorrt_edgellm/scripts/export.py`, and Cosmos3-Edge's reasoner is a *dense* model
   (`config.json`: `backbone_type: "cosmos3_edge_nemotron_dense"`); the flag silently exports
   plain FP16 with no warning at all, which runs and serves correctly but at ~4x the memory
   and meaningfully slower than real INT4. A real pre-quantization pass is required.
   **The quantizer's own output metadata matters as much as its math.** It must embed
   `"quantization_config": {"quant_method": "gptq", "group_size": 128}` **directly in
   `config.json`** - that exact key (`quant_method`, not `quant_algo`) in that exact location
   is the *only* code path `tensorrt_edgellm/config.py`'s `_algo_to_quant_type()` recognizes
   as GPTQ-packed; it has no "GPTQ" branch for any `quant_algo` string at all. A sidecar
   `hf_quant_config.json` file (a very natural thing to write, and what earlier versions of
   this quantizer did) is checked for *existence* first and unconditionally short-circuits
   detection before the parser would ever reach the correct path - so even a well-formed
   sidecar file silently routes the checkpoint to `QUANT_FP16`, and the packed INT4 tensors
   underneath get loaded as if they were raw FP16 weight bytes. That produces a checkpoint
   which builds and serves without any error, with structurally valid API responses and
   correct token counts, but generates complete garbage text - repeated nonsense characters -
   on every request, regardless of prompt. Confirmed by isolation testing on real hardware:
   the packed weight values and zero-point convention were correct all along (verified
   against `repack_gptq_to_plugin`'s exact unpacking math in
   `tensorrt_edgellm/checkpoint/repacking.py`); only the metadata was undiscoverable.
4. **Export the LLM with `--int4-gemm-plugin-version 1`, not the default 2**, into
   `checkpoints/Cosmos3-Edge/onnx/reasoning` - the shim also reads the `llm/` directory this
   writes there, at runtime (`SHARED_CHECKPOINT_DIR`):

   ```
   external/TensorRT-Edge-LLM/.venv/bin/tensorrt-edgellm-export checkpoints/Cosmos3-Edge/quantized \
     checkpoints/Cosmos3-Edge/onnx/reasoning --task reasoning --skip-visual --int4-gemm-plugin-version 1
   ```

   No `--quantization` flag needed or wanted here; the exporter auto-detects the pre-quantized
   checkpoint from `config.json` itself. V1 (the legacy AWQ-swizzled plugin) exports within the
   board's RAM. With the default V2 (cuteDSL fragment-layout) plugin, the export itself gets
   OOM-killed on an 8 GB Orin Nano. Use the host path for V2 and validate the
   resulting engine on the Orin; see
   "Host-accelerated quantization" above.
5. **Export the vision tower to ONNX too, from the same downloaded snapshot** - the raw
   checkpoint (`vision_encoder/` + the snapshot root's `config.json`), not the quantized one:
   no quantization, vision stays FP16. This is `do_export_onnx_visual`; step 7's `visual_build`
   turns its output into the vision engine:

   ```
   external/TensorRT-Edge-LLM/.venv/bin/python scripts/fix_cosmos_visual_layout.py \
     external/TensorRT-Edge-LLM
   external/TensorRT-Edge-LLM/.venv/bin/tensorrt-edgellm-export checkpoints/Cosmos3-Edge/raw \
     checkpoints/Cosmos3-Edge/onnx/reasoning --task reasoning --skip-llm
   ```

   The revision- and digest-guarded repair aligns the pinned exporter with the native
   runner's HWC patch layout. It rejects unexpected SDK source; do not bypass it.

   A vision engine built against a *different* checkpoint's externalized/refit weights will not
   load (`missing tensor model.projector.linear_fc1.bias`) - always build both from the same
   source.
6. **Validate the provider's `chat_template.jinja` after export.** Version 0.11 renders
   the original template in C++ and no longer supports `processed_chat_template.json`.
   `do_validate_chat_template` requires the exported template to match the pinned
   checkpoint byte-for-byte and rejects stale template artifacts. Do not apply the old
   JSON `content_types` repair or copy a template from another model.

   ```bash
   cmp checkpoints/Cosmos3-Edge/raw/chat_template.jinja \
     checkpoints/Cosmos3-Edge/onnx/reasoning/llm/chat_template.jinja
   ```
7. **Build the engines** as `do_build_engine` does: the SDK build's own binaries, with
   `EDGELLM_PLUGIN_PATH` set and a temporary swapfile on. Without `EDGELLM_PLUGIN_PATH`, both
   binaries look for the plugin at the relative path `build/libNvInfer_edgellm_plugin.so` and
   fail to parse the ONNX; without the swap, `llm_build` is OOM-killed on an 8 GB Orin Nano
   while serializing the engine.

   ```
   mkdir -p data engines/reasoning
   sudo fallocate -l 4G data/build-swap.img && sudo chmod 600 data/build-swap.img
   sudo mkswap data/build-swap.img && sudo swapon data/build-swap.img
   export EDGELLM_PLUGIN_PATH=/opt/live-vision-cosmos-demo/external/TensorRT-Edge-LLM/build/libNvInfer_edgellm_plugin.so
   external/TensorRT-Edge-LLM/build/examples/llm/llm_build \
     --onnxDir checkpoints/Cosmos3-Edge/onnx/reasoning/llm --engineDir engines/reasoning \
     --maxInputLen 1024 --maxKVCacheCapacity 1024 --maxKVPoolPages 8 --maxBatchSize 1
   external/TensorRT-Edge-LLM/build/examples/multimodal/visual_build \
     --onnxDir checkpoints/Cosmos3-Edge/onnx/reasoning/visual --engineDir engines/reasoning \
     --minImageTokens 4 --maxImageTokens 1024 --maxImageTokensPerImage 512
   sudo swapoff data/build-swap.img && sudo rm data/build-swap.img
   ```

   The visual engine lands under `engines/reasoning/visual/` automatically. These specific
   input/KV/batch capacities match RAM headroom measured on an 8 GB Orin Nano - raising them
   needs re-measuring, not just a flag change. The per-image token budget is fixed here too
   (`--maxImageTokensPerImage 512`); the shim ignores a per-request
   `max_image_tokens_per_image`. `/opt/live-vision-cosmos-demo/engines/reasoning` is what
   `engine-link` should point at below - `shim/cosmos3_shim_v1.py` reads `llm.engine` +
   `config.json` from there.

### 2. Install this repo

(If you already made the checkout for step 1, start at the `cd`.)

```
sudo mkdir -p /opt/live-vision-cosmos-demo && sudo chown $USER /opt/live-vision-cosmos-demo
git clone --branch feat/live-vision-cosmos-demo https://github.com/NVIDIA-AI-IOT/live-vision-cosmos-demo.git /opt/live-vision-cosmos-demo
cd /opt/live-vision-cosmos-demo
ln -s /opt/live-vision-cosmos-demo/engines/reasoning engine-link   # the directory containing llm.engine + config.json
cp ui/config/engines.example.json engines.json # edit "path" to match the engine-link above
```

### 3. Set up text-to-speech

```
python3 scripts/fetch-piper.py --output /opt/live-vision-cosmos-demo/piper
```

The default voice is `en_US-ljspeech-medium`. Measure speech latency on your own
configuration if you enable this optional source-install feature. See NOTICE.md
before substituting a different voice - several published Piper voices carry non-commercial-only
licenses inherited from their base voice, not obvious from the filename. Custom voices
require a reviewed `PIPER_DOWNLOAD_MANIFEST` containing pinned URLs, checksums and notices.

### 4. Set up the Reachy Mini bridge (optional - only for using Reachy Mini as a video source)

```
python3 -m venv reachy_env && reachy_env/bin/pip install -r reachy/requirements.txt
```

Installs cleanly from prebuilt aarch64 wheels on JetPack + Python 3.12, no compilation needed.
`requirements.txt`'s own header explains the pinned `aiortc==1.10.1`: it's the version verified
against the robot. It has aiortc's RTX-decoding bug too - 1.10.1 and 1.14.0 both do, so upgrading
doesn't fix it; the bridge avoids it by answering H.264 without RTX. Motor/app/TTS control
(`--reachy-daemon-url`) does not need this bridge - it talks to the robot's own REST API directly.

### 5. TLS certificate

```
mkdir tls && openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout tls/key.pem -out tls/cert.pem -subj "/CN=live-vision-cosmos-demo"
```

### 6. Services config and systemd units

Prefer the installer, or `setup-orin.sh --reachy-only` when upgrading only robot
support. It renders the service account, prepares a writable `reachy.env`, installs
restricted bridge-control permissions and registers the bridge in `services.json`
without duplicating an existing entry.

The UI always keeps `--reachy-config` and the Piper paths. An empty robot config is
supported; do not remove those flags to skip Reachy. The bridge unit is installed
and enabled but its condition skips startup when the address is empty. The UI
loads connection changes dynamically and Piper starts only when speech is used.

`services.json` describes additional processes for the resource panel. A stopped
bridge after **Skip** is expected; Reachy Mini setup distinguishes prepared from
connected. The UI account may restart or stop only the fixed bridge service for
robot configuration. It receives no package-install or arbitrary service rights.

Engine switching (`/api/engines/*`) starts and stops the shim via `sudo -n systemctl {start,stop}
live-vision-cosmos-demo-shim.service` (its `service` value in `engines.json`, which the rule
must match exactly) - grant that specific, narrow passwordless sudo rule to whichever user runs
the UI service, rather than broader sudo access:

```
echo 'YOUR_USERNAME ALL=(root) NOPASSWD: /usr/bin/systemctl start live-vision-cosmos-demo-shim.service, /usr/bin/systemctl stop live-vision-cosmos-demo-shim.service' | sudo tee /etc/sudoers.d/live-vision-cosmos-demo
```

The automated setup also pins the clocks (`do_pin_clocks`): `sudo nvpmodel -m 2` (`MAXN_SUPER` on
the Orin Nano Super; mode IDs differ between Jetson modules, so check `nvpmodel -q` afterwards)
and a small boot-time `jetson-clocks.service`, since `jetson_clocks` doesn't survive a reboot.
The historical 0.10.1 trial reported a 36–60 ms reduction per request; remeasure the current engine before making a performance claim.

Open `https://<orin-ip>:8443/`.

**Before trusting this deployment, check the actual generated text, not just that the UI
loads and services report ready.** A broken engine has been observed to pass both of those
checks - health endpoints ready, structurally valid API responses with correct token counts
- while every actual generated response was complete garbage. Submit a real request
(through the UI, or `curl .../v1/chat/completions` on the shim's port directly) and confirm
the response is coherent text matching the prompt.

## Adding a second engine

`engines.json` is a registry, not a single path - add a second entry with its own `backend_port`,
`service`, and `path` (pointing at a second built engine directory) to get a live model-switch
button between them, with a real elapsed-time progress bar during the swap. Every registered
`protocol` must currently be `"cosmos"` (an OpenAI-compatible `/v1/chat/completions` + `/health/ready`
shim, same contract as `shim/cosmos3_shim_v1.py`) - `ui/scripts/engine_backends.py` validates the
registry strictly and will refuse anything else.

The second entry's `service` is a separate systemd unit you create - for example a copy of
`live-vision-cosmos-demo-shim.service` under a new name, with `Environment=ENGINE_DIR=...` and
`Environment=PORT=...` set to that engine directory and its `backend_port` (see
`shim/cosmos3_shim_v1.py` for the other environment variables it reads). Leave it disabled;
switching starts and stops it. For a copy of this shim, keep the entry's `model_id` at
`nvidia/Cosmos3-Edge`: a switch waits for the new service's `/v1/models` to list the entry's
`model_id`, and this shim always reports `nvidia/Cosmos3-Edge`. A switch runs
`sudo -n systemctl stop` on every other registered service and `sudo -n systemctl start` on the
selected one, so add `start` and `stop` rules for the new unit - spelled exactly as its
`service` value in `engines.json` - to `/etc/sudoers.d/live-vision-cosmos-demo`, then check the
file with `sudo visudo -cf /etc/sudoers.d/live-vision-cosmos-demo`. Without them, the first
switch stops the shim, can't start the new service or restore the old one, and inference stays
down.

## Running the tests

```
python3 -m unittest discover -s ui/tests
node --test ui/tests/test_engine_switch.js ui/tests/test_reachy_setup.js
```

The original UI suites cover the following behaviors. No device, network, GPU, or model required - a fake HTTP backend (Python) and a
fake DOM (JS) stand in for both.

## License

This repository's own code is Apache License 2.0 - see [LICENSE.md](../LICENSE.md). It talks to and builds on
several third-party components under their own licenses - see `NOTICE.md` before redistributing,
particularly regarding the Piper voice model and NVIDIA Cosmos model weights, neither of which
this repository ships. The source bodies of `reachy/reachy_stream/stream.py` and `const.py`
are copied unchanged from Pollen Robotics'
[reachy_mini_homeassistant](https://github.com/pollen-robotics/reachy_mini_homeassistant), also
under the Apache License 2.0.
The upstream [reachy/reachy_stream/LICENSE](../reachy/reachy_stream/LICENSE) retains
Pollen Robotics' Apache-2.0 copyright notice. Local changes to those two files are
license and provenance headers. See [CONTRIBUTING.md](../CONTRIBUTING.md) for this
application's DCO sign-off requirements.
