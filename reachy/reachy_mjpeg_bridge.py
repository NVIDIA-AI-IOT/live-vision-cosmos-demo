#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Serve the Reachy Mini camera and microphone to everything on the Jetson, from one session.

The robot speaks WebRTC only. go2rtc/ffmpeg, other VLM front ends and the Live Vision UI all want
something simpler, and each of them opening its own WebRTC session would cost the robot one H.264
encoder per consumer. So this holds exactly ONE session to the robot and fans it out:

    /mjpeg       multipart MJPEG, for go2rtc/ffmpeg consumers and browser previews
    /still.jpg   the newest frame, 503 when there is no fresh one
    /audio.mp3   the robot's microphone as an endless MP3 stream (encoded only while listened to)
    /healthz     state, and WHY it is in that state
    --push-url   optionally POST each new frame (image/jpeg) to an HTTP endpoint

The official Reachy app is a separate WebRTC consumer and coexists with this one. An on-robot app
that takes the camera outright, or the daemon releasing its media, is not an error: the bridge
goes dormant, says why in /healthz, and resumes on its own when the camera comes back.

Why aiortc and not the Reachy SDK: the SDK's WebRTC backend needs GStreamer's `webrtcsrc` from
gst-plugins-rs, which is not packaged for Linux and must be built from Rust source. aiortc is pure
Python. The heavy lifting is Pollen's own ReachyMiniStreamClient, vendored from their Home
Assistant component (reachy_stream/), so this tracks their signalling handling.

Why the answer is H.264 only. Daemon 1.11 offers RTX (retransmission) alongside H.264, and aiortc
(1.10.1, and still 1.14.0) mishandles an RTX packet: after unwrapping it, _handle_rtp_packet keeps
the RTX codec, so a frame completed by a retransmission is queued to the decoder as `video/rtx`.
The decoder thread dies with "No decoder found for MIME type `video/rtx`" and the track goes silent
seconds into every session. Leaving RTX out of the answer means the robot never sends it; losses are
recovered by the keyframe requests aiortc already sends.
"""
from __future__ import annotations

import argparse
import asyncio
import errno
import logging
import os
import re
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_LOG = logging.getLogger("reachy-bridge")

# A session that delivers no video frame for this long is dead, whatever the signalling says.
STALL_AFTER = 8.0
# A brand new session gets longer: ICE, DTLS and the first keyframe take a few seconds.
FIRST_FRAME_TIMEOUT = 20.0
# still.jpg and has_frame only vouch for a frame this recent.
FRESH_FOR = 5.0

# Retry pacing for real failures. Resets once a session has stayed live this long.
#
# The cap is deliberately long. Every session costs the robot more than it looks: daemon 1.11 puts
# TURN relays on every consumer, LAN ones included, and libnice leaves their refresh sockets open
# when the session ends ("We still have alive TURN refreshes"). One failed session every ~26 s
# overnight exhausted the daemon's file descriptors - "Too many open files" - after which it
# rejected every consumer, the official app included, until the robot was power-cycled. A retry
# that cannot succeed must not be allowed to use up the robot.
MIN_BACKOFF_S = 3.0
MAX_BACKOFF_S = 300.0
HEALTHY_AFTER_S = 60.0
# This many sessions in a row that never delivered video means the robot, not the network.
REJECTED_STREAK_WARN = 4

# While the camera is deliberately unavailable (an app holds it, the daemon released it), poll the
# robot's REST API at this pace instead of opening WebRTC sessions into a camera we cannot have.
DORMANT_POLL_S = 10.0
# A robot app's lock only counts as released once it has stayed free this long (see
# camera_unavailable): long enough to cover an app restarting.
LOCK_FREE_GRACE_S = 30.0

# The daemon out of file descriptors is worse than a refusal. Adding a WebRTC viewer makes its
# encoder allocate buffers, libcamera's next request fails ("Internal data stream error"), and the
# robot's whole camera pipeline stops - for its local apps too, until the media is rebuilt. Seen
# 2026-09-24 with the testbench app: one test session and its captures went dead. So after a
# session that never delivered video, the bridge reads the daemon's own journal (a websocket on the
# REST port, never WebRTC) for the tell-tale line, and if it is there it stops trying until the
# daemon is a new process. Each read spawns a journalctl on the robot, hence the slow re-check.
DAEMON_LOG_PATH = "/logs/ws/daemon"
FD_EXHAUSTED_MARK = "Too many open files"
FD_EXHAUSTED_RECHECK_S = 60.0
# Between releasing and re-acquiring the robot's media when rebuilding its camera pipeline.
MEDIA_REBUILD_PAUSE_S = 3.0
# Forced recovery: with --recover-ssh/--recover-key, an exhausted daemon is restarted over SSH by a
# key the robot only lets run `systemctl restart reachy-mini-daemon` (see
# setup_robot_recovery.sh, same directory). At most once per interval, so a daemon that wedges
# again at once (or a restart that does not take) cannot turn into a restart loop.
RECOVER_MIN_INTERVAL_S = 900.0
# After an attempt that restarted nothing (ssh failed, or a robot app was running), try again sooner.
RECOVER_RETRY_S = 120.0
# The forced command is `systemctl restart --no-block`, which returns at once; this only bounds ssh.
RECOVER_TIMEOUT_S = 60.0
_DAEMON_PID = re.compile(r"(?:launcher\.sh|python\d*)\[(\d+)\]")

# Frame push (--push-url): the pause after a refusal (an HTTP error, a 409 from Stop), and the
# slower one after the receiver did not answer at all, as when it is stopped on purpose.
PUSH_RETRY_S = 5.0
PUSH_UNREACHABLE_RETRY_S = 10.0

# An MJPEG or audio client that leaves is noticed when the next write to it fails. While dormant
# there is no next write, so between frames check this often whether the client is still there.
CLIENT_IDLE_CHECK_S = 5.0

# Retry pace for a listen address that does not exist yet (the docker gateway before dockerd).
BIND_RETRY_S = 5.0

# Pollen's stream client ships inside the HA custom component and has no HA imports.
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import av
    from aiortc import RTCRtpReceiver
    from aiortc.mediastreams import MediaStreamError

    from reachy_stream.stream import ReachyMiniStreamClient, _default_pc_factory
except ImportError as e:  # pragma: no cover
    sys.exit(f"cannot import the vendored stream client ({e})")


def h264_only_pc():
    """A peer connection whose answer accepts H.264 video without RTX, plus the audio track.

    The transceivers are added BEFORE the robot's offer is applied: aiortc negotiates codecs inside
    setRemoteDescription, against the preferences of whichever transceiver claims each m-line, and
    it reuses a pre-added transceiver of the right kind rather than creating its own.
    """
    pc = _default_pc_factory()
    h264 = [c for c in RTCRtpReceiver.getCapabilities("video").codecs
            if c.mimeType.lower() == "video/h264"]
    pc.addTransceiver("video", direction="recvonly").setCodecPreferences(h264)
    pc.addTransceiver("audio", direction="recvonly")
    return pc


class AVStreamClient(ReachyMiniStreamClient):
    """Pollen's client, plus frame timestamps and the audio track.

    The upstream client only reads video. An unread audio track is not harmless: aiortc queues
    every decoded frame for a reader that never comes, so the process grows until it is killed.
    """

    def __init__(self, host: str, *, session: aiohttp.ClientSession, port: int, on_audio) -> None:
        super().__init__(host, session=session, port=port, pc_factory=h264_only_pc)
        self.last_video_at = 0.0
        self.video_frames = 0
        self.signalling_reached = False
        self._on_audio = on_audio
        self._audio_task: asyncio.Task | None = None

    @property
    def session_over(self) -> bool:
        return self._task is None or self._task.done()

    @property
    def end_reason(self) -> str:
        """Why the session ended, as specifically as the upstream client lets us know.

        It catches network errors itself (and logs them); anything else - a failed SDP
        negotiation, say - ends the task with the exception still attached. For the swallowed
        ones, how far the session got tells them apart: a signalling port that never answered and
        a robot that ended a running session send a human to different places.
        """
        task = self._task
        if task is not None and task.done() and not task.cancelled() and task.exception():
            e = task.exception()
            return f"session failed: {type(e).__name__}: {e}"
        if not self.signalling_reached:
            return f"robot signalling at {self.signalling_url} unreachable"
        if self._session_id is None:
            return ("robot signalling started no session (camera producer not advertised, "
                    "or the robot refused the consumer)")
        return "robot ended the session (producer gone or signalling closed)"

    async def _signalling_loop(self, ws, pc) -> None:
        # Only ever called once the websocket is open.
        self.signalling_reached = True
        await super()._signalling_loop(ws, pc)

    def _on_track(self, track) -> None:
        super()._on_track(track)
        if track.kind == "audio" and self._audio_task is None:
            self._audio_task = asyncio.get_running_loop().create_task(self._consume_audio(track))

    async def _consume_video(self, track) -> None:
        try:
            while True:
                self._frame = await track.recv()
                self.last_video_at = time.monotonic()
                self.video_frames += 1
                self._frame_event.set()
        except MediaStreamError:
            _LOG.info("video track ended")

    async def _consume_audio(self, track) -> None:
        try:
            while True:
                self._on_audio(await track.recv())
        except MediaStreamError:
            pass
        except Exception as e:  # a bad audio frame must never take the video down with it
            _LOG.warning("audio track error: %s", e)

    async def close(self) -> None:
        await self.async_shutdown()
        task, self._audio_task = self._audio_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        # async_shutdown() awaits the session task with CancelledError suppressed. A cancel aimed
        # at the caller while it waits there (SIGTERM during a session's teardown) is passed on to
        # that task and suppressed along with it, so the caller would carry on: the supervisor
        # would sleep its backoff and open the next robot session instead of stopping. The cancel
        # is still counted on the caller's task, so raise it here.
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise asyncio.CancelledError


class FrameHub:
    """The newest JPEG, a sequence number, and a way to wait for the next one."""

    def __init__(self) -> None:
        self.jpeg: bytes | None = None
        self.seq = 0
        self.at = 0.0
        self._changed = asyncio.Event()

    def publish(self, jpeg: bytes) -> None:
        self.jpeg, self.seq, self.at = jpeg, self.seq + 1, time.monotonic()
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    @property
    def age(self) -> float | None:
        return None if not self.at else time.monotonic() - self.at

    @property
    def fresh(self) -> bool:
        return self.jpeg is not None and self.age is not None and self.age < FRESH_FOR

    async def next(self, after_seq: int, timeout: float) -> bool:
        """Wait until a frame newer than after_seq exists. False on timeout."""
        deadline = time.monotonic() + timeout
        while self.seq <= after_seq:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except asyncio.TimeoutError:
                return False
        return True


class AudioHub:
    """Robot microphone -> MP3, encoded only while somebody is listening.

    MP3 because it is the one format every consumer here reads as an endless HTTP stream: ffmpeg
    (go2rtc, say) and a browser <audio> element alike. The encoder is torn down when the last
    listener leaves, so an unheard microphone costs nothing beyond draining the track.
    """

    FRAME_SIZE = 1152   # samples per MP3 frame
    QUEUE_MAX = 64      # ~1.5 s of MP3 frames per listener before its oldest data is dropped

    def __init__(self, bitrate: int = 96_000) -> None:
        self.bitrate = bitrate
        self.frames_in = 0
        self.last_at = 0.0
        self._listeners: set[asyncio.Queue] = set()
        self._encoder = None
        self._resampler = None
        self._fifo = None
        self._pts = 0

    @property
    def listeners(self) -> int:
        return len(self._listeners)

    def feed(self, frame) -> None:
        self.frames_in += 1
        self.last_at = time.monotonic()
        if not self._listeners:
            self._encoder = None
            return
        try:
            if self._encoder is None:
                self._open()
            # The resampler checks pts continuity from zero; aiortc's frames carry the session's
            # running clock. The MP3 stream gets its own pts below, so drop the incoming one.
            frame.pts = None
            for resampled in self._resampler.resample(frame):
                self._fifo.write(resampled)
            while self._fifo.samples >= self.FRAME_SIZE:
                chunk = self._fifo.read(self.FRAME_SIZE)
                chunk.pts, self._pts = self._pts, self._pts + self.FRAME_SIZE
                for packet in self._encoder.encode(chunk):
                    self._broadcast(bytes(packet))
        except Exception as e:
            _LOG.warning("mp3 encode failed, resetting the encoder: %s", e)
            self._encoder = None

    def _open(self) -> None:
        enc = av.CodecContext.create("libmp3lame", "w")
        enc.sample_rate = 48000
        enc.layout = "stereo"
        enc.format = "s16p"
        enc.bit_rate = self.bitrate
        enc.open()
        self._encoder = enc
        self._resampler = av.AudioResampler(format="s16p", layout="stereo", rate=48000)
        self._fifo = av.AudioFifo()
        self._pts = 0

    def _broadcast(self, data: bytes) -> None:
        for q in self._listeners:
            if q.full():
                q.get_nowait()   # a slow listener loses its oldest audio, never stalls the rest
            q.put_nowait(data)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(self.QUEUE_MAX)
        self._listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._listeners.discard(q)


class Bridge:
    def __init__(self, host: str, port: int, fps: float, daemon_port: int = 8000,
                 push_url: str | None = None, push_fps: float = 0.0,
                 recover_ssh: str | None = None, recover_key: str | None = None) -> None:
        self.host = host
        self.port = port
        self.daemon = f"http://{host}:{daemon_port}"
        self.interval = 1.0 / fps if fps > 0 else 0.2
        self.frames = FrameHub()
        self.audio = AudioHub()
        self.session: aiohttp.ClientSession | None = None
        self.client: AVStreamClient | None = None
        # What the bridge is doing and why. `reason` is the part a human needs when it is not live:
        # "camera held by the app 'x'" and "robot unreachable" call for different responses.
        self.state = "starting"
        self.reason: str | None = None
        self.blocked_by: str | None = None
        self.sessions = 0
        self.restarts = 0
        self.failed_streak = 0
        self._backoff = MIN_BACKOFF_S
        # Set while the daemon process is out of file descriptors: its pid, so a restarted daemon
        # (a new pid) lifts it. See FD_EXHAUSTED_MARK.
        self.fd_exhausted_pid: str | None = None
        self._fd_checked_at = 0.0
        self.recover_ssh, self.recover_key = recover_ssh, recover_key
        self.recovery = {"configured": bool(recover_ssh and recover_key), "attempts": 0,
                         "last_result": None}
        # What recovery is doing, carried in `reason` for as long as the hold lasts.
        self._recovery_note: str | None = None
        # Earliest monotonic time for the next restart. A restart's time is also written to systemd's
        # StateDirectory, so the 15 min limit holds across bridge restarts, not just within one run.
        self._state_file = (Path(os.environ["STATE_DIRECTORY"].split(":")[0]) / "last_recover"
                            if os.environ.get("STATE_DIRECTORY") else None)
        self._next_recover_at = 0.0
        try:
            # A clock step can make the age slightly negative; that is "just now", not "never".
            ago = max(0.0, time.time() - float(self._state_file.read_text()))
            if ago < RECOVER_MIN_INTERVAL_S:
                self._next_recover_at = time.monotonic() + RECOVER_MIN_INTERVAL_S - ago
        except (AttributeError, OSError, ValueError):
            pass
        # When a robot app was last seen holding the robot, and which (LOCK_FREE_GRACE_S).
        self._lock_seen_at = 0.0
        self._last_holder: str | None = None
        self.mjpeg_clients = 0
        self.push_url = push_url
        self.push_interval = (1.0 / push_fps) if push_fps > 0 else self.interval
        self.push = {"enabled": bool(push_url), "state": "idle", "pushed": 0, "detail": None}
        # Held, not fire-and-forget: the event loop keeps only weak references to tasks, and stop()
        # needs them to end the robot session.
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self.session = aiohttp.ClientSession()
        self._tasks.append(asyncio.create_task(self._supervise()))
        if self.push_url:
            self._tasks.append(asyncio.create_task(self._push_loop()))

    async def stop(self) -> None:
        """End the robot session, then close the HTTP client.

        Cancelling the supervisor unwinds the running session through close(), which sends the
        robot its endSession. Left to asyncio.run() cancelling whatever is still running, that
        happens too, but nothing closes the client session and aiohttp logs it as leaked.
        """
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.session is not None:
            await self.session.close()

    # ------------------------------------------------------------------ robot side
    async def _get_json(self, path: str):
        async with self.session.get(f"{self.daemon}{path}",
                                    timeout=aiohttp.ClientTimeout(total=4)) as r:
            if r.status != 200:
                raise RuntimeError(f"{path} answered HTTP {r.status}")
            return await r.json()

    async def camera_unavailable(self) -> tuple[str | None, str | None]:
        """(reason, blocking_app) if the camera is deliberately or knowably unavailable.

        Asked over plain REST before any WebRTC session is opened, because from the WebRTC side
        "no frames" looks the same whatever the cause, and only some causes are worth retrying.
        Every failed session into a camera we cannot have is an extra encoder spun up on the robot.
        """
        try:
            status = await self._get_json("/api/daemon/status")
        except Exception as e:
            return f"robot daemon unreachable ({type(e).__name__})", None
        if status.get("state") != "running":
            return f"robot daemon is {status.get('state')!r}, not running", None
        if self.fd_exhausted_pid is not None:
            reason = await self._still_fd_exhausted()
            if reason:
                return reason, None
        try:
            media = await self._get_json("/api/media/status")
            if media.get("no_media"):
                return "daemon runs without media", None
            if media.get("released"):
                return "daemon has released the camera and microphone", None
            if media.get("available") is False:
                return "daemon reports the camera unavailable", None
        except Exception:
            pass   # older daemons lack the endpoint; the session attempt will tell us
        try:
            lock = await self._get_json("/api/daemon/robot-app-lock-status")
            if lock.get("state") == "local_app":
                holder = lock.get("holder_name") or "an app"
                self._lock_seen_at, self._last_holder = time.monotonic(), holder
                return f"camera held by the robot app {holder!r}", holder
        except Exception:
            pass
        # An app restarting frees the lock for a few seconds. Dialling into that gap is how, on
        # 2026-09-24, one session reached a robot out of descriptors and stopped the testbench's
        # camera the moment it came back. So a lock only counts as released once it stays free.
        since = time.monotonic() - self._lock_seen_at
        if self._lock_seen_at and since < LOCK_FREE_GRACE_S:
            return (f"robot app {self._last_holder!r} just released the camera; waiting "
                    f"{LOCK_FREE_GRACE_S - since:.0f}s in case it is restarting"), None
        return None, None

    async def _daemon_log_tail(self, seconds: float = 2.0) -> list[str] | None:
        """The daemon's recent journal lines (it sends its last 100, then follows), or None.

        Read from the daemon's REST port, never over WebRTC, so it costs the robot a journalctl
        process and nothing else. None when the endpoint is missing or does not answer.
        """
        url = "ws" + self.daemon[len("http"):] + DAEMON_LOG_PATH
        lines: list[str] = []
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(seconds + 5):
                async with self.session.ws_connect(url) as ws:
                    deadline = loop.time() + seconds
                    while (left := deadline - loop.time()) > 0:
                        # The backlog arrives at once; after it, a short lull means it is all here,
                        # and the rest would only be the live tail this does not need.
                        wait = min(left, 0.5) if lines else left
                        try:
                            msg = await asyncio.wait_for(ws.receive(), wait)
                        except asyncio.TimeoutError:
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            break
                        if msg.data:
                            lines.append(msg.data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _LOG.debug("daemon log unreadable: %s", e)
        return lines or None

    async def _note_fd_exhaustion(self) -> bool:
        """After a session without video: is the daemon out of file descriptors? Sets the state."""
        lines = await self._daemon_log_tail()
        pid = daemon_pid(lines or [])
        # Only the current process's lines count: straight after a restart the journal still holds
        # the previous process's "Too many open files", which says nothing about the new one.
        if not lines or not fd_exhausted(lines, pid):
            return False
        self.fd_exhausted_pid = pid or "unknown"
        self._fd_checked_at = time.monotonic()
        issued = await self._try_recovery()
        self.state, self.reason = "dormant", self._fd_reason()
        _LOG.error("%s", self.reason)
        if not issued:
            await self._repair_robot_camera()
        return True

    def _fd_reason(self) -> str:
        return fd_exhausted_reason(self.fd_exhausted_pid,
                                   self._recovery_note if self.recovery["configured"] else None)

    async def _run_recover_command(self) -> tuple[int, str]:
        """Run the recovery key once. What runs on the robot is fixed by the key's forced command."""
        proc = await asyncio.create_subprocess_exec(
            # -T: the key is no-pty on the robot, and asking for none keeps ssh from saying so.
            "ssh", "-T", "-i", self.recover_key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=yes", self.recover_ssh,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), RECOVER_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, "timeout"
        return proc.returncode, err.decode(errors="replace").strip()[-300:]

    async def _try_recovery(self) -> bool:
        """Restart the exhausted daemon over SSH when that is allowed now. True if one was issued.

        Called when the exhaustion is first seen and again on every dormant poll while it lasts, so a
        failed or deferred attempt is retried instead of leaving the robot for a power-cycle.
        """
        if not self.recovery["configured"]:
            return False
        now = time.monotonic()
        if now < self._next_recover_at:
            if self._recovery_note is None:
                self._recovery_note = (f"A restart over SSH was issued less than "
                                       f"{RECOVER_MIN_INTERVAL_S / 60:.0f} min ago; the next is "
                                       f"allowed in {self._next_recover_at - now:.0f}s.")
            return False
        # A restart ends any running robot app (apps are the daemon's children). The media rebuild
        # already defers to a running app; a restart must too, and the hold retries it later.
        try:
            lock = await self._get_json("/api/daemon/robot-app-lock-status")
            if lock.get("state") == "local_app":
                self._next_recover_at = now + RECOVER_RETRY_S
                self._recovery_note = (f"Automatic restart deferred while the robot app "
                                       f"{lock.get('holder_name')!r} runs; retrying in "
                                       f"{RECOVER_RETRY_S:.0f}s.")
                self.recovery["last_result"] = f"deferred: app {lock.get('holder_name')!r} running"
                return False
        except Exception:
            pass
        self.recovery["attempts"] += 1
        try:
            rc, err = await self._run_recover_command()
        except (OSError, ValueError) as e:
            rc, err = -1, f"{type(e).__name__}: {e}"
        stamp = time.strftime("%H:%M:%S")
        if rc != 0 and err != "timeout":
            self._next_recover_at = now + RECOVER_RETRY_S
            self._recovery_note = (f"Automatic restart over SSH failed at {stamp} ({err}); "
                                   f"retrying in {RECOVER_RETRY_S:.0f}s.")
            self.recovery["last_result"] = f"failed at {stamp} (exit {rc}): {err}"
            _LOG.error("could not restart the robot daemon over SSH: %s", err)
            return False
        # Exit 0 means systemd queued the restart. A timeout means it may have: either way the new
        # pid is what proves it, so watch for one now rather than guess.
        self._next_recover_at = now + RECOVER_MIN_INTERVAL_S
        self._recovery_note = (f"Restarted it over SSH at {stamp}{' (unconfirmed)' if rc else ''}; "
                               f"waiting for the new process.")
        self.recovery["last_result"] = (f"restart {'issued, unconfirmed' if rc else 'issued'} at "
                                        f"{stamp} for pid {self.fd_exhausted_pid}")
        _LOG.warning("robot daemon pid %s: %s", self.fd_exhausted_pid, self._recovery_note)
        self._fd_checked_at = 0.0     # look for the new pid on the next poll, not in a minute
        try:
            if self._state_file is not None:
                self._state_file.write_text(f"{time.time():.3f}")
        except OSError as e:
            _LOG.warning("could not record the restart time: %s", e)
        return True

    async def _repair_robot_camera(self) -> None:
        """Rebuild the daemon's media pipeline, which the failed session just stopped.

        Otherwise the next robot app to start finds no camera at all: on 2026-09-24 the testbench
        answered "No camera frame available" until the media was released and re-acquired. Only
        done while no local app holds the robot, because a release interrupts a running app's media
        (the testbench's pipeline died with it and needed an app restart).
        """
        try:
            lock = await self._get_json("/api/daemon/robot-app-lock-status")
            if lock.get("state") == "local_app":
                _LOG.warning("not rebuilding the robot's media: the app %r is running",
                             lock.get("holder_name"))
                return
            for path, pause in (("/api/media/release", MEDIA_REBUILD_PAUSE_S),
                                ("/api/media/acquire", 0.0)):
                async with self.session.post(f"{self.daemon}{path}",
                                             timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status != 200:
                        raise RuntimeError(f"{path} answered HTTP {r.status}")
                await asyncio.sleep(pause)
            _LOG.info("rebuilt the robot's media pipeline for its local apps")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _LOG.warning("could not rebuild the robot's media: %s", e)

    async def _still_fd_exhausted(self) -> str | None:
        """The reason while the same daemon process is still running, None once it has changed.

        Also where recovery is retried: a failed, deferred or rate-limited restart gets another go
        from here once its wait is over, so the hold never becomes permanent by itself.
        """
        if time.monotonic() - self._fd_checked_at >= FD_EXHAUSTED_RECHECK_S:
            self._fd_checked_at = time.monotonic()
            pid = daemon_pid(await self._daemon_log_tail() or [])
            if pid is None or pid != self.fd_exhausted_pid:
                # A new process, or no way to tell: stop holding back. If it is still exhausted,
                # the next session fails and this state comes straight back.
                _LOG.info("robot daemon pid %s -> %s: trying the camera again",
                          self.fd_exhausted_pid, pid)
                self.fd_exhausted_pid, self._recovery_note = None, None
                self.failed_streak = 0
                self._backoff = MIN_BACKOFF_S
                return None
        if self.recovery["configured"] and time.monotonic() >= self._next_recover_at:
            await self._try_recovery()
        return self._fd_reason()

    async def _supervise(self) -> None:
        while True:
            try:
                reason, holder = await self.camera_unavailable()
                if reason:
                    if self.reason != reason:
                        _LOG.warning("%s - dormant, polling every %.0fs", reason, DORMANT_POLL_S)
                    self.state, self.reason, self.blocked_by = "dormant", reason, holder
                    await asyncio.sleep(DORMANT_POLL_S)
                    continue
                self.blocked_by = None
                lived = await self._run_session()
                if lived >= HEALTHY_AFTER_S:
                    self._backoff = MIN_BACKOFF_S
                self.failed_streak = 0 if lived > 0 else self.failed_streak + 1
                if lived == 0 and await self._note_fd_exhaustion():
                    continue   # dormant from here on; camera_unavailable() says why
                if self.failed_streak >= REJECTED_STREAK_WARN:
                    # Say what a human should do, not just what happened.
                    self.reason = (f"{self.failed_streak} sessions in a row without video "
                                   f"(last: {self.reason}). If this persists, restart the robot: "
                                   f"its daemon can run out of file descriptors.")
                self.restarts += 1
                _LOG.warning("%s - next session in %.0fs", self.reason, self._backoff)
                await asyncio.sleep(self._backoff)
                self._backoff = min(MAX_BACKOFF_S, self._backoff * 2)
            except asyncio.CancelledError:
                raise
            except Exception as e:   # the supervisor itself must never die
                self.state, self.reason = "error", f"supervisor: {type(e).__name__}: {e}"
                _LOG.exception("supervisor error")
                await asyncio.sleep(MAX_BACKOFF_S)

    async def _run_session(self) -> float:
        """Run one WebRTC session until it ends or stalls. Returns how long it was live."""
        self.sessions += 1
        self.state, self.reason = "connecting", None
        client = AVStreamClient(self.host, session=self.session, port=self.port,
                                on_audio=self.audio.feed)
        self.client = client
        started = time.monotonic()
        live_since = None
        seen = 0.0
        try:
            await client.acquire()
            while True:
                await asyncio.sleep(self.interval)
                now = time.monotonic()
                # Before any frame fetch: the upstream client drops its frame when the session ends,
                # so a last frame that arrived with the end cannot be fetched, and that failure
                # would be reported instead of why the session ended.
                if client.session_over:
                    self.reason = client.end_reason
                    break
                if client.last_video_at > seen:
                    seen = client.last_video_at
                    jpeg = await client.async_get_image(timeout=2)
                    self.frames.publish(jpeg)
                    if live_since is None:
                        live_since = now
                        _LOG.info("live: video flowing (session %d)", self.sessions)
                    self.state, self.reason = "live", None
                    continue
                if live_since is None and now - started > FIRST_FRAME_TIMEOUT:
                    self.reason = f"no video within {FIRST_FRAME_TIMEOUT:.0f}s of connecting"
                    break
                if live_since is not None and now - seen > STALL_AFTER:
                    self.reason = f"video stalled for {now - seen:.0f}s"
                    break
        except Exception as e:
            self.reason = f"session error: {type(e).__name__}: {e}"
        finally:
            self.state = "reconnecting"
            self.client = None
            try:
                await client.close()
            except Exception as e:
                _LOG.debug("close: %s", e)
        return 0.0 if live_since is None else time.monotonic() - live_since

    # ------------------------------------------------------------------ frame push (--push-url)
    async def _push_loop(self) -> None:
        """POST new frames to the --push-url receiver.

        The receiver may be stopped on purpose, so its absence is a state, not an error: say so
        once and probe slowly. A 409 means someone pressed Stop on that session at the receiver;
        it refuses frames until Start is pressed there, and re-creating the session behind their
        back would silently keep the VLM running, so this waits instead.
        """
        last_seq = 0
        while True:
            try:
                if not await self.frames.next(last_seq, timeout=10):
                    self._push_state("waiting for video", None)
                    continue
                # Taken before the freshness check, so a frame too old to push still counts as
                # seen. One that went stale while this loop slept (receiver down, and the video
                # stopped meanwhile) would otherwise satisfy next() at once on every pass; none of
                # those awaits suspends, so the loop would spin without yielding and freeze the
                # whole bridge, HTTP and supervisor included.
                last_seq, jpeg = self.frames.seq, self.frames.jpeg
                if not self.frames.fresh:
                    self._push_state("waiting for video", None)
                    continue
                t0 = time.monotonic()
                async with self.session.post(
                        self.push_url, data=jpeg, ssl=False,
                        headers={"Content-Type": "image/jpeg"},
                        timeout=aiohttp.ClientTimeout(total=10)) as r:
                    body = await r.text()
                    if r.status == 200:
                        self.push["pushed"] += 1
                        self._push_state("pushing", None)
                    elif r.status == 409:
                        self._push_state("stopped at the receiver; press Start there", body[:200])
                        await asyncio.sleep(PUSH_RETRY_S)
                        continue
                    else:
                        self._push_state(f"receiver answered HTTP {r.status}", body[:200])
                        await asyncio.sleep(PUSH_RETRY_S)
                        continue
                await asyncio.sleep(max(0.0, self.push_interval - (time.monotonic() - t0)))
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientConnectorError, aiohttp.ServerDisconnectedError,
                    asyncio.TimeoutError) as e:
                self._push_state("receiver not reachable (stopped?)", f"{type(e).__name__}: {e}")
                await asyncio.sleep(PUSH_UNREACHABLE_RETRY_S)
            except Exception as e:
                self._push_state("push error", f"{type(e).__name__}: {e}")
                await asyncio.sleep(PUSH_RETRY_S)

    def _push_state(self, state: str, detail: str | None) -> None:
        if self.push["state"] != state:
            _LOG.info("frame push: %s%s", state, f" ({detail})" if detail else "")
        self.push["state"], self.push["detail"] = state, detail

    # ------------------------------------------------------------------ HTTP
    async def mjpeg(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(status=200, headers={
            "Content-Type": "multipart/x-mixed-replace; boundary=frame",
            "Cache-Control": "no-store",
        })
        await resp.prepare(request)
        self.mjpeg_clients += 1
        # A fresh frame is shown immediately, then only NEW frames. A stale one is not shown at all:
        # a new viewer would take a picture from minutes ago for the live camera.
        seq = self.frames.seq - 1 if self.frames.fresh else self.frames.seq
        try:
            while True:
                if not await self.frames.next(seq, timeout=CLIENT_IDLE_CHECK_S):
                    if _client_gone(request):
                        break
                    continue
                seq, jpeg = self.frames.seq, self.frames.jpeg
                await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                 + f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
                                 + jpeg + b"\r\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.mjpeg_clients -= 1
        return resp

    async def still(self, request: web.Request) -> web.Response:
        if not self.frames.fresh:
            raise web.HTTPServiceUnavailable(text=f"no fresh frame ({self.state}: {self.reason})")
        return web.Response(body=self.frames.jpeg, content_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

    async def audio_mp3(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(status=200, headers={
            "Content-Type": "audio/mpeg", "Cache-Control": "no-store",
        })
        await resp.prepare(request)
        q = self.audio.subscribe()
        try:
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), CLIENT_IDLE_CHECK_S)
                except asyncio.TimeoutError:
                    if _client_gone(request):
                        break
                    continue
                await resp.write(data)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.audio.unsubscribe(q)
        return resp

    async def health(self, request: web.Request) -> web.Response:
        age = self.frames.age
        audio_age = time.monotonic() - self.audio.last_at if self.audio.last_at else None
        return web.json_response({
            "state": self.state,
            "reason": self.reason,
            "live": self.state == "live" and self.frames.fresh,
            # has_frame only vouches for a FRESH frame: a client may read it as "connected",
            # and a picture from minutes ago is not a connection.
            "has_frame": self.frames.fresh,
            "frames": self.frames.seq,
            "last_frame_age_s": None if age is None else round(age, 1),
            "stale_s": None if age is None else round(age, 1),
            "sessions": self.sessions,
            "restarts": self.restarts,
            "failed_streak": self.failed_streak,
            "recovery": self.recovery,
            "blocked_by": self.blocked_by,
            "mjpeg_clients": self.mjpeg_clients,
            "audio": {
                "live": audio_age is not None and audio_age < FRESH_FOR,
                "frames": self.audio.frames_in,
                "last_frame_age_s": None if audio_age is None else round(audio_age, 1),
                "listeners": self.audio.listeners,
            },
            "push": self.push,
        })


def daemon_pid(lines: list[str]) -> str | None:
    """The daemon's pid, from the newest journal line that carries one."""
    for line in reversed(lines):
        m = _DAEMON_PID.search(line)
        if m:
            return m.group(1)
    return None


def fd_exhausted(lines: list[str], pid: str | None = None) -> bool:
    """The tell-tale line, from process `pid` when given.

    On this robot every daemon journal line carries the Python daemon's own pid, whether journald
    tags it `launcher.sh[pid]` or `python[pid]` (seen 2026-09-25: launcher 106494, daemon 106501,
    lines `launcher.sh[106501]`), so one pid identifies one daemon process.
    """
    return any(FD_EXHAUSTED_MARK in line and (pid is None or (
        (m := _DAEMON_PID.search(line)) is not None and m.group(1) == pid)) for line in lines)


def fd_exhausted_reason(pid: str | None, recovery_note: str | None = None) -> str:
    """`recovery_note` is what automatic recovery is doing; without recovery, a person must act."""
    return (f"robot daemon (pid {pid}) is out of file descriptors - '{FD_EXHAUSTED_MARK}' in its "
            f"log. Every new viewer fails, and its failed setup stops the robot's camera for its "
            f"local apps too, so the bridge is not dialling. "
            + (recovery_note or "Power-cycle the robot; the bridge resumes on its own once the "
                                "daemon runs as a new process."))


def _client_gone(request: web.Request) -> bool:
    """True once a streaming client has disconnected.

    aiohttp does not cancel a handler whose client leaves; it only fails the next write. A stream
    with nothing to write (a dormant camera, a silent microphone) has no next write, and without
    this check each departed client stays counted, and its handler alive, until video returns.
    """
    transport = request.transport
    return transport is None or transport.is_closing()


def _address_missing(e: OSError) -> bool:
    """True when a bind failed because the address is not on this host (yet).

    Not just errno: asyncio's create_server (3.12) swallows EADDRNOTAVAIL per address, assuming the
    family is disabled, and then raises a plain OSError with no errno at all - "could not bind on
    any address out of [...]". Matching only the errno made a missing docker gateway at boot look
    like a real failure, and the bridge crash-looped under systemd instead of waiting for it.
    """
    return e.errno == errno.EADDRNOTAVAIL or (
        e.errno is None and "could not bind on any address" in str(e))


async def _bind(runner: web.AppRunner, host: str, port: int) -> None:
    """Bind one address, waiting for it to exist rather than dying.

    The docker gateway address only appears once dockerd is up. At boot this can start first,
    and a missing address is a reason to wait, not to crash-loop.
    """
    warned = False
    while True:
        site = web.TCPSite(runner, host, port)
        try:
            await site.start()
            _LOG.info("listening on http://%s:%d", host, port)
            return
        except OSError as e:
            # start() registers the site with the runner before it binds. Unregister the failed
            # one, or every retry leaves one behind for as long as the address is missing, which
            # without docker enabled at boot is indefinitely.
            await site.stop()
            if not _address_missing(e):
                raise
            if not warned:
                _LOG.warning("%s is not up yet; will bind it when it appears", host)
                warned = True
            await asyncio.sleep(BIND_RETRY_S)


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    # No built-in address: it's different on every LAN. The systemd unit passes REACHY_MINI_IP
    # from reachy.env (see scripts/set-reachy-ip.sh); run by hand, export it or pass the flag.
    p.add_argument("--robot-host", default=os.environ.get("REACHY_MINI_IP", ""),
                   help="Reachy Mini IP or hostname (default: $REACHY_MINI_IP)")
    p.add_argument("--robot-port", type=int, default=8443, help="WebRTC signalling port")
    p.add_argument("--daemon-port", type=int, default=8000, help="robot daemon REST port")
    p.add_argument("--listen", action="append",
                   help="bind address, repeatable (default 127.0.0.1); loopback keeps it local")
    p.add_argument("--listen-port", type=int, default=8099)
    p.add_argument("--fps", type=float, default=5.0, help="rate frames are published at")
    p.add_argument("--push-url", help="HTTP(S) endpoint to POST each new frame to, as image/jpeg")
    p.add_argument("--push-fps", type=float, default=0.0, help="push rate (default: --fps)")
    p.add_argument("--recover-ssh", help="user@robot whose forced-command key restarts the daemon "
                   "when it runs out of file descriptors (see setup_robot_recovery.sh)")
    p.add_argument("--recover-key", help="private key for --recover-ssh")
    args = p.parse_args()
    if not args.robot_host:
        p.error("no robot address: set REACHY_MINI_IP or pass --robot-host <the robot's IP or hostname>")

    bridge = Bridge(args.robot_host, args.robot_port, args.fps, args.daemon_port,
                    args.push_url, args.push_fps, args.recover_ssh, args.recover_key)
    await bridge.start()

    app = web.Application()
    app.router.add_get("/mjpeg", bridge.mjpeg)
    app.router.add_get("/still.jpg", bridge.still)
    app.router.add_get("/audio.mp3", bridge.audio_mp3)
    app.router.add_get("/healthz", bridge.health)

    runner = web.AppRunner(app)
    await runner.setup()
    _LOG.info("robot %s (signalling :%d, daemon :%d)%s", args.robot_host, args.robot_port,
              args.daemon_port, f", pushing to {args.push_url}" if args.push_url else "")
    try:
        # Any bind failure other than "address not up yet" (a port conflict, say) propagates and
        # exits the process, so systemd restarts it rather than leaving a bridge nobody can reach.
        await asyncio.gather(*(_bind(runner, h, args.listen_port)
                               for h in args.listen or ["127.0.0.1"]))
        await asyncio.Event().wait()
    finally:
        # On SIGTERM/SIGINT (see _run) and on a failed bind alike, the robot session ends first.
        _LOG.info("stopping: ending the robot session")
        await bridge.stop()


async def _run() -> None:
    """main(), cancelled cleanly on SIGTERM as well as SIGINT.

    systemd stops and restarts with SIGTERM, whose default action kills Python on the spot. The
    robot then keeps encoding for a consumer that no longer exists until ICE gives up on it, and a
    second session opened in that window can get audio but no video. Cancelling instead unwinds
    the session, which sends the robot an endSession.
    """
    import signal
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await main()
    except asyncio.CancelledError:
        _LOG.info("stopped")


if __name__ == "__main__":
    asyncio.run(_run())
