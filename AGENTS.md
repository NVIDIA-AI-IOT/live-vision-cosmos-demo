# Agent instructions

## This branch: TensorRT-Edge-LLM 0.11 migration candidate

Use SDK commit `95515c2f87fba8982db5a519f9022277667b3cc9`. Read
[the upgrade assessment](docs/edgellm-0.11-upgrade.md) before deployment. Offline checks
do not establish Orin build success, visual accuracy, RAM footprint or latency. Historical
measurements below describe the 0.10.1 deployment. Re-export and rebuild both engines;
do not reuse 0.10.1 serialized engines or apply its JSON chat-template repair. The current
installer validates the checkpoint's original Jinja and invalidates stale SDK-dependent
stage markers. Use a fresh SD installation for this trial so the existing demo stays available.

This repo is a one-shot deployable demo: **Live Vision UI + Reachy Mini robot control +
NVIDIA Cosmos3-Edge**, running on a Jetson Orin. If you were asked to "set up" or "deploy"
this demo, the goal is: a fresh Orin (JetPack already flashed, nothing else installed) plus
any laptop → this repo's automation → a working `https://<orin-ip>:8443/` serving live
camera captions from a locally-hosted TensorRT engine.

You do not need to understand TensorRT-Edge-LLM, Cosmos3-Edge's checkpoint format, or CUDA
to do this. The two scripts below encode all of that. Read this whole file before running
anything - it tells you which parts are safe to just run and which ones need a human.

## Optional JetPack preparation

Keep an existing booting JetPack 7.2.1 / L4T 39.2.1 installation. Missing CUDA/TensorRT
components are installed by demo setup; they are not a reason to reflash.
For an explicitly requested fresh SD card from a Mac, use
[the flash skill](.agents/skills/flash-jetpack-sd-mac/SKILL.md) and
[its workflow](docs/jetpack-sd-mac.md) before returning here. OS preparation requires
identifying the card and macOS administrator authentication.

### First boot is a separate acceptance gate

Read [First-boot setup and recovery](docs/first-boot-recovery.md) before discovering
or configuring a newly connected board. Identify it through the USB connection or
its local console; never probe a historical Orin IP to find a new device. Keep an
existing deployment untouched unless the user explicitly targets it. Do not remove
another board's SSH key or accept a mismatch to bypass device identification.

Use `python3 scripts/jetpack/first_boot.py probe --json` for a read-only USB/serial
inventory. If USB is absent, check power and reconnect the data cable once before
inferring anything about firmware. A login prompt does not validate account creation.
Stop after one controlled failed login with explicitly supplied credentials; do not
guess usernames/passwords or assume wizard cancellation was safe. For an account
failure, identify the returned SD by reader, size and available serial, then use the
guide's read-only offline audit before choosing a repair. The audit is not a reset.

Before running bootstrap, verify a fresh successful login, the intended account,
SD root device, L4T release, filesystem expansion and this board's network identity.
State unknowns explicitly. Neither a flash receipt nor a reachable login prompt
establishes those results.

## Network changes after installation

Follow [the network-switching guide](docs/network-switching.md) only after verified
real-image inference over Ethernet. Keep an authenticated USB console during the
Wi-Fi test, identify profiles from the actual board, and enter Wi-Fi secrets in
the local prompt. Restore Ethernet and verify another caption before declaring
the network test complete. Keep device details and secrets out of source control.

## Model access needs no human

The pinned Cosmos3-Edge revision `344d602b128d1bbdacb43b08d0a3626f46343e29` is
ungated and lists OpenMDW 1.1. `HF_TOKEN` is optional; never request that a token be
pasted into chat or committed. See NOTICE.md for the pinned model card and agreement.
A different revision needs a fresh check of its terms and access requirements.

This imported tree is a release candidate. The original source installation remains the
advanced path; container/README.md describes the fixed-engine packaged path. A readiness
endpoint or declared INT4 setting does not prove accuracy or release approval. Preserve
the source's pinned build configuration until measurements on the final artifacts justify
a change. Do not put engines, weights, credentials or internal approval records in this tree.

Everything below is safe to run autonomously, except typing passwords - the only step that
needs a human. `bootstrap.sh` (and anything below run with `sudo` on the Orin) needs the Orin
account's sudo password unless that account has passwordless sudo, and `bootstrap.sh` needs the
account's SSH password at every connection unless you log in with a key - see "Running it".

## Running it

From any laptop with `bash`, `ssh`, `scp`, `tar`, and network access to the Orin - no GPU, no
particular OS, no local Python/CUDA toolchain needed there:

```bash
export REACHY_MINI_IP=192.0.2.50   # optional - omit if there's no robot
./scripts/bootstrap.sh jetson-user@orin-ip
```

`bootstrap.sh` asks for the Orin account's sudo password twice: once to create
`/opt/live-vision-cosmos-demo` and make the SSH login account its owner, then again to run
`setup-orin.sh` as root. An unattended run needs passwordless sudo on the Orin and SSH key login
to it (`bootstrap.sh` opens a new SSH connection for each step, so a password login is asked for
at every one); otherwise the user has to be there to type the passwords.

Reachy Mini is optional. Read [Reachy setup](docs/reachy-setup.md). The installer
always prepares bridge dependencies and Piper so the user can add a robot later
from the UI's **Reachy Mini setup**. Interactive setup offers bounded mDNS discovery,
a manual address, or skip. Noninteractive setup defers to the UI; use
`REACHY_SETUP_SKIP=1` only when the user explicitly wants to skip. Never select an
arbitrary robot or sweep a subnet. If discovery finds no usable robot, ask the user
to locate it in the Reachy dashboard/router, supply a verified address, or skip.

The shared settings manager verifies the daemon and changes only robot configuration
and the fixed bridge service. It does not rebuild or restart the model engine.
The CLI equivalents, run as the deployment account on the intended Orin, are:

```bash
bash /opt/live-vision-cosmos-demo/scripts/set-reachy-ip.sh --discover
bash /opt/live-vision-cosmos-demo/scripts/set-reachy-ip.sh <verified-robot-address>
bash /opt/live-vision-cosmos-demo/scripts/set-reachy-ip.sh --skip
```

No argument reports status. If an older installation lacks the manager, dependencies
or dynamic UI configuration, refresh this project in its existing directory and
run `sudo -E bash /opt/live-vision-cosmos-demo/scripts/setup-orin.sh --reachy-only`.
This path may install robot dependencies and restart the UI, but never enters
model download, quantization, export, engine building or shim control. Do not run
the full installer or fabricate engine markers merely to add a robot.

Piper is installed even when Reachy is skipped. It is a persistent UI child started
by the first speech request, not a separate systemd service. A skipped bridge is
prepared but stopped. Home Assistant is not a runtime dependency. Robot-daemon SSH
recovery remains separately opt-in; see docs/source-installation.md before enabling it. Configuration
and discovery must not move the robot or start an onboard application.

`bootstrap.sh` copies this repo to `/opt/live-vision-cosmos-demo` on the Orin over SSH - a
`git archive` of `HEAD`, so commit any local edits you want deployed (outside a git checkout
it tars the directory instead) - then runs `scripts/setup-orin.sh` there over `ssh -t`, under
`sudo -E` on the Orin. That second script does every GPU- and compute-heavy step **on the
Orin**, in order:

1. Install apt build/runtime packages.
2. Clone NVIDIA's TensorRT-Edge-LLM SDK at its pinned commit and build its native runtime
   (CUDA kernels, `llm_build`/`visual_build` binaries) - this is the slow step, easily
   45+ minutes on an Orin Nano.
3. Download the Cosmos3-Edge checkpoint from Hugging Face (no token needed).
4. Quantize the checkpoint to INT4 with the vendored RTN pre-quantization pass, export to
   ONNX, then build the final TensorRT engine.
5. Prepare TLS and systemd services; start the model/UI, poll readiness and print the URL.
6. Prepare optional Piper (TTS) and Reachy bridge dependencies, then offer robot setup.
   The bridge stays off until a verified robot is selected. If optional preparation
   fails, keep inference available and retry with `setup-orin.sh --reachy-only`.

It is **idempotent** - every stage records a marker under
`/opt/live-vision-cosmos-demo/.setup-state/`. If it fails partway (a flaky download, a
transient apt mirror issue), fix whatever it reported and just run `bootstrap.sh` again;
completed stages are skipped, not redone (the markers are root-owned files - `sudo rm` one to
make that stage run again). Do not try to skip ahead by hand-editing markers
unless you're certain a stage's actual output is already correct on disk.

For a failure after both ONNX exports, read the
[installer recovery guidance](docs/first-boot-recovery.md#if-the-demo-installer-stops).
The preflight can select a 12 GiB remaining-space allowance only after checking all
preceding stages, exported tensor-file ranges and the provider Jinja. Do not lower
the threshold or create markers by hand. On util-linux 2.39.3, select swap report
columns with `swapon --show=NAME,SIZE`; `--output` silently selects a different option
and caused a verified false rejection of active swap during this trial.

**Optional `--host-quantize`** (`./scripts/bootstrap.sh --host-quantize jetson-user@orin-ip`)
does step 4's LLM quantize+export on the laptop instead of the Orin, getting the SDK's
primary V2/cuteDSL plugin instead of its legacy V1 fallback - see the Boundaries entry on
`--int4-gemm-plugin-version` below and docs/source-installation.md "Host-accelerated quantization" for why
this exists and its exact requirements (Linux, 12+ GB available RAM, and `python3`, `git` and
`rsync` on the laptop; no macOS at any RAM size, no GPU needed). It's a genuine improvement
when available, not required. Validate the resulting engine for either plugin. The laptop copies its export to a staging directory on the Orin, which `setup-orin.sh`
adopts and rebuilds the engine from - so this also upgrades an Orin already set up on the
default path: re-run `bootstrap.sh` with `--host-quantize`, and the Orin skips the stages it
has already completed, such as the SDK build and the checkpoint download. If the laptop you're
working from is a Mac or visibly RAM-constrained, don't bother attempting it; say so and
proceed with the default path rather than letting the preflight check fail loudly for a user
who didn't ask for it.

## If bootstrap.sh isn't an option (no SSH from this laptop, etc.)

SSH into the Orin yourself, get this repo onto it by whatever means you have (the point of
`bootstrap.sh` is just "get the repo there and run one script" - any transport works), then:

```bash
cd /opt/live-vision-cosmos-demo   # scripts/setup-orin.sh expects to live at this exact path -
                                   # systemd units and the shim's own defaults hardcode it
export REACHY_MINI_IP=192.0.2.50   # optional - omit if there's no robot
sudo -E bash scripts/setup-orin.sh
```

The account that runs `sudo` owns the install and runs the services; export `SERVICE_USER` as
well to choose a different one. The Orin itself needs internet access on this path too: setup
downloads apt packages, the SDK, Python packages, the checkpoint and Piper.

## Verifying success

```bash
curl -k https://<orin-ip>:8443/          # UI responds
curl http://<orin-ip>:8001/health/ready  # shim reports {"status":"ready"}
```

**This is not enough on its own.** A broken checkpoint has been observed to pass both of
these checks - services active, health endpoints reporting ready, structurally valid
OpenAI-format API responses with correct token counts - while every actual generated
response was complete garbage (repeated nonsense characters, not real text in any
language). Always also submit a real request and read the actual text:

```bash
curl -s http://<orin-ip>:8001/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "nvidia/Cosmos3-Edge",
  "messages": [{"role": "user", "content": "Say hello and count from 1 to 5."}],
  "max_tokens": 32
}'
```

The response's `choices[0].message.content` must be coherent English matching the prompt.
If it isn't - even if every health check passed - something is genuinely broken; see
`vendor/quantize_cosmos3_rtn.py`'s header comment for the bug this once was: quantization
metadata the checkpoint loader couldn't recognize, so it read the packed INT4 weights as FP16.
(`scripts/setup-orin.sh`'s `do_quantize` comment covers the wrong fix tried first - an
on-the-fly quantization flag that silently exported FP16, which gave coherent text, so this
check can't catch it; see the Boundaries entry on `--quantization int4_awq` below.)
Or just open `https://<orin-ip>:8443/` in a browser (accept the self-signed cert warning
once), grant camera permission, and confirm captions stream in and actually describe what
the camera sees.

The 0.11 trial also passed coherent text while producing incorrect image captions.
Its ONNX exporter permuted patch weights to CHW although the native Cosmos runner
supplies HWC pixels. `scripts/fix_cosmos_visual_layout.py` corrects that specific
pinned source during visual export, preserving the original notices. It rejects
other SDK revisions or unexpected source edits. A same-SDK retry rebuilds vision
without re-quantizing or replacing the finished language engine. Inspect the
actual exported weights and repeat the same known-image tests before accepting
the repair; a successful build or readiness response is insufficient.

## Boundaries - things not to "fix" without re-deriving them

Several values in `scripts/setup-orin.sh` are retained from the measured 0.10.1
deployment to control the first comparison. They are not new 0.11 performance results.
Don't change them speculatively:

- **`nvpmodel -m 2` (`do_pin_clocks`).** Mode 2 is `MAXN_SUPER` on the Orin Nano Super this
  was measured on - an uncapped power mode one step above the board's "25W" default (mode 1).
  Combined with `jetson_clocks` (pinned to a boot-time systemd unit, since it is never
  persistent across a reboot on its own regardless of nvpmodel mode). Re-measure power,
  accuracy, memory and latency for the release artifacts. `nvpmodel -m`
  silently keeps the previous mode if the requested ID doesn't exist on a different board
  (mode IDs are not portable across Jetson modules), which is why `do_pin_clocks` checks
  `nvpmodel -q` reports `MAXN_SUPER` afterward and warns (doesn't die) if not.
- **`--int4-gemm-plugin-version 1`** in the on-Orin export step (`do_export_onnx_llm`).
  Exporting with the default V2 (cuteDSL) plugin **on an 8 GB Orin Nano gets OOM-killed by
  the kernel partway through the export itself**. Keep V1 for the on-device export and
  require at least 12 GiB available RAM for the host export. Revalidate memory use, accuracy,
  and the actual engine precision on the final artifacts.
  `scripts/bootstrap.sh --host-quantize` does the export on a capable host instead (Linux,
  12+ GB available RAM - no GPU needed, it's RAM-bound not GPU-bound) and gets the V2 plugin
  as a result; without it, or on a host that doesn't
  qualify (macOS at any RAM size, or a light-RAM Linux machine), the Orin does this step
  itself with V1; validate its output before use. See docs/source-installation.md "Host-accelerated
  quantization" for the full requirements and what actually differs. If you're setting this
  up unattended for someone on a Mac or a light-RAM laptop, tell them plainly that they got
  the on-Orin V1 build and why - don't silently stay quiet about it, and don't try to force
  V2 on the Orin directly; that just reproduces the OOM kill above.
- **`--maxInputLen 1024 --maxKVCacheCapacity 1024 --maxKVPoolPages 8 --maxBatchSize 1`** in
  the `llm_build` step. These match this repo's actual deployed engine's own `config.json`
  `builder_config` byte-for-byte. Raising them changes RAM headroom on an 8 GB board - do
  not bump them without re-measuring.
  `--parallel 1` in the TensorRT-Edge-LLM native build. More workers OOMs a stock Orin Nano
  during compilation; this was a measured constraint, not a default left unchanged.
- **Do not pass `--quantization int4_awq` (or any `--quantization` flag) to the export step.**
  It looks like the obvious way to get an INT4 engine, but it's a complete no-op for this
  checkpoint: it's gated on Mixture-of-Experts detection in
  `tensorrt_edgellm/scripts/export.py`, and Cosmos3-Edge's reasoner is a *dense* model. It
  silently exports plain FP16 with no warning or error - the engine builds, the service
  reports ready, and generated text is coherent, but the engine is ~4x oversized and
  meaningfully slower. This was the actual cause of a memory crisis (shim RSS
  5.9 GB on an 8 GB board, SSH sessions hanging under load) that looked, from the outside,
  like a stable working deployment until someone checked `free -h` or engine file size.
  INT4 quantization must happen as its own pass, via `vendor/quantize_cosmos3_rtn.py`,
  *before* export - see `do_quantize` in `scripts/setup-orin.sh`.
- **`vendor/quantize_cosmos3_rtn.py`'s output metadata format.** It must embed
  `"quantization_config": {"quant_method": "gptq", "group_size": 128}` directly inside the
  quantized checkpoint's own `config.json`. That is the *only* code path
  `tensorrt_edgellm/config.py`'s `_algo_to_quant_type()` recognizes as GPTQ-packed weights.
  Do not have it write a sidecar `hf_quant_config.json` file instead (even with a seemingly
  reasonable `quant_algo` field) - that file's mere *existence* short-circuits detection
  before the parser ever reaches the correct `config.json`-embedded path, silently falling
  back to `QUANT_FP16` and loading the packed INT4 tensors as raw FP16 bytes. This produces a
  checkpoint that builds and serves without any error, with structurally valid API responses,
  but generates complete garbage text (repeated nonsense characters) on every request -
  confirmed via direct ONNX weight-dtype inspection
  (`onnx.load(path, load_external_data=False)`, checking `graph.initializer` dtypes and
  whether `graph.node` op_types are `Int4GroupwiseGemmPlugin` vs plain `MatMul`) as the
  decisive test, since output coherence alone cannot distinguish "quantized correctly" from
  "silently not quantized" or "quantized but undiscoverable." The packing/zero-point math
  itself was correct the whole time; only the metadata was wrong.
- The four `*_token_id` values injected into the raw checkpoint's `config.json` before export
  are copied from this repo's actual deployed engine's own config, not derived from first
  principles. If a future Cosmos3-Edge checkpoint revision changes these, the fix is to
  re-derive them from a real working engine, not to guess.

If you hit a build failure this document doesn't cover, the full step-by-step manual recipe
(with the reasoning behind each fix) is in `docs/source-installation.md`'s "Setup" section - read that before
improvising a workaround, since several of its steps exist because the naive/obvious command
silently produces a broken checkpoint rather than an error.

## Testing changes to this repo's own code (not the Cosmos3-Edge build)

```bash
python3 -m unittest discover -s ui/tests
node --test ui/tests/test_engine_switch.js ui/tests/test_reachy_setup.js
```

No device, external network, GPU, or model is required: fake HTTP backends, robot
responses and DOM elements exercise the flows. Run these after editing `ui/`.

For migration and SD-helper changes, run the corresponding offline checks:

```bash
python3 -m unittest discover -s scripts/tests
python3 -m unittest discover -s scripts/jetpack -p 'test_*.py'
```

The migration tests exercise stage invalidation, host-export receipts, template
validation and the shim's request/timing contract with a fake native runtime. The
SD-helper tests use fixtures and fake devices. None establishes a successful GPU build,
physical card write, boot, caption accuracy or inference performance.

## Repo layout

See the table in `docs/source-installation.md` ("What's here") - it's the same information, kept there so it
has one home instead of two copies drifting apart.
