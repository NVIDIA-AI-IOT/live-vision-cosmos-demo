# ARM64 container runtime

This directory builds an ARM64 runtime with one Cosmos3-Edge engine. No prebuilt
image or engine download is published here. Build them using the instructions
below, or use the [automated source installation](../docs/source-installation.md#advanced-source-installation).

The browser camera path needs no Reachy Mini. The image runs the existing inference shim
and HTTPS UI as child processes, with one fixed engine, no host systemd access, and no
Docker socket. Engine switching, robot camera bridging, and Flite speech remain in the
advanced [source installation](../docs/source-installation.md#advanced-source-installation).

## Launch with an image and engine bundle

The target is an Orin Nano Super 8GB with JetPack 7.2.1 / L4T 39.2.1, NVIDIA Container
Runtime, and the matching CUDA 13.2 / TensorRT 10.16.2.10 runtime. Compatibility is declared
in [runtime-spec.json](runtime-spec.json). Python 3.10 or newer is required for the helpers.

For a supplied bundle, obtain its image digest, archive URL and SHA-256 from its
publisher. Keep downloads outside the source checkout. From this application's root:

```bash
export COSMOS_ARTIFACTS="$HOME/cosmos-artifacts"
mkdir -p "$COSMOS_ARTIFACTS"
# Set COSMOS_IMAGE, COSMOS_BUNDLE_URL and COSMOS_BUNDLE_SHA256 from your artifact source.
docker pull "${COSMOS_IMAGE:?image@sha256 digest required}"
python3 container/download.py \
  --url "${COSMOS_BUNDLE_URL:?engine URL required}" \
  --sha256 "${COSMOS_BUNDLE_SHA256:?trusted SHA-256 required}" \
  --archive "$COSMOS_ARTIFACTS/engine.tar.gz" \
  --output "$COSMOS_ARTIFACTS/engine"
```

An interrupted download retains `engine.tar.gz.part`. Rerun the same command to resume.
If the server ignores a range request, the download restarts. A checksum failure discards
the corrupt download. Installation rejects links, path traversal, unexpected files, and
incompatible manifest identities before exposing the bundle at its final path. Checksums
verify integrity against the supplied hash.

Place a certificate and private key as `cert.pem` and `key.pem` in a separate TLS directory.
For a local test on the Jetson, create a certificate with the correct IP in its SAN:

```bash
export COSMOS_TLS="$COSMOS_ARTIFACTS/tls"
mkdir -p "$COSMOS_TLS"
chmod 700 "$COSMOS_TLS"
openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
  -keyout "$COSMOS_TLS/key.pem" -out "$COSMOS_TLS/cert.pem" \
  -subj '/CN=localhost' -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1'
chmod 600 "$COSMOS_TLS/key.pem"
python3 container/launch.py --image "$COSMOS_IMAGE" \
  --bundle "$COSMOS_ARTIFACTS/engine" --tls-dir "$COSMOS_TLS"
```

The launcher binds to loopback by default. Open `https://127.0.0.1:8443` on the Jetson,
trust the local certificate, and allow the browser to use your camera. Select a camera,
start it, and request a caption. For a browser on another computer, use a certificate
covering the Jetson's LAN IP and pass `--bind <Jetson-LAN-IP>` to the launcher; the camera
then belongs to that browser's computer. The service has no account login, so expose it
only on your trusted LAN. Use a trusted certificate when browser camera policy requires it.

Stop with Ctrl-C. The supervisor terminates both children; `--rm` removes the stopped
container. Rerun the launch command to restart. Artifacts and certificates stay on the host.
`--dry-run` verifies the bundle and prints the Docker command without starting it.

## Inference API

The UI proxy accepts one JPEG and one text prompt per streaming request. On the
Jetson, with a small `scene.jpg` in the current directory and the certificate from
the launch instructions above:

```bash
curl --cacert "$COSMOS_TLS/cert.pem" https://127.0.0.1:8443/health/ready

python3 - <<'PY' | curl --no-buffer --cacert "$COSMOS_TLS/cert.pem" \
  https://127.0.0.1:8443/v1/chat/completions \
  -H 'Content-Type: application/json' --data-binary @-
import base64, json
from pathlib import Path
image = base64.b64encode(Path('scene.jpg').read_bytes()).decode('ascii')
print(json.dumps({
    'model': 'nvidia/Cosmos3-Edge', 'stream': True,
    'temperature': 0, 'max_tokens': 64,
    'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'Describe the main object in this image.'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + image}}
    ]}]
}))
PY
```

Keep the encoded JSON request below 2 MiB. The browser prepares the same
OpenAI-style content with a `text` part and an `image_url` part containing a
`data:image/jpeg;base64,...` URL. `/health/ready` returning `{"status":"ready"}`
means the backend loaded; inspect generated text and captions separately.
The packaged UI reports engine switching as unconfigured.

## Build the image and engine bundle

Use native ARM64 Linux with sufficient build memory. Supply a compatible NVIDIA JetPack
development base image by digest that contains the exact TensorRT and CUDA versions in
`runtime-spec.json`. The helper fails on missing or incompatible dependencies. It does
not choose a base tag and has no publication step.

```bash
export COSMOS_ARTIFACTS="$HOME/cosmos-artifacts"
export COSMOS_SOURCE_COMMIT="$(git rev-parse HEAD)"
mkdir -p "$COSMOS_ARTIFACTS"
python3 container/build-image.py \
  --candidate-commit "$COSMOS_SOURCE_COMMIT" \
  --base-image "${COSMOS_BASE_IMAGE:?nvcr.io image@sha256 digest required}" \
  --image-id-file "$COSMOS_ARTIFACTS/runtime-image.id"
export COSMOS_IMAGE="$(cat "$COSMOS_ARTIFACTS/runtime-image.id")"
```

The image builds the [NVIDIA TensorRT-Edge-LLM v0.11.0](https://github.com/NVIDIA/TensorRT-Edge-LLM/tree/95515c2f87fba8982db5a519f9022277667b3cc9) software development kit (SDK), pinned to commit
`95515c2f87fba8982db5a519f9022277667b3cc9`, with native SM87 FMHA and INT4 kernels,
and retains its source, submodules, license files, base-image reference and
resolved Python/Debian package lists. Preserve those materials when redistributing
an image; NVIDIA runtime and third-party components retain their own terms.

Build the engine through the advanced source path, using the pinned model revision and
the same SDK/runtime. SDK 0.11 requires a fresh LLM and visual export/build; do not
reuse a 0.10.1 engine. Apply the source installer's guarded HWC visual repair. The bundle
requires `visual/visual.engine`, `visual/config.json`, `visual/preprocessor_config.json`,
the engine's `embedding.safetensors`, tokenizer files, and matching nonempty provider
`chat_template.jinja` files in the engine and exported checkpoint. Legacy JSON/model
templates are rejected. The manifest records the expected visual source correction. Keep the completed
engine and its exported checkpoint together.
Prepare a separate notices directory containing `LICENSE.OpenMDW` copied from the
[official OpenMDW 1.1 agreement](https://openmdw.ai/license/1-1/) and `MODEL_CARD.md`
copied from the [pinned model card](https://huggingface.co/nvidia/Cosmos3-Edge/blob/344d602b128d1bbdacb43b08d0a3626f46343e29/README.md).
Preserve any other applicable origin notices there. Then:

```bash
python3 container/bundle.py create \
  --engine-dir /opt/live-vision-cosmos-demo/engines/reasoning \
  --checkpoint-dir /opt/live-vision-cosmos-demo/checkpoints/Cosmos3-Edge/onnx/reasoning/llm \
  --notices-dir "$COSMOS_ARTIFACTS/model-notices" \
  --candidate-commit "$COSMOS_SOURCE_COMMIT" \
  --plugin-version 1 --output "$COSMOS_ARTIFACTS/engine-bundle"
python3 container/bundle.py verify "$COSMOS_ARTIFACTS/engine-bundle"
tar -czf "$COSMOS_ARTIFACTS/engine-bundle.tar.gz" -C "$COSMOS_ARTIFACTS/engine-bundle" .
sha256sum "$COSMOS_ARTIFACTS/engine-bundle.tar.gz"
```

Use plugin version 2 only for a V2 engine built through the Linux host quantization path.
The manifest records the source commit, plugin version, model and SDK revisions,
compatibility, and every artifact's size and checksum. Test the resulting engines
on the target device before use.

## Check the build

```bash
python3 -m unittest discover -s container/tests
```

On the target Orin, confirm a clean launch, coherent text, captions on known
scenes, changing browser-camera frames, stop/restart behavior and memory stability.
A readiness response alone does not establish inference quality. Tie any published
performance measurements to the exact image, engine bundle and device settings.
