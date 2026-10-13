#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Laptop-side bootstrap: copies this repo to a Jetson Orin over SSH and runs the Orin-side
# build (scripts/setup-orin.sh) there. By default this runs entirely on the laptop's CPU -
# no GPU, no particular OS, and no local Python/CUDA/build toolchain are needed. Everything
# that needs a GPU or a big download happens on the Orin itself, over SSH.
#
# Optional --host-quantize uses the LAPTOP's own CPU and RAM to do the LLM text-tower
# quantize+export step instead of the Orin - see "Host-accelerated quantization" below and
# in docs/source-installation.md. This is the only part of the pipeline that benefits from running off-device:
# it's RAM-bound (an 8 GB Orin Nano gets OOM-killed exporting with the SDK's primary
# V2/cuteDSL plugin), not GPU-bound. The model is quantized and exported on CPU either way -
# a GPU on the host is not required and is not used for this step; any machine with enough
# free RAM gets the better result.
#
# Requires only: bash, ssh, scp, tar (all present by default on macOS, Linux, and Windows
# via WSL or Git Bash/OpenSSH). --host-quantize additionally requires python3, git, rsync,
# and a Linux host with enough free RAM - see its own preflight checks below for exactly why.
#
# Usage:
#   export REACHY_MINI_IP=192.0.2.50        # optional - omit if you have no Reachy Mini
#   ./scripts/bootstrap.sh jetson@192.0.2.100
#   ./scripts/bootstrap.sh --host-quantize jetson@192.0.2.100     # better engine, Linux host only
#
# The Orin needs JetPack already flashed and booting, SSH reachable, and an account with
# sudo - you'll be asked for its password twice (copying the repo, then setup itself); an
# unattended run needs passwordless sudo instead. Everything else - cloning
# TensorRT-Edge-LLM, downloading and quantizing the checkpoint, building the engine,
# installing Flite/systemd - happens on the Orin itself, unless --host-quantize moved the
# quantize+export step here.
set -euo pipefail

# Must match EDGELLM_COMMIT / COSMOS_MODEL_REPO / COSMOS_MODEL_REVISION in setup-orin.sh
# exactly - --host-quantize exports against the same pinned SDK commit and checkpoint
# revision the Orin itself builds against, so the ONNX this produces is interchangeable
# with what do_export_onnx_llm would have produced on-device.
EDGELLM_COMMIT="71dd1bae032e70771265917ec74d3ff4cad07a10"   # tag v0.10.0
COSMOS_MODEL_REPO="nvidia/Cosmos3-Edge"
COSMOS_MODEL_REVISION="344d602b128d1bbdacb43b08d0a3626f46343e29"
# The source host-export preflight requires 12 GiB available RAM. Lower-memory hosts
# fall back to the on-Orin V1 path. Validate the final artifacts on the target device.
HOST_QUANTIZE_MIN_RAM_GB=12

HOST_QUANTIZE=0
HF_TOKEN="${HF_TOKEN:-}"
TARGET=""
for arg in "$@"; do
  case "$arg" in
    --host-quantize) HOST_QUANTIZE=1 ;;
    -*) echo "unknown flag: $arg" >&2; exit 2 ;;
    *) TARGET="$arg" ;;
  esac
done
# Fixed, not configurable: setup-orin.sh, the systemd units and the shim all hardcode it.
INSTALL_DIR=/opt/live-vision-cosmos-demo
SETUP_ORIN_USER="${SETUP_ORIN_USER:-}"   # defaults to the ssh login user, remote-side
[[ "${REACHY_SETUP_SKIP:-0}" == 0 || "${REACHY_SETUP_SKIP:-0}" == 1 ]] \
  || { echo 'REACHY_SETUP_SKIP must be 0 or 1.' >&2; exit 2; }

if [[ -z "$TARGET" ]]; then
  cat >&2 <<'USAGE'
Usage: ./scripts/bootstrap.sh [--host-quantize] user@orin-host-or-ip

JetPack 7.2.1 / L4T 39.2.1 must already boot on the Orin. Keep an existing
compatible installation. For a fresh SD card from a Mac, first follow
docs/jetpack-sd-mac.md or run scripts/flash-jetpack-sd-mac.sh --help.

Optional env vars:
  HF_TOKEN          Not needed: nvidia/Cosmos3-Edge is public and not gated on Hugging
                    Face. If you set one anyway, the download uses it.
  REACHY_MINI_IP    IP or hostname of a Reachy Mini robot on the same LAN. Omit for
                    discovery/manual/skip during interactive setup, or configure later in
                    the UI's Reachy settings. No model rebuild is needed.
  REACHY_SETUP_SKIP Set to 1 to explicitly skip robot connection; dependencies remain ready
                    for later configuration in the UI.
  SETUP_ORIN_USER   Account setup-orin.sh should own the install and run services as.
                    Default: whichever account you SSH in as.

Optional flag:
  --host-quantize   Do the LLM quantize+export step on THIS machine instead of the Orin,
                    using the SDK's primary V2/cuteDSL plugin instead of its legacy V1
                    fallback. Requires Linux (not macOS - NVIDIA's cuda-python has no macOS
                    wheel, any RAM amount) with at least 12 GB available RAM (the export
                    peaks near 9 GB). No GPU needed on this machine - the step is RAM-bound, not
                    GPU-bound. Without this flag, or on a host that doesn't qualify, the
                    Orin does this step itself and the result uses
                    the legacy plugin - see docs/source-installation.md "Host-accelerated quantization".
USAGE
  exit 2
fi

command -v ssh >/dev/null || { echo "ssh not found on this laptop - install an OpenSSH client." >&2; exit 2; }
command -v scp >/dev/null || { echo "scp not found on this laptop - install an OpenSSH client." >&2; exit 2; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$REPO_ROOT/scripts/setup-orin.sh" ]] || { echo "internal error: $REPO_ROOT doesn't look like this repo (scripts/setup-orin.sh missing)." >&2; exit 2; }
if [[ -n "${REACHY_MINI_IP:-}" ]]; then
  source "$REPO_ROOT/scripts/reachy-address.sh"
  reachy_address_valid "$REACHY_MINI_IP" \
    || { echo 'REACHY_MINI_IP must be an IP address or hostname, without quotes, scheme, port or path.' >&2; exit 2; }
fi

# ---------------------------------------------------------------------------------------
# Host-accelerated quantization (--host-quantize only)
# ---------------------------------------------------------------------------------------
# Does the LLM text-tower quantize+export on THIS machine, then rsyncs the result to a staging
# directory beside the Orin's $ONNX_DIR/llm (llm.host) and flags it complete. setup-orin.sh,
# already running as root, adopts it (adopt_host_export): swaps it in, marks its own quantize
# and export_onnx_llm stages done, and clears the stages built on top of the old export, so
# the engine is rebuilt from it. That works the same on a fresh Orin and on one already set up
# (an upgrade from the V1 build), needs no extra sudo over SSH, and a copy that never finished
# is never adopted. The visual tower always still exports on the Orin; it was never the stage
# that OOMs.
#
# Why this check order (OS, then RAM): OS is a hard, unconditional disqualifier regardless
# of RAM - NVIDIA's cuda-bindings package (a base, non-optional dependency of
# tensorrt-edgellm itself) publishes manylinux and Windows wheels only, no macOS build at
# any Python version. A Mac with 128 GB of RAM still cannot install it. RAM is the real gate
# only once OS has already passed.
host_quantize_preflight() {
  local os; os="$(uname -s)"
  if [[ "$os" != Linux ]]; then
    cat >&2 <<EOF
--host-quantize requires Linux: this machine reports '$os'.

NVIDIA's cuda-bindings package (a base dependency of tensorrt-edgellm, pulled in even just
to export a checkpoint) ships manylinux and Windows wheels only - there is no macOS build
at any Python version, for any Mac, regardless of how much RAM it has. This is a hard
platform limit, not a resource one: a MacBook Air and a maxed-out Mac Studio are equally
unable to run this step.

Omit --host-quantize to use the standard on-Orin path - it uses
the SDK's legacy V1 INT4 GEMM plugin instead of its primary V2/cuteDSL one. See docs/source-installation.md
"Host-accelerated quantization" for what that tradeoff actually is. If you have a Linux
machine available (a desktop, a cloud VM, WSL on Windows), re-run bootstrap.sh with
--host-quantize from there instead.
EOF
    return 1
  fi
  local avail_kb avail_gb
  avail_kb="$(awk '/MemAvailable:/{print $2}' /proc/meminfo 2>/dev/null || echo 0)"
  avail_gb=$((avail_kb / 1024 / 1024))
  if [[ "$avail_gb" -lt "$HOST_QUANTIZE_MIN_RAM_GB" ]]; then
    cat >&2 <<EOF
--host-quantize needs at least ${HOST_QUANTIZE_MIN_RAM_GB} GB of free RAM on this machine;
only ${avail_gb} GB is currently available.

This step is RAM-bound, not GPU-bound - the same V2/cuteDSL export that OOM-kills an 8 GB
Orin Nano peaks near 9 GB, on whichever machine runs it. Light-RAM laptops hit this same
ceiling: an 8 GB-class machine (the RAM tier some MacBook Air configurations ship with,
were macOS not already excluded above) never clears this bar, and a 16 GB one only with
most of its memory free. Close other applications and retry, or use a machine with more RAM.

Omit --host-quantize to use the standard on-Orin path - it uses
the SDK's legacy V1 INT4 GEMM plugin instead of its primary V2/cuteDSL one. See docs/source-installation.md
"Host-accelerated quantization" for what that tradeoff actually is.
EOF
    return 1
  fi
  command -v python3 >/dev/null || { echo "--host-quantize requires python3 on this machine." >&2; return 1; }
  command -v git >/dev/null || { echo "--host-quantize requires git on this machine." >&2; return 1; }
  command -v rsync >/dev/null || { echo "--host-quantize requires rsync on this machine." >&2; return 1; }
  # Soft check only: cuda-bindings' actual import-time requirements beyond "a Linux wheel
  # exists" (e.g. whether an NVIDIA driver must be present at all, even though the export
  # itself runs on CPU) were not exhaustively verified. nvidia-smi's absence is a strong
  # signal worth surfacing, not a confirmed hard failure - so this warns and continues
  # rather than aborting the whole bootstrap over an unconfirmed requirement.
  command -v nvidia-smi >/dev/null || echo "!! nvidia-smi not found - continuing, but if --host-quantize fails to even import tensorrt_edgellm, this is the first thing to check (an NVIDIA driver may be required even though no GPU compute happens here)." >&2
  echo "    ok: Linux, ${avail_gb} GB free RAM, python3/git/rsync present"
}

run_host_quantize() {
  echo "==> --host-quantize: doing the LLM quantize+export step on this machine (V2/cuteDSL plugin)"
  host_quantize_preflight || { echo "!! --host-quantize preflight failed - falling back to the standard on-Orin path (legacy V1 plugin)." >&2; return 1; }

  # Inside $REPO_ROOT, not /tmp: vendor/quantize_cosmos3_rtn.py requires --output to resolve
  # under its own project root (one level up from vendor/, i.e. wherever the script itself
  # lives) and refuses an external path with "Output must be a separate new directory inside
  # this project". Cleaned up below regardless of outcome - never left in the working tree.
  local scratch; scratch="$(mktemp -d -p "$REPO_ROOT")"
  trap 'rm -rf "$scratch" "$REPO_ROOT/results"' RETURN

  echo "    cloning TensorRT-Edge-LLM @ $EDGELLM_COMMIT"
  git -c credential.helper= clone --quiet --depth 1 https://github.com/NVIDIA/TensorRT-Edge-LLM.git "$scratch/edgellm" \
    || { echo "!! clone failed - falling back to the standard on-Orin path." >&2; return 1; }
  git -c credential.helper= -C "$scratch/edgellm" fetch --quiet --depth 1 origin "$EDGELLM_COMMIT" \
    && git -C "$scratch/edgellm" checkout --quiet --detach "$EDGELLM_COMMIT" \
    || { echo "!! checkout of the pinned commit failed - falling back to the standard on-Orin path." >&2; return 1; }
  # pyproject.toml's license-files glob matches 3rdParty/nlohmannJson/LICENSE.MIT - pip's
  # metadata build fails outright ("did not match any") without the submodules checked out,
  # even though the export path never uses their contents otherwise.
  git -c credential.helper= -C "$scratch/edgellm" submodule update --quiet --init --recursive \
    || { echo "!! submodule checkout failed - falling back to the standard on-Orin path." >&2; return 1; }

  echo "    creating a local venv and installing the tools extra (no native build, no GPU needed)"
  python3 -m venv "$scratch/venv"
  local py="$scratch/venv/bin/python"
  "$py" -m pip install --quiet --upgrade pip
  "$py" -m pip install --quiet -e "$scratch/edgellm[tools]" \
    || { echo "!! pip install failed - falling back to the standard on-Orin path. If this failed importing cuda-bindings, an NVIDIA driver may be required on this host even for the CPU-only export path." >&2; return 1; }

  echo "    downloading the raw checkpoint from Hugging Face (same files setup-orin.sh would fetch)"
  local raw="$scratch/raw" quant="$scratch/quantized" onnx="$scratch/onnx"
  mkdir -p "$raw"
  HF_TOKEN="${HF_TOKEN:-}" "$py" - "$raw" <<PY || { echo "!! checkpoint download failed - falling back to the standard on-Orin path." >&2; return 1; }
import json, os, sys
from pathlib import Path
from huggingface_hub import hf_hub_download, snapshot_download

target = Path(sys.argv[1])
repo = "$COSMOS_MODEL_REPO"
revision = "$COSMOS_MODEL_REVISION"
token = os.environ.get("HF_TOKEN") or None   # not needed - the model is public


def fetch():
    index_path = hf_hub_download(repo, "model.safetensors.index.json", revision=revision, token=token)
    shards = sorted(set(json.loads(Path(index_path).read_text())["weight_map"].values()))
    allow = [
        "config.json", "model.safetensors.index.json",
        "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
        "chat_template.jinja", "generation_config.json",
        "README.md",   # the model card - see do_download_checkpoint in setup-orin.sh
        "preprocessor_config.json", "processor_config.json",
        "vision_encoder/*",
        *shards,
    ]
    snapshot_download(repo, revision=revision, local_dir=str(target), token=token, allow_patterns=allow)


try:
    fetch()
except Exception as e:
    status = getattr(getattr(e, "response", None), "status_code", None)
    if status in (401, 403) or "Gated" in type(e).__name__:
        sys.exit(f"Hugging Face refused the download (HTTP {status}). {repo} needed no token when "
                 f"this was written, so it has probably been gated since: accept its terms at "
                 f"https://huggingface.co/{repo} and re-run with HF_TOKEN set to a read token.")
    raise
PY

  # Same four multimodal token IDs do_fix_raw_config patches on-Orin - keep in sync with it.
  "$py" - "$raw/config.json" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
cfg = json.loads(p.read_text())
needed = {"image_token_id": 19, "video_token_id": 18, "vision_start_token_id": 20, "vision_end_token_id": 21}
cfg.update({k: v for k, v in needed.items() if k not in cfg})
p.write_text(json.dumps(cfg, indent=2))
PY

  # vendor/quantize_cosmos3_rtn.py's source_catalog() AND its own pre-conversion
  # re-verification (right before --apply touches any weight data) both require a "verified
  # official model-download receipt" at $REPO_ROOT/results/model-download.json - not just for
  # config.json/the index, but a sha256 for EVERY top-level metadata file it may copy through
  # (tokenizer/chat-template/preprocessor configs etc - see its own "copy everything else
  # through unchanged" loop) and BOTH a size_bytes AND a sha256 for every shard (two separate
  # checks at two separate points, not redundant). Discovered the hard way by hitting each
  # missing field as a separate failure in turn - keep this in sync with do_fix_raw_config's
  # copy of it in setup-orin.sh. Generated here, after the token-ID patch above, so the
  # config.json hash matches what's on disk now, not the as-downloaded file.
  # $REPO_ROOT/results itself is removed by this function's cleanup trap, not left in the tree.
  "$py" - "$raw" "$REPO_ROOT/results/model-download.json" "$COSMOS_MODEL_REVISION" <<'PY'
import hashlib, json, sys
from pathlib import Path
source, out, revision = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]

def sha256_of(p):
    digest = hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda: f.read(4 << 20), b""):
            digest.update(block)
    return digest.hexdigest()

index = json.loads((source / "model.safetensors.index.json").read_text())["weight_map"]
shards = sorted(set(index.values()))
files = {}
for p in sorted(source.glob("*")):
    if p.is_file() and p.suffix in {".json", ".jinja", ".md"}:
        files[p.name] = {"sha256": sha256_of(p)}
for shard in shards:
    p = source / shard
    files[shard] = {"size_bytes": p.stat().st_size, "sha256": sha256_of(p)}
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({"revision": revision, "complete": True, "files": files}, indent=2))
PY

  echo "    quantizing (CPU, several minutes)"
  "$py" "$REPO_ROOT/vendor/quantize_cosmos3_rtn.py" --source "$raw" --output "$quant" \
    --quantization-scope all-linears --apply \
    || { echo "!! quantization failed - falling back to the standard on-Orin path." >&2; return 1; }

  echo "    exporting to ONNX with the primary V2/cuteDSL plugin"
  mkdir -p "$onnx"
  "$scratch/venv/bin/tensorrt-edgellm-export" "$quant" "$onnx" \
    --task reasoning --skip-visual --int4-gemm-plugin-version 2 \
    || { echo "!! V2 export failed on this host too - falling back to the standard on-Orin path (legacy V1 plugin)." >&2; return 1; }
  [[ -f "$onnx/llm/model.onnx" ]] || { echo "!! export reported success but $onnx/llm/model.onnx is missing - falling back to the standard on-Orin path." >&2; return 1; }
  [[ -s "$onnx/llm/processed_chat_template.json" ]] \
    || { echo "!! Export lacks the processed chat template - refusing this export." >&2; return 1; }
  printf '%s\n' "$EDGELLM_COMMIT" > "$onnx/llm/.edgellm-commit"

  # Staged beside the live export, never over it, and flagged complete only once the copy has
  # finished: setup-orin.sh adopts it only with the flag (see adopt_host_export there).
  local staged="$INSTALL_DIR/checkpoints/Cosmos3-Edge/onnx/reasoning/llm.host"
  echo "    copying the result to $TARGET for setup-orin.sh to adopt"
  ssh "$TARGET" "rm -rf '$staged' && mkdir -p '$staged'" \
    || { echo "!! could not prepare the remote directory - falling back to the standard on-Orin path." >&2; return 1; }
  rsync -a --delete "$onnx/llm/" "$TARGET:$staged/" \
    || { echo "!! rsync of the exported ONNX failed - falling back to the standard on-Orin path." >&2; return 1; }
  ssh "$TARGET" "touch '$staged/.complete'" \
    || { echo "!! could not flag the copy complete - falling back to the standard on-Orin path." >&2; return 1; }

  echo "==> --host-quantize done: the Orin will use this V2/cuteDSL export instead of quantizing and exporting it locally."
  return 0
}

echo "==> checking SSH reachability and target architecture: $TARGET"
REMOTE_UNAME="$(ssh -o ConnectTimeout=10 "$TARGET" 'uname -sm')" \
  || { echo "could not SSH to $TARGET - check the host/IP, that the Orin is powered on and on the network, and your credentials." >&2; exit 1; }
[[ "$REMOTE_UNAME" == "Linux aarch64" ]] \
  || { echo "$TARGET reports '$REMOTE_UNAME' - this needs to be run against the Jetson Orin (Linux aarch64), not a laptop or a different device." >&2; exit 1; }
echo "    ok: $REMOTE_UNAME"
REMOTE_L4T="$(ssh "$TARGET" "dpkg-query -W -f='\${Version}' nvidia-l4t-core 2>/dev/null" || true)"
[[ "$REMOTE_L4T" == 39.2.1-* ]] \
  || { echo "JetPack 7.2.1 / L4T 39.2.1 is required before demo setup. See docs/jetpack-sd-mac.md for optional Mac SD preparation; existing compatible systems do not need reflashing." >&2; exit 1; }

echo "==> copying this repo to $TARGET:$INSTALL_DIR"
# -t so sudo can ask for the account's password; without a terminal it can't, and a stock
# JetPack account's sudo needs one. (Unattended runs need passwordless sudo either way.)
ssh -t "$TARGET" "sudo mkdir -p '$INSTALL_DIR' && sudo chown \$(whoami) '$INSTALL_DIR'"
# Transfer only the committed application subtree.
bash "$REPO_ROOT/scripts/archive-source.sh" | ssh "$TARGET" "tar -x -C '$INSTALL_DIR'"

if [[ "$HOST_QUANTIZE" -eq 1 ]]; then
  run_host_quantize || echo "!! continuing with the standard on-Orin path - the engine uses the SDK's legacy V1 INT4 GEMM plugin. See docs/source-installation.md \"Host-accelerated quantization\"." >&2
else
  cat >&2 <<EOF
==> Using the standard on-Orin quantize+export path (legacy V1 INT4 GEMM plugin).
    Validate its output before use. For the SDK's primary V2/cuteDSL plugin instead, re-run with
    --host-quantize from a Linux machine with ${HOST_QUANTIZE_MIN_RAM_GB}+ GB free RAM
    (macOS cannot run this step at any RAM size - see docs/source-installation.md "Host-accelerated
    quantization" for why, and what the difference actually is).
EOF
fi

echo "==> running scripts/setup-orin.sh on $TARGET (a first run takes well over an hour on an Orin Nano - checkpoint download plus an on-device compiler build; a re-run skips the stages already done)"
REMOTE_USER="${SETUP_ORIN_USER:-$(ssh "$TARGET" whoami)}"
# Transfer optional authentication through encrypted stdin into a mode-0600 temporary
# file. Never interpolate a token into SSH's command arguments or terminal output.
REMOTE_ENV="$(ssh "$TARGET" 'umask 077; mktemp /tmp/cosmos-env.XXXXXXXXXX')"
[[ "$REMOTE_ENV" =~ ^/tmp/cosmos-env\.[a-zA-Z0-9]+$ ]] || { echo 'Invalid remote temporary path' >&2; exit 1; }
cleanup_remote_env() { ssh "$TARGET" "rm -f '$REMOTE_ENV'" >/dev/null 2>&1 || true; }
trap cleanup_remote_env EXIT
{
  printf 'export HF_TOKEN=%q\n' "$HF_TOKEN"
  printf 'export REACHY_MINI_IP=%q\n' "${REACHY_MINI_IP:-}"
  printf 'export REACHY_SETUP_SKIP=%q\n' "${REACHY_SETUP_SKIP:-0}"
  printf 'export SERVICE_USER=%q\n' "$REMOTE_USER"
  printf 'exec bash %q\n' "$INSTALL_DIR/scripts/setup-orin.sh"
} | ssh "$TARGET" "cat > '$REMOTE_ENV'"
# Open then unlink the file before reading it, so no credential file survives setup.
ssh -t "$TARGET" "sudo bash -c 'exec 3<\"$REMOTE_ENV\"; rm -f \"$REMOTE_ENV\"; source /dev/fd/3'"

echo "==> done. See the URL setup-orin.sh printed above, or open https://${TARGET#*@}:8443/ using the Orin's actual IP (not the SSH hostname, if those differ)."
