# Notices

This application's code, installer and agent helpers are licensed under
[Apache License 2.0](LICENSE.md). Third-party components and model artifacts retain
their own terms. [SOURCE.json](SOURCE.json) records source provenance; build recipes
record dependency pins. Downloaded binaries, model weights and engines are not
included in this source repository.

## Copied source and prompt text

### Reachy Mini WebRTC client

`reachy/reachy_stream/const.py` and `stream.py` are copied from Pollen Robotics'
[Reachy Mini Home Assistant integration](https://github.com/pollen-robotics/reachy_mini_homeassistant/tree/855c1c09fdd503e15cf1fac7a733ae7918d4784c).
Their source bodies match that reference; the original import revision was not
recorded. Local changes are license and provenance headers.

Copyright 2026 Pollen Robotics

The original Apache-2.0 [LICENSE](reachy/reachy_stream/LICENSE), including its
copyright notice, is retained verbatim. The local wrapper uses this client for the
optional camera bridge without installing or requiring Home Assistant. The robot
separately runs the [Reachy Mini daemon and SDK](https://github.com/pollen-robotics/reachy_mini)
under its own Apache-2.0 license.

### Live VLM WebUI prompts

`ui/web/app.js` retains prompt labels and text from
[NVIDIA-AI-IOT/live-vlm-webui](https://github.com/NVIDIA-AI-IOT/live-vlm-webui),
Copyright (c) 2025 NVIDIA Corporation & Affiliates., under Apache-2.0.
The attribution remains in that file.

### RTN quantizer

`vendor/quantize_cosmos3_rtn.py` is Apache-2.0 code by the same author as this
application. Its local modifications store quantization metadata in `config.json`
and describe that format in generated checkpoint documentation. Original notices
remain in the file. The quantizer is separate from TensorRT-Edge-LLM.

## NVIDIA inference and model components

[NVIDIA TensorRT-Edge-LLM v0.11.0](https://github.com/NVIDIA/TensorRT-Edge-LLM/tree/95515c2f87fba8982db5a519f9022277667b3cc9)
is the software development kit (SDK) used for model export, engine building and
native inference. Setup downloads it at commit
`95515c2f87fba8982db5a519f9022277667b3cc9`. Its Apache-2.0 license and all component
notices remain in the downloaded checkout, including:

| Component | License / attribution |
|---|---|
| Inja | MIT; Copyright (c) 2018-2025 Lars Berscheid. The SDK retains its modified source and license. |
| XGrammar and DLPack | Apache-2.0; respective upstream notices remain in the SDK checkout. |
| picojson | BSD-2-Clause. |
| nlohmann/json and cpptrace | MIT. |
| NVTX | Apache-2.0 with LLVM exception. |
| googletest | BSD-3-Clause. |

The SDK's export/tools extras install Torch, ONNX, ModelOpt and related Python
packages. Their installed licenses apply independently: audio dependencies include
LGPL libsndfile and SoXR; NumPy/SciPy wheels include BSD components and GCC runtimes
with the GCC Runtime Library Exception; tqdm includes MPL-2.0 and MIT terms.
Preserve complete package notices and applicable exception text. The source
installer excludes unused SDK video-server extras. The fixed-engine container
uses a smaller dependency set, declared in `container/build-runtime.sh`.

CUDA 13.2, TensorRT 10.16.2.10, and JetPack / Jetson Linux have accompanying NVIDIA
and component terms; this application's license does not replace them. Container
builds retain the SDK source and notices at `/opt/TensorRT-Edge-LLM`, Python package
license files, and system package copyright files under `/usr/share/doc/`.
A compiled image requires its own complete package inventory and any applicable
source and replacement/relinking materials.

### NVIDIA Cosmos

**Built on NVIDIA Cosmos.** The pinned
[Cosmos3-Edge model revision](https://huggingface.co/nvidia/Cosmos3-Edge/blob/344d602b128d1bbdacb43b08d0a3626f46343e29/README.md)
`344d602b128d1bbdacb43b08d0a3626f46343e29` specifies
[OpenMDW 1.1](https://openmdw.ai/license/1-1/). Separately created engine bundles
must preserve applicable model terms and notices of origin. The application's
Apache-2.0 license does not relicense the model or derived artifacts.

## Flite speech and SLT voice

Setup builds [Flite 2.2](https://github.com/festvox/flite/tree/e9e2e37c329dbe98bfeb27a1828ef9a71fa84f88)
from pinned, checksum-verified source, selecting the bundled `cmu_us_slt` voice,
`usenglish` language and `cmulex` lexicon. No separate voice weights are downloaded.

Flite uses permissive CMU terms with component-specific notices and exceptions.
Its original [COPYING](licenses/Flite-COPYING) is retained verbatim; the SLT files
are covered by it and are not listed as an exception. Preserve that full text and
individual source notices with builds. Native system libraries keep their own terms.
See [speech setup and rebuilding](docs/source-installation.md#3-set-up-text-to-speech).

## Optional Reachy media libraries

The bridge uses aiortc 1.10.1 (BSD-3-Clause) and builds PyAV 13.1.0 (BSD-3-Clause)
against shared FFmpeg 7.1.5 (LGPL-2.1-or-later) and LAME 3.100 (LGPL-2.0-or-later).
The selected FFmpeg build disables GPL, nonfree and version-3 components, automatic
optional-library detection, and x264/x265 encoders.

[Source URLs and checksums](scripts/reachy-media-sources.json) identify the build
inputs. Original source archives, licenses and build configuration are retained
locally. Redistribution of compiled LGPL libraries requires applicable notices,
corresponding source and replacement/relinking provisions. See the
[media build and library replacement guide](docs/reachy-media.md).

## Mac JetPack SD preparation

The Mac orchestration and card-writing helpers are original Apache-2.0 code.
They download tools and OS images for local preparation; none is bundled here.

| Separately installed component | License / acknowledgement |
|---|---|
| NVIDIA JetPack / Jetson Linux BSP and rootfs | Accompanying NVIDIA and component terms. The downloaded image creator uses NVIDIA proprietary terms; its local performance edit preserves the original header. The prerequisite script is BSD-3-Clause. [Official downloads](https://developer.nvidia.com/embedded/jetpack/downloads). |
| QEMU | [GPL version 2 and component-specific licenses](https://www.qemu.org/docs/master/about/license.html); QEMU is a trademark of Fabrice Bellard. |
| Ubuntu 22.04 cloud image and build utilities | Canonical and package authors; [individual package terms](https://ubuntu.com/legal/intellectual-property-policy). Compiler tools and runuser include GPL terms; compiler runtimes may carry exceptions. Package notices remain under `/usr/share/doc/`. |
| Python | [Python Software Foundation License and included component notices](https://docs.python.org/3/license.html). |
| Zstandard | Meta Platforms, Inc. and affiliates; [BSD-3-Clause](https://github.com/facebook/zstd/blob/dev/LICENSE), alternatively GPL-2.0. |
| Homebrew | Homebrew contributors; [BSD-2-Clause](https://github.com/Homebrew/brew/blob/master/LICENSE.txt). |

Generated OS images retain upstream package licenses. The repository's license
does not relicense those images or downloaded utilities.

## Tutorial images and animations

The images and silent GIFs in `docs/media/` are excerpts from Alex Sotelo's
NVIDIA demonstration recorded on October 2, 2026,
`demo-cosmos-orin-reachy-oneshot-Oct2.mp4`. They show the packaged continuation of
the demo presented in the September 30, 2026 livestream
[“A New Way to Build Edge AI: Agentic Development on NVIDIA Jetson”](https://www.youtube.com/live/OF_ryVZmB3w?t=1927).
[The media manifest](docs/media/manifest.json) records their source and timestamps.
Recorded measurements describe that demonstration.
