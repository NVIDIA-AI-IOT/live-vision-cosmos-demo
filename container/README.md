# Packaged runtime candidate

This directory prepares an ARM64 runtime with one Cosmos3-Edge engine. It is an
internal build candidate: an approved base-image digest, built image, engine bundle,
and device validation are still required. No image or model download is published here.
Use the release record supplied with the eventual approved artifacts; do not substitute
an unverified engine or a mutable image tag.

The browser camera path needs no Reachy Mini. The image runs the existing inference shim
and HTTPS UI as child processes, with one fixed engine, no host systemd access, and no
Docker socket. Engine switching, robot camera bridging, and Piper speech remain in the
advanced [source installation](../docs/source-installation.md#advanced-source-installation).

## Download and launch

The target is an Orin Nano Super 8GB with JetPack 7.2.1 / L4T 39.2.1, NVIDIA Container
Runtime, and the matching CUDA 13.2 / TensorRT 10.16.2.10 runtime. Compatibility is declared
in [runtime-spec.json](runtime-spec.json); device validation must establish that the
actual image and engine work on this target. Python 3.10 or newer is required for the helpers.

Obtain the image reference (by digest), engine archive URL, and its SHA-256 from the trusted
release record. Keep downloads under a separate directory, such as `$HOME/cosmos-artifacts`,
outside the source checkout. From this application's root:

```bash
export COSMOS_ARTIFACTS="$HOME/cosmos-artifacts"
mkdir -p "$COSMOS_ARTIFACTS"
# Set COSMOS_IMAGE, COSMOS_BUNDLE_URL and COSMOS_BUNDLE_SHA256 from the release record.
docker pull "${COSMOS_IMAGE:?approved image@sha256 digest required}"
python3 container/download.py \
  --url "${COSMOS_BUNDLE_URL:?approved engine URL required}" \
  --sha256 "${COSMOS_BUNDLE_SHA256:?trusted SHA-256 required}" \
  --archive "$COSMOS_ARTIFACTS/engine.tar.gz" \
  --output "$COSMOS_ARTIFACTS/engine"
```

An interrupted download retains `engine.tar.gz.part`. Rerun the same command to resume.
If the server ignores a range request, the download restarts. A checksum failure discards
the corrupt download. Installation rejects links, path traversal, unexpected files, and
incompatible manifest identities before exposing the bundle at its final path. Checksums
verify integrity against the trusted release record; they do not establish model accuracy.

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

## Build an internal candidate

Use native ARM64 Linux with sufficient build memory. Supply an approved NVIDIA JetPack
development base image by digest that contains the exact TensorRT and CUDA versions in
`runtime-spec.json`. The helper fails on missing or incompatible dependencies. It does
not choose a base tag and has no publication step.

```bash
python3 container/build-image.py \
  --candidate-commit "${COSMOS_CANDIDATE_COMMIT:?exact candidate Git SHA required}" \
  --base-image "${COSMOS_BASE_IMAGE:?approved nvcr.io image@sha256 digest required}" \
  --image-id-file "$COSMOS_ARTIFACTS/runtime-image.id"
export COSMOS_IMAGE="$(cat "$COSMOS_ARTIFACTS/runtime-image.id")"
```

The image builds SDK commit `95515c2f87fba8982db5a519f9022277667b3cc9` with native
SM87 FMHA and INT4 kernels and includes the SDK source, submodules and license files.
Build-time package resolution can change: record the image digest and scan that image,
not only this Dockerfile. The resulting image retains `/opt/base-image-reference.txt`,
`/opt/runtime-python-packages.json`, `/opt/runtime-debian-packages.tsv`, and
`/opt/runtime-sdk-submodules.txt`. Capture those files and an SBOM from the completed
image for review. This source inventory is not the image's complete dependency inventory.

Build the engine through the advanced source path, using the pinned model revision and
the same SDK/runtime. SDK 0.11 requires a fresh LLM and visual export/build; do not
reuse a 0.10.1 engine. Apply the source installer's guarded HWC visual repair. The bundle
requires `visual/visual.engine`, `visual/config.json`, `visual/preprocessor_config.json`,
the engine's `embedding.safetensors`, tokenizer files, and matching nonempty provider
`chat_template.jinja` files in the engine and exported checkpoint. Legacy JSON/model
templates are rejected. The manifest records the expected visual source correction;
that declaration still needs artifact-level hardware evidence. Keep the completed engine and its exported checkpoint together.
Prepare a separate notices directory containing `LICENSE.OpenMDW` copied from the
[official OpenMDW 1.1 agreement](https://openmdw.ai/license/1-1/) and `MODEL_CARD.md`
copied from the [pinned model card](https://huggingface.co/nvidia/Cosmos3-Edge/blob/344d602b128d1bbdacb43b08d0a3626f46343e29/README.md).
Preserve any other applicable origin notices there. Then:

```bash
python3 container/bundle.py create \
  --engine-dir /opt/live-vision-cosmos-demo/engines/reasoning \
  --checkpoint-dir /opt/live-vision-cosmos-demo/checkpoints/Cosmos3-Edge/onnx/reasoning/llm \
  --notices-dir "$COSMOS_ARTIFACTS/model-notices" \
  --candidate-commit "$COSMOS_CANDIDATE_COMMIT" \
  --plugin-version 1 --output "$COSMOS_ARTIFACTS/engine-candidate"
python3 container/bundle.py verify "$COSMOS_ARTIFACTS/engine-candidate"
tar -czf "$COSMOS_ARTIFACTS/engine-candidate.tar.gz" -C "$COSMOS_ARTIFACTS/engine-candidate" .
sha256sum "$COSMOS_ARTIFACTS/engine-candidate.tar.gz"
```

Use plugin version 2 only for a V2 engine built through the Linux host quantization path.
The manifest records the candidate Git commit, declared plugin version, upstream source/model/SDK commits, compatibility,
and every artifact's size and checksum. Its `pending_hardware_and_human_review` status
is intentional. Declaring INT4 does not prove the serialized engine uses it.

## Validation

Offline checks:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s container/tests
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s ui/tests
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/jetpack
# Requires NumPy; exercises synthetic CPU weights, not model inference.
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
node --test ui/tests/test_engine_switch.js ui/tests/test_reachy_setup.js
```

The device gate must record the candidate Git commit, image digest, manifest/archive
hashes, SDK/model revisions, L4T/TensorRT versions, power mode, and test inputs. Confirm
clean installation, actual INT4 in the build/runtime evidence, coherent text, correct
captions on known scenes, changing browser-camera frames, stop/restart behavior, and
memory stability during a sustained camera run. Keep expected answers and actual outputs;
health-only checks and synthetic unit tests cannot establish inference quality.

No latency, throughput or memory claim in this candidate is a release benchmark. Record
measurements only against the final artifacts and device configuration.
