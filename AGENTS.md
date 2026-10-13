# Working on Live Vision

This demo runs Cosmos3-Edge locally on Jetson Orin Nano Super and serves a browser
camera UI. Reachy Mini camera and Flite speech are optional. Read [README.md](README.md)
for the user workflow and [the source guide](docs/source-installation.md) for setup.

## Setup

Use an existing JetPack 7.2.1 / L4T 39.2.1 installation. If the user needs an SD
card, follow [the Mac preparation guide](docs/jetpack-sd-mac.md) and the
[flash skill](.agents/skills/flash-jetpack-sd-mac/SKILL.md).

Verify the board and its SSH address with the user, then run from the local checkout:

```sh
bash scripts/bootstrap.sh jetson-user@orin-ip
```

The installer downloads dependencies and the model, quantizes and exports it,
builds the engine on the Jetson, and starts the shim and UI services. The host
computer needs no GPU. Enter passwords locally; keep credentials and device
configuration out of Git. Do not reflash a working board or run the full installer
for a UI-only change. The installer prepares optional robot dependencies and asks
whether to configure a robot; skipping it leaves browser-camera operation available.

Use the [first-boot guide](docs/first-boot-recovery.md) for account/boot recovery
and the [network guide](docs/network-switching.md) when changing connections.
Do not guess credentials, probe old addresses, or change another device's SSH key.

## Build configuration

- [NVIDIA TensorRT-Edge-LLM SDK v0.10.0](https://github.com/NVIDIA/TensorRT-Edge-LLM/tree/71dd1bae032e70771265917ec74d3ff4cad07a10): `71dd1bae032e70771265917ec74d3ff4cad07a10`.
- [Cosmos3-Edge](https://huggingface.co/nvidia/Cosmos3-Edge/tree/344d602b128d1bbdacb43b08d0a3626f46343e29): `344d602b128d1bbdacb43b08d0a3626f46343e29`, OpenMDW 1.1. This revision needs no Hugging Face token.
- CUDA 13.2 and TensorRT 10.16.2.10; exact compatibility is in [runtime-spec.json](container/runtime-spec.json).
- On-device INT4 export uses plugin V1. The optional `--host-quantize` path needs
  Linux and at least 12 GiB available RAM and uses V2. Do not force V2 on an 8 GB Orin.
- Native compilation uses one worker. Engine limits are batch 1, input length 1024,
  KV capacity 1024 and 8 KV pages. Raising them requires memory and inference checks.
- Keep the separate RTN quantization pass. The SDK's `--quantization int4_awq` path
  does not quantize this dense model. Store GPTQ-compatible metadata inside
  `config.json`; do not create an `hf_quant_config.json` sidecar.
- Preserve `scripts/fix_cosmos_visual_layout.py`: the selected SDK exporter needs
  its guarded HWC-to-CHW weight-layout correction. A model or SDK change requires fresh
  export/build and image-caption checks. Do not reuse engines across SDK versions.

## Runtime changes

Preserve `/v1/chat/completions` and `/health/ready`. Keep browser-camera operation
independent of Reachy. Robot setup uses the shared settings manager; see
[Reachy setup](docs/reachy-setup.md). Do not discover robots by sweeping a subnet.
Changing a robot must not restart the inference engine or move the robot.

Flite uses the built-in SLT voice and a bounded child process per utterance. Keep
its source pin and original notices. The optional bridge builds shared LGPL
FFmpeg/LAME and PyAV; do not replace them with a prebuilt PyAV wheel containing
additional codecs. [NOTICE.md](NOTICE.md) identifies third-party terms.

## Validation

Run tests relevant to the files changed:

```sh
python3 -m unittest discover -s ui/tests
node --test ui/tests/test_engine_switch.js ui/tests/test_reachy_setup.js
python3 -m unittest discover -s scripts/tests
python3 -m unittest discover -s container/tests
python3 -m unittest discover -s tests
python3 -m unittest discover -s scripts/jetpack -p 'test_*.py'
```

The quantizer tests need NumPy. Offline tests use fixtures and do not require a
robot or GPU. For deployment changes, check readiness, submit a real text request,
then compare captions against known images and changing camera frames. Readiness
alone does not establish correct inference. Keep engine binaries, weights, TLS
keys, credentials and local reports outside the source archive.
