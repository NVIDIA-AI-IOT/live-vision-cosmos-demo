# TensorRT-Edge-LLM 0.11: recommendation for Live Vision

Reviewed 2026-10-05. Baseline: demo `e84cd37783a19b8be1889fa20a191abbee9d8c1f`,
TensorRT-Edge-LLM 0.10.1 `e8b29522938901f6df19ebeedd4b69bc8edbcd97`.
Candidate SDK: **0.11.0**, `95515c2f87fba8982db5a519f9022277667b3cc9`.

## Recommendation

**Proceed with a separate upgrade branch and fresh engines.** The strongest immediate
benefits are Cosmos visual correctness and simpler chat-template handling. Native video
input and structured observations are useful next steps for the Reachy demonstration.
Keep the current demo available until the replacement passes an Orin smoke test and a
comparison using the same inputs and settings.

There is **no measured 0.11 latency or RAM improvement for this demo yet**. The release
does not provide a Cosmos3-Edge Orin Nano before/after benchmark. Newer SDK and smaller
quantized weights do not by themselves establish a faster application.

## What changes, and why it matters

| Change in 0.11 | Value for this demo | What the upgrade alone delivers |
| --- | --- | --- |
| Cosmos-specific patch layout and half-pixel positional interpolation | Correct handling of camera images addresses the same compatibility area that needed repairs during the original bring-up. | Upstream runtime implementation; rebuild the visual engine and recheck caption accuracy. |
| Fused RGB resize and normalization | Potentially reduces the fixed GPU preprocessing cost of each sampled frame. | Used by the vision runtime; the size of the benefit needs measurement. JPEG and network costs still exist. |
| Provider-owned Jinja chat templates rendered in C++ | Removes our repair of the intermediate `processed_chat_template.json` representation. | Export and runtime now use the checkpoint's template directly. Unsupported template constructs fail explicitly. |
| Native Cosmos reasoner video input | Can answer questions about a short action over time, useful for a tabletop Physical AI demonstration. | SDK capability. The existing UI still sends individual images; a bounded clip-capture mode needs additional work. |
| NV12 image-buffer support | Could connect a hardware camera/decoder to inference with fewer intermediate conversions. | SDK capability. The browser/Reachy JPEG bridge does not become a direct NV12 path automatically. |
| XGrammar guided decoding | Can constrain observations to JSON schemas or a fixed set of choices for downstream applications. | SDK capability; needs an exposed request option. Valid syntax does not guarantee factual accuracy or safe robot behavior. |
| Published platform-specific Python wheels | Can shorten installation on a qualified platform by avoiding a local native build. | Optional deployment route. This branch retains a source build matching the existing CUDA/TensorRT pins. |
| GPU-free CuTe DSL kernel AOT generation | More build preparation can happen without an NVIDIA GPU on the computer running the agent. | Explicit architecture-targeted kernel compilation; TensorRT engine construction and inference still use the Orin GPU. |
| INT4 plugin fixes for rank-2/rank-3 activations | Supports the new flattened/ragged runtime input contract correctly. | A correctness/compatibility change, not evidence of faster decoding for our workload. |

The new in-flight batching mode is useful for concurrent clients. Leave it disabled for
the initial single-camera comparison on an 8 GB board. Thor/Blackwell optimizations,
NVFP4, multi-GPU inference, and speculative decoding added for other model families are
not reasons to promise a Cosmos-on-Orin speedup. Orin remains an FP16/INT8/INT4 target.

## Platform and SD card

Use **JetPack 7.2.1 / Jetson Linux 39.2.1**, CUDA 13.2, and the demo's existing
TensorRT 10.16.2.10 source-build configuration. Upstream's Orin platform is AArch64,
SM87, L4T R39.2; its detector maps revision 2.1 into that platform family.

The published wheel's dependency guidance lists TensorRT 10.16.0.72 as its qualified
non-Spark TensorRT-10 package. Matching the `libnvinfer.so.10` major name is not proof
that our existing 10.16.2.10 environment is an identically qualified wheel setup. We
therefore keep the source build for this first migration.

The Mac SD workflow creates a **clean bootable OS image**, not a prebuilt inference
engine. It targets the Orin Nano Super 8 GB developer kit, P3767-0005, and requires
matching QSPI firmware on the Jetson. A USB installation of JetPack must establish
that compatible firmware; this Mac writer cannot update QSPI. See
[the SD workflow](jetpack-sd-mac.md) for the exact board contract.

After writing, verify the entire image against its SHA-256. After moving it to the
Jetson, complete first-boot setup and verify the root device, L4T release and filesystem
expansion. Then install this branch with its bootstrap script. Flash readback alone
does not prove physical boot or model execution.

## Migration boundaries

- Re-export and rebuild both text and vision engines. The new ragged input contract
  makes reusing a 0.10.1 serialized engine an invalid upgrade strategy.
- Validate the exported provider Jinja template; remove the old JSON-template repair.
- Keep the same model revision, quantization scope, image budget, output cap, prompt,
  clocks, batch size and KV capacity for the first comparison.
- The current public demo uses **all-linear INT4 RTN**, with V1 on-device export and
  optional V2 host export. It is a different baseline from the historical MLP-only
  experimental engine. Do not mix those results.
- Keep the 12 GiB available-RAM requirement for optional Linux-host V2 export until
  a new measurement supports changing it. GPU-free AOT does not prove V2 export now
  fits on an 8 GB Orin.
- Update the dependency inventory for the new SDK, including Inja and XGrammar.
  Guided decoding can be unused while its compiled dependency is still present.

## CuTe DSL decoding on Orin

The SDK's **V2 INT4 plugin** has a CuTe DSL GEMV path for Orin SM87. For batch-one,
single-token decoding the flattened row count is one; the plugin dispatches rows
one through four to that path when the generated modules load. It uses CUDA cores
for this small-row workload and shares the packed INT4 weights with the prefill
GEMM path. Group size 128 matches this demo's quantizer.

This path already existed in the pinned 0.10.1 baseline; it is not a new 0.11 speedup
claim. The migration preserves access to it under the new ragged input contract.
Compiling `fmha;int4_fp16_gemm` makes kernels available, but **does not convert a V1
engine to V2**. The default on-Orin export remains V1 because of the prior 8 GB
export-memory failure. CuTe attention (`fmha`) is a separate kernel path.

For CuTe INT4 decoding, use the existing V2 export route from a qualifying Linux
host with at least 12 GiB available RAM; no NVIDIA GPU is required on that host:

```bash
./scripts/bootstrap.sh --host-quantize <jetson-user>@<orin-address>
```

Engine building and inference still happen on the Orin. Native macOS does not meet
the export dependency requirements. A Linux VM would be a separate Linux exporter
whose dependencies and available RAM must be verified first; the 8 GiB SD-builder VM
does not qualify. If bootstrap falls back to V1, do not label the resulting engine as
CuTe INT4. Confirm the V2 export and runtime modules, then compare marginal
milliseconds per generated token at matched settings. TTFT also includes vision
and prompt processing, so a decode improvement need not yield the same TTFT gain.

Sources: [V2 decode dispatch](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/95515c2f87fba8982db5a519f9022277667b3cc9/cpp/plugins/int4GroupwiseGemmPluginV2/int4GroupwiseGemmPluginV2.cpp#L369),
[SM87 GEMV registry](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/95515c2f87fba8982db5a519f9022277667b3cc9/kernelSrcs/build_cutedsl.py#L2092).

## Acceptance and stopping rule

**Hardware finding, 2026-10-06:** the first candidate built and served coherent
text, but failed known-image caption checks. The pinned ONNX export module
`tensorrt_edgellm/models/cosmos3_reasoner/modeling_cosmos3_reasoner_visual.py`
still applies a CHW weight permutation, while the native Cosmos runner supplies
HWC patches. The exported initializer matched that obsolete permutation exactly.
The separate experimental checkpoint-direct builder is a different code path.
This branch now carries a revision- and digest-guarded compatibility applier for
the ONNX exporter. It preserves original notices and invalidates only the vision
artifacts and service acceptance stages; the completed language engine is retained.
The repaired engine passed the repeated text and three known-image checks through
the API and browser UI, with minor unsupported caption details recorded explicitly.
The language engine was unchanged. This validates bounded signs of life, not a
general accuracy or latency improvement; the controlled comparison below has not
been performed. See the [hardware record](first-boot-recovery.md).

First establish coherent text and accurate captions from a freshly built engine,
then check browser streaming and Reachy switching. Use fixed, versioned test images
with small objects, colors, text and multiple subjects; healthy endpoints alone are
insufficient. Keep video, guided decoding and batching out of the initial comparison.

Use one declared protocol for each build: five warmups followed by at least 30 measured
requests spanning several output lengths. Control vision-cache hits explicitly, or use
the same collection of distinct images in both runs. Record actual generated token
counts, prompt/image tokens, precision, image dimensions, clocks and cache state.
Record V1/V2 plugin selection too: a V1-to-V2 kernel change must not be reported as
an SDK-version improvement in a comparison that changed both at once.

Measure server TTFT and inference duration with the same timing boundaries, excluding
JPEG encode/decode, transport and admission queueing. Fit duration against actual
output tokens to report **fixed milliseconds and marginal milliseconds per token**,
alongside the raw requests and residuals. Report browser timing separately. Measure
whole-system shared RAM, available memory and peak process RSS without adding
overlapping counters together.

One controlled A/B comparison is sufficient for the migration decision. Repeat only
for a failed validity check or an identified corrective change. Treat differences
smaller than 5% as inconclusive unless repeatability supports them. Promote the branch
for correctness/capability benefits only if it passes the quality and memory checks
without a material latency regression; otherwise keep it experimental. Do not pursue
unbounded parameter tuning as part of this SDK update.

## Candidate implementation and checks

The candidate pins the SDK in setup, bootstrap and quantizer provenance; validates the
provider's original Jinja during export and engine build; explicitly disables thinking
for captions; and invalidates SDK-dependent setup markers, including the installed
application-source copy. Optional host exports carry an SDK revision receipt and are
rejected if it differs from the target runtime. Existing profiles and timing boundaries
are retained for comparison.

On 2026-10-05, **71 offline tests passed**: eight migration checks, 40 Python UI checks,
10 JavaScript UI checks and 13 SD-helper checks. Shell syntax and diff whitespace checks
passed. An independent source review checked the native constructor and request fields
against the pinned SDK. These checks use fake runtimes/devices where needed. They do
not establish GPU compilation, real checkpoint export, engine loading, card boot,
visual accuracy, RAM use or latency on the new SDK.

Two additional **CPU-native template checks passed** using the unmodified 0.11
Inja renderer and the actual pinned Cosmos Jinja: a text prompt and a single-image
caption prompt, both with thinking disabled. The checks verify prompt preservation,
the image placeholder and the non-thinking assistant prefix. They do not exercise
the full tokenizer/pybind/inference path. See the
[native-template validation record](validation/edgellm-0.11-native-template-2026-10-05.json).

A separate source audit confirmed that the existing SM87/AArch64 CuTe commands,
CMake options and targets, Cosmos export flags, and LLM/visual engine-capacity flags
are accepted by 0.11. The explicit pybind11, CuTe DSL, CuPy and NumPy pins match its
requirements. The exact 0.10.1 and 0.11 pins retain the same PyTorch and Transformers
requirements. The new SDK adds native Inja/XGrammar dependencies and changes its tools
requirements; inventory the resolved build and installed image separately.

Deploy from this candidate's committed checkout after the fresh Orin is reachable:

```bash
./scripts/bootstrap.sh <jetson-user>@<orin-address>
```

The Mac sends the committed sources; the Orin builds the runtime and engines. Keep
the previous installation available until the acceptance checks above pass.

## Primary evidence

- [0.11.0 release](https://github.com/NVIDIA/TensorRT-Edge-LLM/releases/tag/v0.11.0)
- [Cosmos visual contract](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/95515c2f87fba8982db5a519f9022277667b3cc9/cpp/multimodal/cosmos3/cosmos3EdgeViTRunner.h#L44)
- [Fused preprocessing path](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/95515c2f87fba8982db5a519f9022277667b3cc9/cpp/multimodal/qwen2/qwenViTRunner.cpp#L466)
- [Cosmos video example](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/95515c2f87fba8982db5a519f9022277667b3cc9/docs/source/user_guide/examples/vla/cosmos3.md#L98)
- [Provider template contract](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/95515c2f87fba8982db5a519f9022277667b3cc9/docs/source/user_guide/format/chat-template-format.md)
- [Guided decoding](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/95515c2f87fba8982db5a519f9022277667b3cc9/docs/source/user_guide/features/guided-decoding.md)
- [Orin native payload](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/95515c2f87fba8982db5a519f9022277667b3cc9/packaging/variants.toml#L156)
- [Wheel TensorRT requirements](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/95515c2f87fba8982db5a519f9022277667b3cc9/tensorrt_edgellm/_native/dependencies.py#L33)
- [GPU-free kernel AOT](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/95515c2f87fba8982db5a519f9022277667b3cc9/kernelSrcs/README.md#L48)
- [Cosmos ragged model inputs](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/95515c2f87fba8982db5a519f9022277667b3cc9/experimental/builder/models/cosmos3/modeling_cosmos3_reasoner_text.py#L63)
