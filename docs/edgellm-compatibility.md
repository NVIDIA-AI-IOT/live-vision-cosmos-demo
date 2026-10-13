# TensorRT-Edge-LLM compatibility

This demo uses the [NVIDIA TensorRT-Edge-LLM v0.10.0 SDK](https://github.com/NVIDIA/TensorRT-Edge-LLM/tree/71dd1bae032e70771265917ec74d3ff4cad07a10)
(software development kit), pinned to commit
`71dd1bae032e70771265917ec74d3ff4cad07a10`. It is built from source for Orin SM87
with JetPack 7.2.1 / L4T 39.2.1, CUDA 13.2 and TensorRT 10.16.2.10.

## Engine and export requirements

- Re-export and rebuild both language and vision engines when changing SDK
  versions. Serialized engines from a different SDK are incompatible.
- The SDK writes `processed_chat_template.json` during export. Setup validates
  its roles and generation prompt and fills missing image/video markers from the
  pinned checkpoint's `chat_template.jinja`. The builder copies this JSON into
  the engine directory; the shim rejects missing or incomplete templates. The
  empty system block is emitted once, including for requests with no system message.
- Use engines with their weights included. The pinned SDK's Python binding does
  not expose a checkpoint directory for weight-stripped/refit engines.
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
[pinned V2 dispatch](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/71dd1bae032e70771265917ec74d3ff4cad07a10/cpp/plugins/int4GroupwiseGemmPluginV2/int4GroupwiseGemmPluginV2.cpp).

## Visual-export correction

The checkpoint stores patch-embedding columns in HWC order; the pinned native
Cosmos runner supplies CHW patches. The installer applies
[`fix_cosmos_visual_layout.py`](../scripts/fix_cosmos_visual_layout.py) to permute
only those weight columns before ONNX export, preserving the original notices.
The helper checks the SDK revision and source digest and rejects unexpected edits.

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
and the [upstream release notes](https://github.com/NVIDIA/TensorRT-Edge-LLM/releases/tag/v0.10.0)
for capabilities beyond this single-image demo.
