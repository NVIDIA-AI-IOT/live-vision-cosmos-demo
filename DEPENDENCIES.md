# Component inventory

This source inventory describes the imported application and build recipes. It is not
a complete SBOM of an image that has not yet been built. Inventory the exact completed
image, including base-image packages, transitive Python dependencies, native shared
libraries, codecs, and all retained license/notice files before redistributing it.

| Component | Pin / source | Distribution and license |
|---|---|---|
| Application, source installer and RTN quantizer | `SOURCE.json`, upstream `da489361cee0755f9215cccb7e76757f45728d3f` | Source here; Apache-2.0. Authored integration from the same author's Cosmos/porch-dad work. |
| Live VLM WebUI prompt labels/text | Attribution in `ui/web/app.js`; [upstream](https://github.com/NVIDIA-AI-IOT/live-vlm-webui) | Copied text; Apache-2.0, NVIDIA 2025 notice preserved in NOTICE.md. |
| NVIDIA TensorRT-Edge-LLM | `95515c2f87fba8982db5a519f9022277667b3cc9` (v0.11.0), [source](https://github.com/NVIDIA/TensorRT-Edge-LLM/tree/95515c2f87fba8982db5a519f9022277667b3cc9) | Downloaded during source/image build; Apache-2.0 plus submodule and component licenses retained in that checkout. Native runtime dynamically uses CUDA/TensorRT. |
| Cosmos3-Edge | `344d602b128d1bbdacb43b08d0a3626f46343e29`, [model card](https://huggingface.co/nvidia/Cosmos3-Edge/blob/344d602b128d1bbdacb43b08d0a3626f46343e29/README.md) | Separately downloaded model and derived engines; OpenMDW 1.1. No weights/engines in the source tree or runtime image. |
| JetPack / Jetson Linux | 7.2.1 / 39.2.1 | Host OS and candidate container base; NVIDIA and included package terms. Approved image digest still to be selected. |
| CUDA / TensorRT | 13.2 / 10.16.2.10 | NVIDIA proprietary runtime/development components; their accompanying terms apply. Not relicensed as Apache-2.0. |
| Container Python/native build packages | `container/build-runtime.sh` | pybind11 3.0.4, CUTLASS DSL 4.7.0, CuPy 13.6.0, cuda-python; FastAPI 0.139.2, Uvicorn 0.51.0, requests 2.34.2. The shim imports the native binding directly; SDK server/export extras, including PyAV, are not installed by this image recipe. Image records resolved versions with `pip inspect`. Preserve each package's installed notices. |
| Source installer process utility | Host `util-linux` / `runuser`; installed version not pinned | Invoked as a command to launch the model downloader as the service user. Upstream runuser/su common code is GPL-2.0-or-later; inspect the installed package notices and dependencies in the host/image inventory. Not copied into this source tree. |
| Source-only export/build packages | `scripts/setup-orin.sh`, `scripts/bootstrap.sh`, pinned SDK extras | Export/tools extras add Torch, ONNX, ModelOpt and their dependencies. These are not all installed in the runtime recipe. Resolve and scan on the actual ARM64 build host. |
| Copied Reachy WebRTC source | `reachy/reachy_stream/const.py` and `stream.py`; matching [Pollen Robotics source reference](https://github.com/pollen-robotics/reachy_mini_homeassistant/tree/855c1c09fdd503e15cf1fac7a733ae7918d4784c) | Apache-2.0; Copyright 2026 Pollen Robotics. Upstream LICENSE is preserved in that directory. Historical import revision is unrecorded; source bodies match this reference. Home Assistant is not a runtime dependency. |
| Optional Reachy camera bridge | `reachy/requirements.txt` | Pinned Python dependencies include aiortc, aiohttp, PyAV, cryptography and their closure. Separate source-install environment; excluded from the packaged runtime. PyAV binary wheels carry FFmpeg/codec components requiring their own review. |
| Reachy daemon | [Pollen Robotics Reachy Mini](https://github.com/pollen-robotics/reachy_mini) | Separately running robot software, Apache-2.0 SDK. The included REST client is this application's code. |
| Optional Piper binary | rhasspy/piper v1.2.0 ARM64 | Source installer download only, not in runtime image. Piper MIT; embedded espeak-ng GPL-3.0 and native dependencies require complete binary inventory/source provision review. |
| Optional Piper voice | `en_US-ljspeech-medium`, rhasspy/piper-voices | Source setup pins revision `c10ece1aade47bb51c153c893d14e5bf8e5b7117` and SHA-256 in `scripts/piper-downloads.json`. The repository card lists MIT; the voice card describes its public-domain training dataset. Both cards are retained for review. No voice files included here. |
| Mac SD preparation tools | `scripts/jetpack/release.json`, `docs/jetpack-sd-mac.md` | Downloads NVIDIA BSP/rootfs plus Ubuntu/QEMU/zstd and dependencies for local OS preparation. See NOTICE.md; no generated SD image or third-party binary is distributed here. |

The browser-camera runtime uses HTTPS through Python/OpenSSL, SHA-256 checksums, browser
JPEG capture, and the SDK's image decoder. Optional robot paths add DTLS/SRTP, H.264 and
audio processing. Inventory codecs even when the application does not call them: installed
PyAV/FFmpeg wheels and the base image can include more than the used API surface.

The source website remains MIT-licensed. This application retains its separate Apache-2.0
[LICENSE.md](LICENSE.md) and DCO contribution requirements. Dependency licenses and model terms are separate
from both. Keep review records and security reports outside this public source directory.

## SDK 0.11 and installer dependency details

These descriptions are source-derived inventory evidence. Installed versions and full image/license closure still require validation.

| Component | Pin / source | Distribution and license evidence |
|---|---|---|
| Inja | 3.5.0 base `7d1b4600b68595085a949743331c2e5673f511ea`, modified in the pinned SDK | SDK native template-renderer header, downloaded during build; [MIT, Lars Berscheid](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/95515c2f87fba8982db5a519f9022277667b3cc9/3rdParty/inja/LICENSE). Keep the SDK's modifications and notices. |
| XGrammar | `5b4e9ce9e72524037ae24ecd831b9b6604d2eb48` | Downloaded SDK submodule; [Apache-2.0](https://github.com/mlc-ai/xgrammar/blob/5b4e9ce9e72524037ae24ecd831b9b6604d2eb48/LICENSE). SDK builds its C++ source into `xgrammarCore` and links `edgellmCore`, even when guided output is unused. |
| XGrammar's picojson and DLPack | picojson at XGrammar revision above; DLPack `bbd2f4d32427e548797929af08cfe2a9cbb3cf12` | Native runtime headers: [picojson BSD-2-Clause](https://github.com/mlc-ai/xgrammar/blob/5b4e9ce9e72524037ae24ecd831b9b6604d2eb48/3rdparty/picojson/picojson.h), [DLPack Apache-2.0](https://github.com/dmlc/dlpack/blob/bbd2f4d32427e548797929af08cfe2a9cbb3cf12/LICENSE). Notices remain in downloaded source. |
| XGrammar's optional checkout components | cpptrace `6689d14c203eed390ae7bb64f56a983cfd7dff9c`; googletest `df1544bcee0c7ce35cd5ea0b3eb8cc81855a4140` | Recursive checkout downloads [cpptrace MIT](https://github.com/jeremy-rifkin/cpptrace/blob/6689d14c203eed390ae7bb64f56a983cfd7dff9c/LICENSE) and [googletest BSD-3-Clause](https://github.com/google/googletest/blob/df1544bcee0c7ce35cd5ea0b3eb8cc81855a4140/LICENSE). The SDK native target does not select them; retained SDK source in the image may still include them. |
| Existing SDK submodules | nlohmann/json `55f93686c01528224f448c19128836e7df245f72`; NVTX `2fb879e512ed208f83c6aa4d4c96b958789edf49`; googletest `f132c893119698e10daef8525d0ad7a3f05176f2` | Unchanged from SDK 0.10.1: [nlohmann/json MIT](https://github.com/nlohmann/json/blob/55f93686c01528224f448c19128836e7df245f72/LICENSE.MIT), [NVTX Apache-2.0 with LLVM exception](https://github.com/NVIDIA/NVTX/blob/2fb879e512ed208f83c6aa4d4c96b958789edf49/LICENSE.txt), [googletest BSD-3-Clause](https://github.com/google/googletest/blob/f132c893119698e10daef8525d0ad7a3f05176f2/LICENSE). Inventory retained sources as well as linked binaries. |
| Source tools HTTP client | SDK tools extra: `httpx>=0.27.0,<1` | Newly declared; [BSD-3-Clause registry metadata](https://pypi.org/project/httpx/). Constraint does not identify the installed version. Not added to fixed-engine image recipe. |
| Avahi discovery utilities and daemon | `avahi-utils`, `avahi-daemon`, unpinned apt packages | Source installer prepares robot discovery even if no robot is configured. LGPL-family/component-specific notices; [Ubuntu package copyright reference](https://changelogs.ubuntu.com/changelogs/pool/main/a/avahi/avahi_0.8-13ubuntu6.2/copyright) is not the installed package identity. Inspect installed notices/closure. App invokes `avahi-browse` as a subprocess; no binary is vendored here. |
| SD VM archive/partition tools | `bzip2`, `zstd`, `gdisk`, unpinned apt packages | Newly explicit installation in the disposable Ubuntu 22.04 VM; no binaries/images included here. Jammy references: [bzip2 BSD-variant with GPL-2 Debian packaging](https://changelogs.ubuntu.com/changelogs/pool/main/b/bzip2/bzip2_1.0.8-5ubuntu0.1/copyright), [zstd component-specific BSD/GPL/zlib/MIT](https://changelogs.ubuntu.com/changelogs/pool/main/libz/libzstd/libzstd_1.4.8+dfsg-3build1/copyright), [gdisk LGPL-2.0-or-later](https://changelogs.ubuntu.com/changelogs/pool/main/g/gdisk/gdisk_1.0.8-4build1/copyright). Confirm actual installed package versions and files. |

The exact 0.11 SDK adds `httpx` to tools, removes the explicit `jinja2==3.1.6` server-tools pin, and adds the already used Hugging Face Hub constraint to export/tools. PyTorch 2.13.0 and Transformers 5.14.1 are unchanged between the old and new pinned manifests. Removed direct Jinja2 declaration does not prove the package absent from the transitive environment.

Reachy and Piper remain optional features and excluded from the packaged runtime; source setup now prepares their dependencies even when robot configuration is skipped. The merged source retains the reviewed Piper 1.2.0 checksum, voice revision and checked downloads.
