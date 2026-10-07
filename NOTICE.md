# Notices

This repository's own code (including `scripts/` and the agent skill) is licensed
under Apache License 2.0 - see [LICENSE.md](LICENSE.md). It builds on and talks to the third-party components
below, each under its own license. See [DEPENDENCIES.md](DEPENDENCIES.md) for the source,
runtime, optional robot, and model distribution boundaries.

## NVIDIA Cosmos

The pinned `nvidia/Cosmos3-Edge` revision
`344d602b128d1bbdacb43b08d0a3626f46343e29` lists
[OpenMDW 1.1](https://openmdw.ai/license/1-1/) in its
[model card](https://huggingface.co/nvidia/Cosmos3-Edge/blob/344d602b128d1bbdacb43b08d0a3626f46343e29/README.md).
The model metadata at that revision reports `gated: false`.

**Built on NVIDIA Cosmos.**

This repository does not ship Cosmos model weights or engines. A separately created
engine bundle must preserve the model agreement and applicable notices of origin.
The application Apache-2.0 license does not relicense the model or its derived artifacts.
Changing the model revision requires checking its license and notices again.

## vendor/quantize_cosmos3_rtn.py

Apache License 2.0, by the same author as this repository; vendored here with a
patch. It is not part of NVIDIA's public TensorRT-Edge-LLM SDK, which has no RTN quantizer
at the commit this repo pins. The patch changes how the script records that a
checkpoint is quantized - embedding `quantization_config` directly in the checkpoint's own
`config.json` instead of writing a separate `hf_quant_config.json` sidecar file, so that
TensorRT-Edge-LLM's own checkpoint loader actually recognizes the result as INT4 rather than
silently treating it as FP16 - and the `README.md` it writes into the quantized checkpoint, which
now describes that `config.json` change and says whether the source model card was actually
copied. Neither touches the quantization algorithm or math itself, which was already correct.
See the file's own header comment for the full story of the `config.json` change, and
`do_quantize` in `scripts/setup-orin.sh` for why the pipeline needs this separate quantization
pass at all.

## TensorRT-Edge-LLM 0.11 runtime dependencies

This upgrade branch downloads and builds
[NVIDIA TensorRT-Edge-LLM 0.11.0](https://github.com/NVIDIA/TensorRT-Edge-LLM/tree/95515c2f87fba8982db5a519f9022277667b3cc9),
licensed under Apache-2.0. Its source and native libraries are not vendored into this
application's tracked files. Preserve the SDK's own third-party notices when distributing
an installed environment or compiled runtime.

Compared with the previous SDK pin, the native runtime includes the
[Inja template renderer](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/95515c2f87fba8982db5a519f9022277667b3cc9/3rdParty/inja/LICENSE)
(MIT; Copyright (c) 2018-2025 Lars Berscheid) and
[XGrammar](https://github.com/mlc-ai/xgrammar/blob/5b4e9ce9e72524037ae24ecd831b9b6604d2eb48/LICENSE)
(Apache-2.0, pinned by the SDK). XGrammar is compiled into the runtime even when this
application does not request guided output. These and their bundled dependencies must
be included in the upgraded runtime inventory; the prior SDK inventory is not a complete
inventory of 0.11. This paragraph does not replace those upstream license files.

## Piper (text-to-speech engine)

The `piper` binary itself: MIT License, Copyright (c) 2022 Michael Hansen. Project:
[rhasspy/piper](https://github.com/rhasspy/piper). (Piper has since moved to active development
as [OHF-Voice/piper1-gpl](https://github.com/OHF-Voice/piper1-gpl) under the Open Home Foundation,
GPL-3.0 - confirm which build you are actually running before redistributing it, by license
terms they differ meaningfully.) `scripts/setup-orin.sh` downloads the official prebuilt
`v1.2.0` `aarch64` release binary from
[rhasspy/piper's GitHub releases](https://github.com/rhasspy/piper/releases) at install time;
this repository does not vendor the binary itself.

Piper embeds [espeak-ng](https://github.com/espeak-ng/espeak-ng) (GPL-3.0) for phonemization.

**Voice model:** this repository does not ship a voice model file - the setup steps in
`README.md` download `en_US-ljspeech-medium` at install time, listed under MIT in the pinned repository card, trained from scratch
on the public-domain [LJ Speech Dataset](https://keithito.com/LJ-Speech-Dataset/) (model card on
[Hugging Face](https://huggingface.co/rhasspy/piper-voices/blob/c10ece1aade47bb51c153c893d14e5bf8e5b7117/en/en_US/ljspeech/medium/MODEL_CARD)).
If you substitute a different Piper voice, check its own MODEL_CARD - several published Piper
voices (including ones with "_r" in their name, trained by fine-tuning from another voice) carry
non-commercial-only research licenses inherited from their base voice.

## Reachy Mini WebRTC client

`reachy/reachy_stream/const.py` and `reachy/reachy_stream/stream.py` are copied from
`custom_components/reachy_mini/` in [Pollen Robotics’ Reachy Mini Home Assistant
integration](https://github.com/pollen-robotics/reachy_mini_homeassistant/tree/855c1c09fdd503e15cf1fac7a733ae7918d4784c).
Their source bodies match that reference commit; the historical import commit was not
recorded. Local changes to these two files are limited to license and provenance headers.

Copyright 2026 Pollen Robotics

The copied files are licensed under Apache-2.0. The upstream
[LICENSE](reachy/reachy_stream/LICENSE), including its copyright notice, is retained
verbatim. The full Apache-2.0 agreement is provided in this application’s [LICENSE.md](LICENSE.md).

The local `reachy/reachy_stream/__init__.py` is a standalone wrapper. It avoids loading
Home Assistant, and the demo does not install or require Home Assistant. The copied
WebRTC client is used only by the optional Reachy camera bridge; browser-camera use
does not require the robot or this bridge.

The robot separately runs the [Reachy Mini daemon and SDK](https://github.com/pollen-robotics/reachy_mini), under its own Apache-2.0 license.
`reachy/reachy.py` is this application’s independent REST client for that daemon.
The copied WebRTC files above come from the Home Assistant integration.

## Mac JetPack SD preparation

The Mac orchestration, card writer, validation helpers, and agent skill are original
Apache-2.0 code, adapted from earlier Jetson bring-up scripts by the same author. Python helpers
use only the standard library. No third-party source, OS image, NVIDIA binary, or
model weight is redistributed with this workflow.

Thanks to NVIDIA for Jetson Linux and its image-creation tools, and the QEMU, Ubuntu,
Python, Zstandard, and Homebrew contributors for the host tooling:

| Downloaded or separately installed component | License / acknowledgement |
|---|---|
| NVIDIA JetPack / Jetson Linux BSP and rootfs | NVIDIA's accompanying Jetson software terms and individual component licenses apply. The downloaded `jetson-disk-image-creator.sh` is marked `LicenseRef-NvidiaProprietary`; `l4t_flash_prerequisites.sh` is BSD-3-Clause. The build makes one local performance edit to the image creator's `dd` invocation (4 MiB blocks, flush, progress), preserving its full NVIDIA copyright/license header. No NVIDIA script is vendored here. [Official downloads](https://developer.nvidia.com/embedded/jetpack/downloads). |
| QEMU | [GNU GPL version 2, with component-specific licenses](https://www.qemu.org/docs/master/about/license.html); QEMU is a trademark of Fabrice Bellard. |
| Ubuntu 22.04 cloud image | Canonical and the included package authors; [individual package licenses apply](https://ubuntu.com/legal/intellectual-property-policy). Guest package copyright files are under `/usr/share/doc/*/copyright`. |
| Python | [Python Software Foundation License and included component notices](https://docs.python.org/3/license.html). |
| Zstandard | Meta Platforms, Inc. and affiliates; [BSD-3-Clause](https://github.com/facebook/zstd/blob/dev/LICENSE), alternatively GPL-2.0. |
| Homebrew | Homebrew contributors; [BSD-2-Clause](https://github.com/Homebrew/brew/blob/master/LICENSE.txt). Used to install tools; not bundled. |

Generated images retain the upstream packages and their licenses; the repository's
Apache-2.0 license does not relicense those images or downloaded tools.

## Development

Developed by Alex Sotelo ([@nv-asotelo](https://github.com/nv-asotelo)), with Claude Sonnet/Opus 5
(Anthropic) on the Cosmos3-Edge engine build and deployment, and OpenAI GPT-6 Astra on the Live
Vision web UI - fixes and iteration by both throughout.

## Live VLM WebUI prompt text

`ui/web/app.js` preserves prompt labels and text from
[NVIDIA-AI-IOT/live-vlm-webui](https://github.com/NVIDIA-AI-IOT/live-vlm-webui),
Copyright (c) 2025 NVIDIA Corporation & Affiliates., under Apache-2.0.
The original prompt attribution remains in that file; the Apache-2.0 agreement is
provided in this application's [LICENSE.md](LICENSE.md).

## Imported application and packaged runtime

The source was imported from `nv-asotelo/live-vision-cosmos-demo` at the commit recorded
in SOURCE.json. The application integration and RTN quantizer came through the author’s
`nv-asotelo/porch-dad` work. The copied Reachy WebRTC files originate in Pollen Robotics’
Home Assistant integration, as identified above.
Packaging and compliance changes retain that source's Apache-2.0 license and notices.

The container separately downloads NVIDIA TensorRT-Edge-LLM and its submodules. Their
source, license and notice files remain in the image at `/opt/TensorRT-Edge-LLM`.
Python distributions retain their installed `*.dist-info` licenses; Debian package
copyright files remain under `/usr/share/doc/`. Inventory the completed image before
redistribution, including base-image packages and native shared libraries.

The source installer verifies the Piper archive and voice assets against
`scripts/piper-downloads.json`, including the pinned voice repository revision. It retains
the voice and repository cards plus the upstream Piper, piper-phonemize, espeak-ng and
ONNX Runtime license files with the local installation. These notices do not establish
complete native-binary redistribution compliance; the binary contains additional files
that must be inventoried if it is ever included in a distributed package.

## Tutorial images and animations

The images and silent GIFs in `docs/media/` are excerpts from Alex Sotelo’s
NVIDIA demonstration recorded on October 2, 2026,
`demo-cosmos-orin-reachy-oneshot-Oct2.mp4`. They show the packaged continuation of
the demo presented in the September 30, 2026 livestream
[“A New Way to Build Edge AI: Agentic Development on NVIDIA Jetson”](https://www.youtube.com/live/OF_ryVZmB3w?t=1927).
The source recording has SHA-256
`9b380c780564e648dd97628dcf5a43a90d17bcd12d5ce55743115ab170f03b1b`.

The excerpts retain the recorded captions and timing. Crops remove browser chrome,
local addresses and private document details; the GIFs omit audio. Their recorded
measurements describe that demonstration, rather than a benchmark of this release.
