# TensorRT-Edge-LLM compatibility

This demo uses the [NVIDIA TensorRT-Edge-LLM v0.11.0 SDK](https://github.com/NVIDIA/TensorRT-Edge-LLM/tree/95515c2f87fba8982db5a519f9022277667b3cc9)
(software development kit), pinned to commit
`95515c2f87fba8982db5a519f9022277667b3cc9`. It is built from source for Orin SM87
with JetPack 7.2.1 / L4T 39.2.1, CUDA 13.2 and TensorRT 10.16.2.10.

## Engine and export requirements

- Re-export and rebuild both language and vision engines when changing SDK
  versions. Serialized 0.10.1 engines are incompatible with this configuration.
- Keep the checkpoint's original `chat_template.jinja`; the installer validates
  it during export and build. Do not apply the older JSON chat-template repair.
- The demo quantizes the model's linear layers with INT4 RTN before export.
  Passing an AWQ quantization flag at export does not perform that conversion.
- On-device export uses the V1 INT4 plugin to fit an 8 GB Orin Nano. The optional
  V2 export path requires a Linux host with at least 12 GiB available RAM:

  ```bash
  ./scripts/bootstrap.sh --host-quantize <jetson-user>@<orin-address>
  ```

  No host GPU is required. Engine building and inference still run on the Orin;
  native macOS does not meet the exporter dependency requirements.
- Keep `--parallel 1` for the native runtime build on an 8 GB board. Increasing
  workers or engine/KV-cache capacities can exhaust available memory.

The V2 INT4 plugin can use CuTe DSL kernels for small-row decoding on SM87.
Compiling those kernels does not convert a V1 engine into V2. See the
[pinned V2 dispatch](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/95515c2f87fba8982db5a519f9022277667b3cc9/cpp/plugins/int4GroupwiseGemmPluginV2/int4GroupwiseGemmPluginV2.cpp#L369).

## Visual-export correction

The pinned ONNX exporter permutes patch weights into CHW order, while the native
Cosmos runner supplies HWC patches. The installer applies
[`fix_cosmos_visual_layout.py`](../scripts/fix_cosmos_visual_layout.py) to the
specific export module, preserving its notices. The helper checks both the SDK
revision and source digest and rejects unexpected local edits.

When this correction changes, setup invalidates the vision export, vision engine
and service checks while retaining the completed language engine. Do not bypass
its revision/digest checks or substitute the separate experimental
checkpoint-direct builder.

After building, test coherent text and captions for known images, then check
changing camera frames and memory during a sustained run. `/health/ready` confirms
that the engine loaded; it does not establish caption accuracy. For speed
comparisons, keep the image, prompt, output length, plugin selection and power
settings consistent. No SDK-version speedup is assumed.

See [source installation](source-installation.md) for the complete build commands
and the [upstream release notes](https://github.com/NVIDIA/TensorRT-Edge-LLM/releases/tag/v0.11.0)
for capabilities beyond this single-image demo.
