#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Check the private Reachy media runtime without starting services or connecting to a robot.

The receipt records build metadata and exercised functionality; it is not a release
approval. Run with the new Reachy environment's Python and with bytecode disabled.
"""

import argparse
import base64
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shlex
import struct
import sys
import tempfile


# Extracted from version.h/version_major.h in the signature-verified 7.1.5
# source archive (SHA256 de668509caf9e35e3cd162473441fdb29538c6d96ed080292b3cf9e6fc5d558f).
# The release download page's library table does not include every patch update.
EXPECTED_VERSIONS = {
    "libavutil": (59, 39, 100), "libavcodec": (61, 19, 101),
    "libavformat": (61, 7, 103), "libavdevice": (61, 3, 100),
    "libavfilter": (10, 5, 100), "libswscale": (8, 3, 100),
    "libswresample": (5, 3, 100),
}
REQUIRED_FLAGS = {
    "--disable-gpl", "--disable-nonfree", "--disable-version3",
    "--disable-autodetect", "--disable-static", "--enable-shared",
    "--disable-everything", "--disable-network", "--enable-libmp3lame",
}
FORBIDDEN_FLAGS = {
    "--enable-gpl", "--enable-nonfree", "--enable-version3",
    "--enable-libx264", "--enable-libx265", "--enable-postproc",
    "--enable-autodetect", "--enable-static", "--disable-shared",
    "--enable-network", "--enable-everything",
}
REQUIRED_SMOKES = {
    "h264_decode", "mjpeg_encode", "mp3_stereo_48000", "mp3_mono_24000",
    "pcm_alaw_decode", "pcm_mulaw_decode", "opus_decode",
}
FORBIDDEN_LIBRARY = re.compile(
    r"^lib(?:x264|x265|postproc|cuda|cudart|cublas|cufft|curand|cusolver|"
    r"cusparse|cudnn|nvrtc|nvinfer|nvonnxparser|nvparsers|nvidia)(?:[._-]|$)"
)

# Original synthetic media: one 16x16 yuv420p frame, Y=16, U=V=128.
# Generated with FFmpeg 8.0.1/libx264, baseline, zerolatency, keyint=1;
# filter_units=remove_types=6 removes encoder SEI. No third-party media or code
# is embedded. Decoding reproduced all 384 input bytes. Fixture SHA256:
# 92a9d5291560b2b02c362ebbe0797e0b2631cd5c2be3fa92ddc8eb1751dd0fde
H264_SAMPLE = "AAAAAWdCwArd6EAAAAMAQAAAAwCjxIngAAAAAWjODyyAAAABZYiEBLyYoAA4o4A="


class VerificationError(ValueError):
    """A runtime does not meet the required media build contract."""


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def library_name(path):
    """Identify normal and wheel-renamed media shared objects."""
    basename = Path(path).name
    for name in (*EXPECTED_VERSIONS, "libmp3lame"):
        if basename.startswith((name + ".", name + "-")):
            return name
    return None


def validate_report(report, prefix, *, require_smoke=True):
    """Pure validation of captured JSON-compatible evidence; no filesystem access.

    The collector resolves real paths before calling this function. ``prefix``
    must likewise be an absolute, resolved directory.
    """
    prefix = Path(prefix)
    require(prefix.is_absolute() and ".." not in prefix.parts, "prefix must be canonical")
    require(report.get("prefix") == str(prefix), "receipt prefix does not match requested prefix")
    require(report.get("pyav_version") == "13.1.0", "PyAV must be exactly 13.1.0")
    require(report.get("aiortc_version") == "1.10.1", "aiortc must be exactly 1.10.1")
    require(report.get("ffmpeg_version") == "7.1.5", "FFmpeg must be exactly 7.1.5")
    metadata = report.get("libraries", {})
    require(isinstance(metadata, dict) and set(metadata) == set(EXPECTED_VERSIONS),
            "metadata must contain all seven FFmpeg libraries")
    for name, expected_version in EXPECTED_VERSIONS.items():
        meta = metadata[name]
        version = meta.get("version", [])
        require(isinstance(version, (list, tuple)) and len(version) == 3
                and all(type(part) is int for part in version) and tuple(version) == expected_version,
                f"{name}: unexpected library version")
        require(meta.get("license") == "LGPL version 2.1 or later",
                f"{name}: expected LGPL version 2.1 or later")
        flags = set(shlex.split(meta.get("configuration", "")))
        require(REQUIRED_FLAGS <= flags, f"{name}: missing explicit configure guards")
        require(not any(flag.split("=", 1)[0] in FORBIDDEN_FLAGS for flag in flags),
                f"{name}: forbidden configure option")

    mapped_paths = report.get("mapped_paths", [])
    require(isinstance(mapped_paths, list) and mapped_paths, "no loaded-object evidence")
    media_paths = {}
    for value in mapped_paths:
        path = Path(value)
        require(path.is_absolute() and ".." not in path.parts, "noncanonical loaded path")
        require("av.libs" not in path.parts, "bundled PyAV wheel libraries are loaded")
        require(not FORBIDDEN_LIBRARY.match(path.name), f"forbidden loaded object: {path.name}")
        name = library_name(path)
        if name:
            require(path.is_relative_to(prefix / "lib"), f"{name}: loaded outside private prefix/lib")
            require(name not in media_paths, f"multiple loaded copies of {name}")
            media_paths[name] = str(path)
    expected = set(EXPECTED_VERSIONS) | {"libmp3lame"}
    require(set(media_paths) == expected, "all seven FFmpeg libraries and LAME must be loaded")
    objects = report.get("loaded_libraries", {})
    require(isinstance(objects, dict) and set(objects) == expected, "missing loaded library checksums")
    for name, path in media_paths.items():
        record = objects[name]
        require(record.get("path") == path, f"{name}: checksum path does not match loaded path")
        require(re.fullmatch(r"[0-9a-f]{64}", record.get("sha256", "")) is not None,
                f"{name}: missing SHA256")
        require(isinstance(record.get("size_bytes"), int) and record["size_bytes"] > 0,
                f"{name}: invalid library size")
    if require_smoke:
        results = report.get("codecs", {})
        require(isinstance(results, dict) and REQUIRED_SMOKES <= set(results),
                "missing required codec smoke result")
        for name in REQUIRED_SMOKES:
            require(isinstance(results[name], dict) and results[name].get("ok") is True,
                    f"{name}: codec smoke failed")


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def collect_loaded_objects():
    """Read mappings after imports and smoke tests, resolving every media symlink."""
    paths = set()
    for line in Path("/proc/self/maps").read_text().splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) != 6 or not fields[5].startswith("/"):
            continue
        raw = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[5])
        require(not raw.endswith(" (deleted)"), "a loaded object has been deleted")
        paths.add(str(Path(raw).resolve(strict=True)))
    records = {}
    for path in sorted(paths):
        name = library_name(path)
        if name:
            require(name not in records, f"multiple loaded copies of {name}")
            records[name] = {
                "path": path, "sha256": file_sha256(path),
                "size_bytes": Path(path).stat().st_size,
            }
    return sorted(paths), records


def audio_frame(av, rate, layout, index, samples=960):
    frame = av.AudioFrame(format="s16", layout=layout, samples=samples)
    channels = len(frame.layout.channels)
    values = [int(4096 * math.sin(2 * math.pi * 440 * (index * samples + i) / rate))
              for i in range(samples) for _ in range(channels)]
    frame.planes[0].update(struct.pack("<" + "h" * len(values), *values))
    frame.sample_rate = rate
    frame.time_base = Fraction(1, rate)
    frame.pts = index * samples
    return frame


def smoke_codecs(av, root):
    """Use the real receive-only decoder, JPEG helper and microphone encoder."""
    from aiortc import RTCRtpReceiver
    from aiortc.codecs.g711 import PcmaDecoder, PcmuDecoder
    from aiortc.codecs.h264 import H264Decoder
    from aiortc.codecs.opus import OpusDecoder, OpusEncoder
    from aiortc.jitterbuffer import JitterFrame

    bridge_path = root / "reachy/reachy_mjpeg_bridge.py"
    spec = importlib.util.spec_from_file_location("reachy_media_probe_bridge", bridge_path)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    from reachy_stream.stream import _encode_jpeg

    require(any(c.mimeType.lower() == "video/h264"
                for c in RTCRtpReceiver.getCapabilities("video").codecs),
            "aiortc does not advertise H264 reception")
    sample = base64.b64decode(H264_SAMPLE, validate=True)
    decoder = H264Decoder()
    frames = decoder.decode(JitterFrame(data=sample, timestamp=0))
    frames += decoder.codec.decode(None)
    require(len(frames) == 1 and (frames[0].width, frames[0].height) == (16, 16),
            "H264 synthetic frame did not decode")
    frame = frames[0]
    for plane, width, height, value in zip(frame.planes, (16, 8, 8), (16, 8, 8), (16, 128, 128)):
        pixels = bytes(plane)
        require(all(pixels[row * plane.line_size:row * plane.line_size + width]
                    == bytes([value]) * width for row in range(height)),
                "H264 decoded pixel values differ from synthetic input")
    results = {"h264_decode": {"ok": True, "frames": 1, "width": 16, "height": 16,
                               "input_sha256": hashlib.sha256(sample).hexdigest()}}
    jpeg = _encode_jpeg(frame)
    require(len(jpeg) > 100 and jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9"),
            "bridge JPEG encoding/muxing failed")
    results["mjpeg_encode"] = {"ok": True, "bytes": len(jpeg),
                                "sha256": hashlib.sha256(jpeg).hexdigest()}

    for layout, rate in (("stereo", 48000), ("mono", 24000)):
        hub = bridge.AudioHub()
        queue = hub.subscribe()
        for i in range(24):
            hub.feed(audio_frame(av, rate, layout, i))
            require(hub._encoder is not None, "bridge AudioHub encoder reset during smoke test")
        packets = []
        while not queue.empty():
            packets.append(queue.get_nowait())
        require(packets and all(len(p) > 4 and p[0] == 0xff and p[1] & 0xe0 == 0xe0
                                for p in packets), "bridge did not emit MP3 frame packets")
        require(hub._encoder.sample_rate == 48000 and hub._encoder.layout.name == "stereo"
                and hub._encoder.format.name == "s16p" and hub._pts > 0
                and hub._fifo.samples < hub.FRAME_SIZE, "bridge audio conversion/FIFO failed")
        hub.unsubscribe(queue)
        results[f"mp3_{layout}_{rate}"] = {
            "ok": True, "input_rate": rate, "input_layout": layout,
            "output_rate": 48000, "output_layout": "stereo",
            "packets": len(packets), "bytes": sum(map(len, packets)),
            "sha256": hashlib.sha256(b"".join(packets)).hexdigest(),
        }
    for name, decoder_type, byte in (("pcm_alaw", PcmaDecoder, 0xd5),
                                     ("pcm_mulaw", PcmuDecoder, 0xff)):
        decoded = decoder_type().decode(JitterFrame(data=bytes([byte]) * 160, timestamp=0))
        require(len(decoded) == 1 and decoded[0].samples == 160
                and decoded[0].sample_rate == 8000 and decoded[0].layout.name == "mono",
                f"{name} reception failed")
        results[name + "_decode"] = {"ok": True, "samples": 160, "rate": 8000}
    payloads, timestamp = OpusEncoder().encode(audio_frame(av, 48000, "stereo", 0))
    require(len(payloads) == 1, "Opus test input did not encode")
    decoded = OpusDecoder().decode(JitterFrame(data=payloads[0], timestamp=timestamp))
    require(len(decoded) == 1 and decoded[0].samples == 960
            and decoded[0].sample_rate == 48000 and decoded[0].layout.name == "stereo",
            "aiortc Opus reception failed")
    results["opus_decode"] = {"ok": True, "samples": 960, "rate": 48000}
    return results


def verify(prefix):
    require(sys.platform == "linux", "loaded-library verification requires Linux /proc/self/maps")
    import av
    import aiortc
    from av._core import library_meta

    root = Path(__file__).resolve().parents[1]
    report = {
        "schema_version": 1, "prefix": str(prefix),
        "pyav_version": av.__version__, "aiortc_version": aiortc.__version__,
        "ffmpeg_version": av.ffmpeg_version_info, "libraries": library_meta,
    }
    report["mapped_paths"], report["loaded_libraries"] = collect_loaded_objects()
    validate_report(report, prefix, require_smoke=False)
    report["codecs"] = smoke_codecs(av, root)
    report["mapped_paths"], report["loaded_libraries"] = collect_loaded_objects()
    validate_report(report, prefix)
    report.update({
        "status": "verified", "verified_at": datetime.now(timezone.utc).isoformat(),
        "verifier_sha256": file_sha256(__file__),
        "bridge_sha256": file_sha256(root / "reachy/reachy_mjpeg_bridge.py"),
        "stream_sha256": file_sha256(root / "reachy/reachy_stream/stream.py"),
    })
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    temporary = None
    try:
        # An earlier success receipt must never survive a failed re-verification.
        if args.output:
            args.output.unlink(missing_ok=True)
        prefix = args.prefix.resolve(strict=True)
        report = verify(prefix)
        payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.output:
            with tempfile.NamedTemporaryFile(mode="w", dir=args.output.parent,
                                             prefix=".reachy-media-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
            os.replace(temporary, args.output)
        else:
            sys.stdout.write(payload)
        return 0
    except (Exception, SystemExit) as exc:
        print(f"Reachy media verification failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
