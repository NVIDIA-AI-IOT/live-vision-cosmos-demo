# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Reachy Mini control: motors, pose, onboard apps, volume, sound upload/play, over the robot's
own daemon REST API.

Talks to the robot's own daemon REST API rather than going through Home Assistant. Pollen's HA
integration exposes the same capabilities, but routing through it would make this page depend on
Home Assistant being up - and a demo UI's job is to work when other things are off.

Everything here is a thin wrapper over documented daemon endpoints. Angles are degrees at this
boundary and radians on the wire, matching the robot's published limits.
"""
from __future__ import annotations

import math
import time

import requests

MOTOR_MODES = ("enabled", "disabled", "gravity_compensation")

# Published safety limits (Reachy Mini "Core Concepts").
LIMITS_DEG = {"pitch": (-40.0, 40.0), "roll": (-40.0, 40.0),
              "yaw": (-180.0, 180.0), "body_yaw": (-160.0, 160.0)}
# Antennas park here rather than 0: at the null position a servo hunts and visibly twitches.
ANTENNA_PARK_DEG = 10.0

# Live-control limits, in the robot's own units (metres and radians) because set_target() is a
# direct passthrough to the daemon and converting twice is how sign errors get in.
#
# The translation envelope was MEASURED on the robot, not read from a document. The head is on a
# Stewart platform, and it saturates upward at about +0.019 m: commands of 0.02, 0.03 and 0.05 all
# return HTTP 200 and all leave the head at 0.0188. So the honest ceiling is lower than the daemon
# will cheerfully accept, and a slider allowed to reach 0.05 would feel broken over its top half.
# Held symmetric at +/-0.018 so the control behaves the same in both directions; the platform can
# actually go to about -0.03 downward, which is simply not used.
LIMITS_M = {"x": (-0.02, 0.02), "y": (-0.02, 0.02), "z": (-0.018, 0.018)}

LIMITS_RAD = {
    "roll": (math.radians(-40.0), math.radians(40.0)),
    "pitch": (math.radians(-40.0), math.radians(40.0)),
    "yaw": (math.radians(-180.0), math.radians(180.0)),
    "body_yaw": (math.radians(-160.0), math.radians(160.0)),
}
# Antennas are near-continuous: measured tracking to +/-3.0 rad with no complaint.
ANTENNA_LIMIT_RAD = math.pi

# Index 0 is the LEFT antenna and index 1 the RIGHT. Confirmed against the robot's own desktop app,
# which displayed Left 0.173 / Right 0.175 while the API returned [0.1733, 0.1749]. An earlier note
# in this project had these the other way round; it was wrong.
ANTENNA_LEFT, ANTENNA_RIGHT = 0, 1

# How long set_target() trusts its own last command when holding an unspecified axis. Long
# enough that a dragged control never falls back to the measured pose mid-drag, short enough
# that moving the head by hand is adopted rather than fought.
CMD_MEMORY_S = 5.0


class Reachy:
    """Minimal client for one Reachy Mini daemon."""

    def __init__(self, base_url: str, timeout: float = 8.0):
        self.base = (base_url or "").rstrip("/")
        self.timeout = timeout
        # A bare requests.get/post opens a fresh TCP connection every call - every state poll,
        # every motor command, every speak. Over WiFi to the robot that variance shows up as
        # occasional multi-second calls for no reason tied to what was actually asked ("still
        # sluggish" even once Piper itself is fast and warm). A Session keeps the connection to
        # this one host alive between calls (HTTP keep-alive, via urllib3's pooling) instead of
        # paying a fresh TCP/TLS-equivalent handshake on every single request.
        self.session = requests.Session()
        # Last pose commanded, so set_target() holds an axis at what it was ASKED to be
        # rather than at what the platform settled on. See set_target().
        self._last_cmd: dict = {}
        self._last_cmd_at = 0.0

    # ------------------------------------------------------------------ plumbing
    def _get(self, path: str, default=None):
        try:
            r = self.session.get(f"{self.base}{path}", timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            return default

    def _post(self, path: str, payload=None) -> tuple[bool, str]:
        try:
            r = self.session.post(f"{self.base}{path}", json=payload, timeout=self.timeout)
            if r.status_code >= 400:
                return False, f"HTTP {r.status_code}: {r.text[:120]}"
            return True, "ok"
        except requests.RequestException as e:
            return False, f"robot unreachable: {e}"

    # ------------------------------------------------------------------ read
    def state(self) -> dict:
        """Everything the panel shows, in one call-set. Never raises: the panel degrades instead."""
        status = self._get("/api/daemon/status") or {}
        if not status:
            return {"reachable": False}

        backend = status.get("backend_status") or {}
        out = {
            "reachable": True,
            "name": status.get("robot_name"),
            "version": status.get("version"),
            "state": status.get("state"),
            # `ready` is the robot's awake flag: False until woken, not an error.
            "awake": bool(backend.get("ready")),
            "motor_mode": backend.get("motor_control_mode"),
            "wireless": status.get("wireless_version"),
            "hardware_id": status.get("hardware_id"),
        }

        full = self._get("/api/state/full") or {}
        pose = full.get("head_pose") or {}
        if pose:
            out["pose_deg"] = {k: round(math.degrees(float(pose.get(k, 0.0))), 1)
                               for k in ("roll", "pitch", "yaw")}
            # Translation as well as rotation: the live panel has X/Y and Z controls, and without
            # these they would have nothing to snap back to when the robot is moved by anything
            # else. Metres, unrounded past mm, because the whole usable range is +/-20 mm.
            out["pos_m"] = {k: round(float(pose.get(k, 0.0)), 4) for k in ("x", "y", "z")}
            out["body_yaw_deg"] = round(math.degrees(float(full.get("body_yaw") or 0.0)), 1)
            out["antennas_deg"] = [round(math.degrees(float(a)), 1)
                                   for a in (full.get("antennas_position") or [])]

        doa = self._get("/api/state/doa") or {}
        if doa:
            # 0 rad is left, pi/2 front/back, pi right.
            out["doa_deg"] = round(math.degrees(float(doa.get("angle") or 0.0)), 1)
            out["speech_detected"] = bool(doa.get("speech_detected"))

        vol = self._get("/api/volume/current") or {}
        mic = self._get("/api/volume/microphone/current") or {}
        out["speaker_volume"] = vol.get("volume")
        out["mic_volume"] = mic.get("volume")

        # current-app-status nests the app under "info" and carries its lifecycle in "state".
        # Reading "name" off the top level silently reports no app while one is plainly
        # running, which is exactly how it behaved until this was measured against the daemon.
        app = self._get("/api/apps/current-app-status")
        if isinstance(app, dict):
            out["app"] = ((app.get("info") or {}).get("name"))
            out["app_state"] = app.get("state")
            out["app_error"] = app.get("error")
        else:
            out["app"] = None
        out["move_running"] = bool(self._get("/api/move/running") or [])

        # Who actually holds the robot. The daemon grants the lock to one app at a time, and
        # whoever holds it owns the camera and the microphone - which is why the camera card goes
        # dark while a conversation app runs. Worth stating plainly rather than leaving someone to
        # work it out from a black preview.
        lock = self._get("/api/daemon/robot-app-lock-status") or {}
        out["lock_state"] = lock.get("state")
        out["lock_holder"] = lock.get("holder_name")

        loop = backend.get("control_loop_stats") or {}
        out["control_hz"] = round(float(loop.get("mean_control_loop_frequency") or 0.0), 1)
        out["control_errors"] = loop.get("nb_error")
        return out


    def wake(self):
        return self._post("/api/move/play/wake_up")

    def sleep(self):
        return self._post("/api/move/play/goto_sleep")


    def set_motor_mode(self, mode: str):
        if mode not in MOTOR_MODES:
            return False, f"mode must be one of {', '.join(MOTOR_MODES)}"
        return self._post(f"/api/motors/set_mode/{mode}")

    def set_volume(self, which: str, value: int):
        value = max(0, min(100, int(value)))
        path = "/api/volume/set" if which == "speaker" else "/api/volume/microphone/set"
        ok, msg = self._post(path, {"volume": value})
        return ok, (f"{which} volume {value}%" if ok else msg)

    def upload_sound(self, data: bytes, filename: str) -> tuple[bool, str]:
        """POST a sound file to the daemon's temp sound directory. Returns (ok, remote path on
        success or error message on failure). Bypasses _post: this is multipart, not JSON."""
        try:
            r = self.session.post(f"{self.base}/api/media/sounds/upload",
                               files={"file": (filename, data, "audio/wav")}, timeout=self.timeout)
            if r.status_code >= 400:
                return False, f"HTTP {r.status_code}: {r.text[:120]}"
            path = (r.json() or {}).get("path") or filename
            return True, path
        except (requests.RequestException, ValueError) as e:
            return False, f"robot unreachable: {e}"

    def play_sound(self, file: str) -> tuple[bool, str]:
        """Play a sound file already on the daemon (an absolute path, or one just uploaded via
        upload_sound) on the robot's speaker."""
        return self._post("/api/media/play_sound", {"file": file})


    def look(self, pitch=None, yaw=None, roll=None, body_yaw=None, duration=1.0):
        """Move the head. Degrees in, radians out. Omitted axes hold their current value.

        Positive pitch tilts the head DOWN - verified from the camera, not assumed - so a caller
        meaning "look up" sends a negative number.
        """
        st = self.state()
        if not st.get("reachable"):
            return False, "robot unreachable"
        if (st.get("motor_mode") or "").lower() == "disabled":
            # The daemon accepts moves with motors off and reports success while nothing turns.
            return False, "motors are disabled - enable them first"

        cur = st.get("pose_deg") or {}
        vals = {
            "pitch": cur.get("pitch", 0.0) if pitch is None else float(pitch),
            "roll": cur.get("roll", 0.0) if roll is None else float(roll),
            "yaw": cur.get("yaw", 0.0) if yaw is None else float(yaw),
            "body_yaw": (st.get("body_yaw_deg") or 0.0) if body_yaw is None else float(body_yaw),
        }
        notes = []
        for k, v in vals.items():
            lo, hi = LIMITS_DEG[k]
            c = max(lo, min(hi, v))
            if c != v:
                notes.append(f"{k} clamped to {c:g}°")
            vals[k] = c

        payload = {
            "head_pose": {"x": 0.0, "y": 0.0, "z": 0.0,
                          "roll": math.radians(vals["roll"]),
                          "pitch": math.radians(vals["pitch"]),
                          "yaw": math.radians(vals["yaw"])},
            "body_yaw": math.radians(vals["body_yaw"]),
            "duration": max(0.2, min(5.0, float(duration))),
            "interpolation": "minjerk",
        }
        ok, msg = self._post("/api/move/goto", payload)
        if not ok:
            return False, msg
        return True, "; ".join(notes) or "moving"

    def set_target(self, pose: dict | None = None, body_yaw=None, antennas=None):
        """Point the robot at a target and return immediately. The unit for live controls.

        Deliberately not goto(). goto() plans an interpolated move of a given duration, which is
        right for "centre yourself" and wrong for a slider: dragging one would queue a backlog of
        overlapping trajectories and the head would lag behind the finger and then catch up in
        lurches. set_target() just updates the setpoint that the robot's own 50 Hz loop is already
        chasing, so the motion is as smooth as the loop and the newest value always wins.

        Angles are RADIANS and positions METRES here - the daemon's own units - because this is a
        passthrough and a second conversion is an opportunity for a sign error.
        """
        st = self.state()
        if not st.get("reachable"):
            return False, "robot unreachable"
        if (st.get("motor_mode") or "").lower() == "disabled":
            # The daemon accepts targets with motors off and reports success while nothing turns.
            return False, "motors are disabled - enable them first"

        payload: dict = {}
        notes = []

        if pose:
            vals = {}
            for k in ("x", "y", "z"):
                if pose.get(k) is not None:
                    lo, hi = LIMITS_M[k]
                    v = float(pose[k])
                    c = max(lo, min(hi, v))
                    if abs(c - v) > 1e-9:
                        notes.append(f"{k} clamped to {c:.3f} m")
                    vals[k] = c
            for k in ("roll", "pitch", "yaw"):
                if pose.get(k) is not None:
                    lo, hi = LIMITS_RAD[k]
                    v = float(pose[k])
                    c = max(lo, min(hi, v))
                    if abs(c - v) > 1e-9:
                        notes.append(f"{k} clamped to {math.degrees(c):.0f}°")
                    vals[k] = c
            if vals:
                # The daemon wants a complete pose, so unspecified axes have to be filled in.
                #
                # They are filled from the LAST COMMANDED pose, not the measured one. Measuring
                # looks more correct and is not: this is a Stewart platform that settles a degree
                # or two off target, so feeding the measurement back as the next command amplifies
                # that error every call. Measured live, roll walked 3.7 -> 5.1 -> 8.6 -> 10.0 over
                # four requests that never mentioned roll. Falling back to the measured pose only
                # when the cache is stale means a head moved by hand, or by another client, is
                # still picked up instead of fought.
                cur = st.get("pose_deg") or {}
                pos = st.get("pos_m") or {}
                fresh = (time.monotonic() - self._last_cmd_at) < CMD_MEMORY_S
                held = self._last_cmd if fresh else {}

                def hold(key, measured):
                    if key in vals:
                        return vals[key]
                    return float(held.get(key, measured))

                full = {
                    "x": hold("x", float(pos.get("x", 0.0))),
                    "y": hold("y", float(pos.get("y", 0.0))),
                    "z": hold("z", float(pos.get("z", 0.0))),
                    "roll": hold("roll", math.radians(cur.get("roll", 0.0))),
                    "pitch": hold("pitch", math.radians(cur.get("pitch", 0.0))),
                    "yaw": hold("yaw", math.radians(cur.get("yaw", 0.0))),
                }
                payload["target_head_pose"] = full

        if body_yaw is not None:
            lo, hi = LIMITS_RAD["body_yaw"]
            v = float(body_yaw)
            c = max(lo, min(hi, v))
            if abs(c - v) > 1e-9:
                notes.append(f"body_yaw clamped to {math.degrees(c):.0f}°")
            payload["target_body_yaw"] = c

        if antennas is not None:
            a = [max(-ANTENNA_LIMIT_RAD, min(ANTENNA_LIMIT_RAD, float(x))) for x in antennas]
            if len(a) != 2:
                return False, "antennas must be [left, right]"
            payload["target_antennas"] = a

        if not payload:
            return False, "nothing to set"
        ok, msg = self._post("/api/move/set_target", payload)
        if not ok:
            return False, msg
        if "target_head_pose" in payload:
            self._last_cmd = dict(payload["target_head_pose"])
            self._last_cmd_at = time.monotonic()
        return True, "; ".join(notes) or "ok"

    def center(self):
        st = self.state()
        if (st.get("motor_mode") or "").lower() == "disabled":
            return False, "motors are disabled - enable them first"
        payload = {
            "head_pose": {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
            "body_yaw": 0.0,
            "antennas": [math.radians(ANTENNA_PARK_DEG)] * 2,
            "duration": 1.0,
            "interpolation": "minjerk",
        }
        ok, msg = self._post("/api/move/goto", payload)
        return ok, ("centred" if ok else msg)

    # ------------------------------------------------------------------ apps
    #
    # The robot runs ONE app at a time, and the daemon owns their lifecycle - installing from a
    # Hugging Face Space into a venv on the robot, starting, stopping. This is how a conversation
    # app gets switched on without SSHing into the robot, so this UI exposes it.
    #
    # Only installed apps are listed. The daemon will also happily list the ~470 published Spaces,
    # which is a catalogue to browse, not a control surface, and putting it on this page would bury
    # the dozen apps that are actually here.
    def apps(self) -> dict:
        installed = self._get("/api/apps/list-available/installed") or []
        current = self._get("/api/apps/current-app-status")
        startup = (self._get("/api/apps/startup-app") or {}).get("startup_app")
        running, state, err = None, None, None
        if isinstance(current, dict):
            running = (current.get("info") or {}).get("name")
            state = current.get("state")
            err = current.get("error")
        return {
            "installed": [{"name": a.get("name"),
                           "url": ((a.get("extra") or {}).get("custom_app_url") or "").replace(
                               "0.0.0.0", self.base.split("//")[-1].split(":")[0])}
                          for a in installed if a.get("name")],
            "running": running,
            # An app that died reports state "error" with a traceback. Surfacing it is the
            # difference between "nothing happened" and a diagnosable failure.
            "state": state,
            "error": (err or "").strip()[:400] or None,
            "startup": startup,
        }

    def start_app(self, name: str):
        """Start an installed app. The daemon stops whatever was running first."""
        if not name or "/" in name:
            return False, "bad app name"
        ok, msg = self._post(f"/api/apps/start-app/{name}")
        return ok, (f"starting {name}" if ok else msg)

    def stop_app(self):
        ok, msg = self._post("/api/apps/stop-current-app")
        return ok, ("stopped" if ok else msg)

    def look_at_voice(self):
        """Turn the BASE toward the last detected sound, not just the head.

        A head-only yaw is a subtle tilt on the Stewart platform - not a convincing "it turned
        toward the noise" for a listener (a 3D printer, a person talking). body_yaw physically
        rotates the robot instead. Head yaw resets to 0 in the same move, so the camera ends up
        centered on whatever the base just turned toward rather than still offset from wherever
        the head last pointed.

        DoA is reported as 0 rad = left, pi/2 = front/back, pi = right, so the useful yaw is the
        angle mapped onto the robot's own yaw convention - the same mapping already proven correct
        for the head, reused here since both are yaw rotations about the same vertical axis.
        """
        st = self.state()
        if st.get("doa_deg") is None:
            return False, "no direction-of-arrival reading available"
        yaw = round(90.0 - float(st["doa_deg"]), 1)
        ok, msg = self.look(yaw=0.0, body_yaw=yaw, duration=1.2)
        return ok, (f"turning base to {yaw:g}° (voice at {st['doa_deg']:g}°)" if ok else msg)
