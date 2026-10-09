# Reachy camera and audio libraries

The optional Reachy bridge builds PyAV against shared FFmpeg 7.1.5 and LAME 3.100
libraries. GPL, nonfree and automatically detected optional FFmpeg components are
disabled. The browser camera and Cosmos inference do not use this environment.

## Install or update

The normal installer prepares these dependencies. To update robot support after
refreshing the source checkout, run:

```sh
sudo -E bash /opt/live-vision-cosmos-demo/scripts/setup-orin.sh --reachy-only
```

This may restart the UI and camera bridge; it preserves the inference service and
engines. Set `REACHY_MEDIA_BUILD_JOBS=1` to reduce build parallelism.

To build without changing services, run as the installation owner:

```sh
sudo apt-get install --no-install-recommends build-essential pkg-config python3-dev python3-venv
python3 scripts/build-reachy-media.py --install-root /opt/live-vision-cosmos-demo --jobs 2
```

The service uses `reachy-media/current/venv/bin/python3`. The `current` symlink
changes only after source checksums, library configuration and codec tests pass.
Successful builds are reused; a failed build preserves the previous selection.
Running services keep their loaded libraries until restarted.

To verify the selected build again from the installation directory:

```sh
reachy-media/current/venv/bin/python3 -B scripts/verify-reachy-media.py \
  --prefix "$(readlink -f reachy-media/current)"
```

The checks cover H.264 decoding, JPEG previews, MP3 streaming and audio resampling.
After an update, also try a live robot camera frame and microphone stream.

## Sources, notices and library replacement

Each build retains the original source archives, license files, build commands,
configuration and verification results. Keep them with any redistributed binary
environment, including the applicable LGPL source and replacement/relinking
materials. See [NOTICE.md](../NOTICE.md).
The repository itself contains the build recipe, not compiled media libraries.

For a separate development environment:

```sh
python3 scripts/build-reachy-media.py \
  --install-root "$HOME/reachy-media-build" --jobs 1
```

To modify a library, re-extract its retained `sources/` archive into a scratch
directory, apply your changes, and repeat the recorded configure/build/install
commands for that component. Use the same prefix, shared-library settings, ABI
and SONAME. Retain that prefix: moving it does not update embedded library paths.
Replace the shared FFmpeg or LAME libraries and start a fresh Python process:

```sh
COSMOS_MEDIA_PREFIX=$(readlink -f "$HOME/reachy-media-build/reachy-media/current")
"$COSMOS_MEDIA_PREFIX/venv/bin/python3" -B scripts/verify-reachy-media.py \
  --prefix "$COSMOS_MEDIA_PREFIX" \
  --output "$HOME/reachy-media-replacement.json"
```

The verifier records the loaded libraries and checks codec behavior. Preserve
modified source and commands alongside the new result. If the ABI changes,
rebuild PyAV against the replacement headers/libraries using the command in
`logs/pyav-build.log`. Changing a system `ffmpeg` executable does not replace the
shared libraries loaded by PyAV.
