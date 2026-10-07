#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The /reachy/ relay in serve_ui.py, against a fake bridge. Stdlib only, no robot involved.

Run: python3 -m unittest discover -s ui/tests   (or run this file directly)

The fake speaks the way reachy_mjpeg_bridge.py (aiohttp) does where it matters: a JSON health
answer, a 503 text answer when there is no fresh frame, an MJPEG stream that ends when the
connection closes, and chunked MP3 audio. It can also hold a stream open, or open one and send
nothing, to exercise the concurrency cap, disconnect propagation and the idle timeout.
"""

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import select
import socket
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

SERVE_UI = Path(__file__).resolve().parents[1] / "scripts" / "serve_ui.py"
spec = importlib.util.spec_from_file_location("serve_ui", SERVE_UI)
serve_ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve_ui)

JPEG = b"\xff\xd8\xff\xe0" + b"reachy-frame" * 200 + b"\xff\xd9"
HEALTH = {"state": "live", "reason": None, "live": True, "has_frame": True, "frames": 42,
          "last_frame_age_s": 0.1, "stale_s": 0.1, "sessions": 1, "restarts": 0, "failed_streak": 0,
          "blocked_by": None, "mjpeg_clients": 1,
          "audio": {"live": True, "frames": 900, "last_frame_age_s": 0.0, "listeners": 0},
          "push": {"enabled": False, "state": "idle", "pushed": 0, "detail": None}}
MJPEG_FRAMES = 3
MJPEG = b"".join(b"--frame\r\nContent-Type: image/jpeg\r\n"
                 + f"Content-Length: {len(JPEG)}\r\n\r\n".encode() + JPEG + b"\r\n"
                 for _ in range(MJPEG_FRAMES))
AUDIO_CHUNKS = [b"\xff\xfb\x90\x64" + bytes([n]) * 400 for n in range(4)]
REACHY_ROUTES = ("/reachy/healthz", "/reachy/still.jpg", "/reachy/mjpeg", "/reachy/audio.mp3")


class FakeBridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def wait_for_client_to_leave(self):
        # What the bridge sees when the relay lets go of it: EOF on its socket.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if select.select([self.connection], [], [], 0.05)[0]:
                try:
                    if not self.connection.recv(1):
                        break
                except OSError:
                    break
        self.server.note_closed(self.path)
        self.close_connection = True

    def do_GET(self):
        bridge = self.server
        bridge.paths.append(self.path)
        if self.path == "/healthz":
            self.reply(200, "application/json; charset=utf-8", json.dumps(HEALTH).encode())
        elif self.path == "/still.jpg":
            if bridge.still_status == 503:
                self.reply(503, "text/plain; charset=utf-8",
                           b"no fresh frame (dormant: camera held by the robot app 'dance')")
            else:
                self.reply(200, bridge.still_type, bridge.still_body)
        elif self.path == "/mjpeg":
            # Like aiohttp answering HTTP/1.0-style: no length, the end is the connection closing.
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if bridge.silent:
                self.wait_for_client_to_leave()
                return
            self.wfile.write(MJPEG)
        elif self.path == "/audio.mp3":
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunks = AUDIO_CHUNKS[:1] if bridge.hold else AUDIO_CHUNKS
            for chunk in chunks:
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            if bridge.hold:
                self.wait_for_client_to_leave()
                return
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.reply(404, "text/plain; charset=utf-8", b"404: Not Found")


class FakeBridge(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeBridgeHandler)
        self.paths = []
        self.still_status = 200
        self.still_type = "image/jpeg"
        self.still_body = JPEG
        self.silent = False
        self.hold = False
        self.closed = []
        self.closed_changed = threading.Condition()

    def note_closed(self, path):
        with self.closed_changed:
            self.closed.append(path)
            self.closed_changed.notify_all()

    def wait_closed(self, count, timeout=5):
        with self.closed_changed:
            return self.closed_changed.wait_for(lambda: len(self.closed) >= count, timeout)


class StubTelemetry:
    def snapshot(self):
        return {}

    def close(self):
        pass


class QuietHandler(serve_ui.Handler):
    def log_message(self, *args):
        pass


def unused_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


class ReachyRelayTest(unittest.TestCase):
    def setUp(self):
        # A fresh cap per test, so one failing test cannot starve the rest of slots.
        self.slots = mock.patch.object(serve_ui, "REACHY_STREAM_SLOTS",
                                       threading.BoundedSemaphore(serve_ui.MAX_REACHY_STREAMS))
        self.slots.start()
        self.open_count = mock.patch.object(serve_ui, "OPEN_REACHY_STREAMS", 0)
        self.open_count.start()
        self.bridge = FakeBridge()
        serve(self.bridge)
        self.ui = serve_ui.Server(("127.0.0.1", 0), QuietHandler, telemetry=StubTelemetry())
        self.ui.backend_port = unused_port()
        self.ui.reachy_address = ("127.0.0.1", self.bridge.server_address[1])
        serve(self.ui)
        self.port = self.ui.server_address[1]
        self.open_connections = []
        self.open_responses = []

    def tearDown(self):
        # The response, not the connection, owns the socket of a Connection: close answer.
        for response in self.open_responses:
            response.close()
        for connection in self.open_connections:
            connection.close()
        # Let the relay's handlers give their slots back while this test's cap and count are
        # still patched in: a late release would otherwise land on the next test's.
        deadline = time.monotonic() + 3
        while serve_ui.OPEN_REACHY_STREAMS and time.monotonic() < deadline:
            time.sleep(0.02)
        self.ui.shutdown()
        self.ui.server_close()
        self.bridge.shutdown()
        self.bridge.server_close()
        self.open_count.stop()
        self.slots.stop()

    def get(self, path, headers=None, token=True):
        # /reachy/ URLs carry the relay token, as the page's do. token=None sends none, and a
        # string is sent in its place.
        if path.startswith("/reachy/") and token is not None:
            value = serve_ui.RELAY_TOKEN if token is True else token
            path += ("&" if "?" in path else "?") + urlencode({"token": value})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.open_connections.append(connection)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        self.open_responses.append(response)
        return connection, response

    def get_json(self, path, headers=None, token=True):
        _, response = self.get(path, headers, token)
        return response.status, response.getheader("Content-Type"), json.loads(response.read())

    def slots_header(self):
        _, response = self.get("/reachy/healthz")
        self.assertEqual(response.status, 200)
        response.read()
        return response.getheader("X-Reachy-Slots")

    def wait_for_slots(self, expected, timeout=3):
        # A slot comes back just after the bridge socket closes, not with it.
        deadline = time.monotonic() + timeout
        while self.slots_header() != expected and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.slots_header()

    def open_held_audio(self):
        connection, response = self.get("/reachy/audio.mp3")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read1(65536), AUDIO_CHUNKS[0])
        return connection, response

    # ------------------------------------------------------------------ bounded answers
    def test_healthz_is_relayed(self):
        status, content_type, body = self.get_json("/reachy/healthz")
        self.assertEqual((status, content_type, body), (200, "application/json", HEALTH))

    def test_still_is_relayed(self):
        _, response = self.get("/reachy/still.jpg")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "image/jpeg")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.read(), JPEG)

    def test_no_fresh_frame_503_passes_through_with_the_bridges_reason(self):
        self.bridge.still_status = 503
        status, _, body = self.get_json("/reachy/still.jpg")
        self.assertEqual(status, 503)
        self.assertIn("camera held by the robot app 'dance'", body["error"]["message"])

    def test_unexpected_content_type_is_refused(self):
        self.bridge.still_type = "text/html"
        status, _, body = self.get_json("/reachy/still.jpg")
        self.assertEqual(status, 502)
        self.assertIn("content type", body["error"]["message"])

    def test_oversized_answer_is_refused(self):
        with mock.patch.object(serve_ui, "MAX_RESPONSE", len(JPEG) - 1):
            status, _, body = self.get_json("/reachy/still.jpg")
        self.assertEqual(status, 502)
        self.assertIn("size limit", body["error"]["message"])

    # ------------------------------------------------------------------ streams
    def test_mjpeg_is_relayed_until_the_bridge_ends_it(self):
        _, response = self.get("/reachy/mjpeg?open=7")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "multipart/x-mixed-replace; boundary=frame")
        self.assertEqual(response.read(), MJPEG)
        # The cache-busting query stays on this side.
        self.assertEqual(self.bridge.paths, ["/mjpeg"])

    def test_chunked_audio_is_relayed_as_plain_bytes(self):
        _, response = self.get("/reachy/audio.mp3?open=8")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "audio/mpeg")
        self.assertIsNone(response.getheader("Transfer-Encoding"))
        self.assertEqual(response.read(), b"".join(AUDIO_CHUNKS))

    def test_closing_the_browser_side_closes_the_bridge_side(self):
        self.bridge.hold = True
        connection, response = self.open_held_audio()
        response.close()
        connection.close()
        self.assertTrue(self.bridge.wait_closed(1, timeout=3), "bridge never saw the relay let go")

    def test_health_says_whether_the_pages_stream_is_still_relayed(self):
        # What lets the page notice a stream that ended cleanly: an <img> itself never says.
        def relay_header(token):
            _, response = self.get(f"/reachy/healthz?stream={token}" if token else "/reachy/healthz")
            self.assertEqual(response.status, 200)
            response.read()
            return response.getheader("X-Reachy-Stream")

        self.bridge.hold = True
        token = "teststream01"  # Synthetic stream correlation ID; not an authentication token.
        connection, response = self.get(f"/reachy/audio.mp3?stream={token}")
        self.assertEqual(response.read1(65536), AUDIO_CHUNKS[0])
        self.assertRegex(relay_header(token), r"^idle_ms=\d+$")
        self.assertEqual(relay_header("someoneelse1"), "closed")
        self.assertIsNone(relay_header(None))
        self.assertIsNone(relay_header("short"))            # not a token: no answer at all
        self.assertIsNone(relay_header("bad%20token%20x"))
        response.close()
        connection.close()
        self.assertTrue(self.bridge.wait_closed(1, timeout=3))
        deadline = time.monotonic() + 3
        while relay_header(token) != "closed" and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(relay_header(token), "closed")
        self.assertEqual(serve_ui.RELAYED_STREAMS, {})

    def test_idle_stream_is_dropped(self):
        self.bridge.silent = True
        with mock.patch.object(serve_ui, "STREAM_IDLE_SECONDS", 0.5):
            started = time.monotonic()
            _, response = self.get("/reachy/mjpeg")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(self.bridge.wait_closed(1, timeout=3))

    def test_streams_are_capped_and_slots_come_back(self):
        self.bridge.hold = True
        held = [self.open_held_audio() for _ in range(serve_ui.MAX_REACHY_STREAMS)]
        status, _, body = self.get_json("/reachy/mjpeg")
        self.assertEqual(status, 503)
        self.assertIn("already open", body["error"]["message"])
        # Health and inference are not behind the stream cap, and streams never hold inference.
        self.assertEqual(self.get_json("/reachy/healthz")[0], 200)
        self.assertTrue(serve_ui.GENERATION_LOCK.acquire(blocking=False))
        serve_ui.GENERATION_LOCK.release()

        connection, response = held.pop()
        response.close()
        connection.close()
        self.assertTrue(self.bridge.wait_closed(1, timeout=3))
        deadline = time.monotonic() + 3
        while True:   # the slot is returned just after the bridge socket closes
            connection, response = self.get("/reachy/audio.mp3")
            if response.status == 200 or time.monotonic() > deadline:
                break
            response.read()
            time.sleep(0.05)
        self.assertEqual(response.status, 200)

    def test_health_counts_the_open_stream_slots(self):
        # What lets a page whose <img> was refused tell a full cap from a broken bridge.
        cap = serve_ui.MAX_REACHY_STREAMS
        self.assertEqual(self.slots_header(), f"0/{cap}")
        self.bridge.hold = True
        held = [self.open_held_audio() for _ in range(2)]
        self.assertEqual(self.slots_header(), f"2/{cap}")
        held += [self.open_held_audio() for _ in range(cap - 2)]
        self.assertEqual(self.slots_header(), f"{cap}/{cap}")
        connection, response = held.pop()
        response.close()
        connection.close()
        self.assertEqual(self.wait_for_slots(f"{cap - 1}/{cap}"), f"{cap - 1}/{cap}")
        _, response = self.get("/reachy/still.jpg")       # health alone carries it
        response.read()
        self.assertIsNone(response.getheader("X-Reachy-Slots"))

    def test_a_replaced_stream_hands_its_slot_to_the_new_one(self):
        # The page replacing an <img> src: the old stream closes and the new one asks at once,
        # before the old one's watcher has noticed. Its slot is handed over, not refused.
        cap = serve_ui.MAX_REACHY_STREAMS
        self.bridge.hold = True
        held = [self.open_held_audio() for _ in range(cap)]
        connection, response = held.pop()
        response.close()
        connection.close()
        _, response = self.get("/reachy/audio.mp3")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read1(65536), AUDIO_CHUNKS[0])
        self.assertEqual(self.slots_header(), f"{cap}/{cap}")
        # With nobody leaving, a full cap still refuses, after the short wait and no longer.
        started = time.monotonic()
        status, _, body = self.get_json("/reachy/mjpeg")
        waited = time.monotonic() - started
        self.assertEqual(status, 503)
        self.assertIn("already open", body["error"]["message"])
        self.assertGreaterEqual(waited, serve_ui.STREAM_HANDOFF_SECONDS * 0.9)
        self.assertLess(waited, serve_ui.STREAM_HANDOFF_SECONDS + 2)

    # ------------------------------------------------------------------ bridge down
    def test_unreachable_bridge_is_502_and_leaks_no_slot(self):
        self.ui.reachy_address = ("127.0.0.1", unused_port())
        for path in ("/reachy/healthz", "/reachy/still.jpg"):
            status, content_type, body = self.get_json(path)
            self.assertEqual((status, content_type), (502, "application/json"))
            self.assertEqual(body["error"]["message"], "Reachy bridge is not reachable.")
        for _ in range(serve_ui.MAX_REACHY_STREAMS + 2):
            for path in ("/reachy/mjpeg", "/reachy/audio.mp3"):
                self.assertEqual(self.get_json(path)[0], 502, path)

    # ------------------------------------------------------------------ who may ask
    def test_cross_origin_requests_are_refused(self):
        evil = {"Origin": "http://evil.example"}
        for path in ("/reachy/healthz", "/reachy/still.jpg", "/reachy/mjpeg", "/reachy/audio.mp3"):
            self.assertEqual(self.get_json(path, evil)[0], 403, path)
        same = {"Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(self.get_json("/reachy/healthz", same)[0], 200)
        self.assertEqual(self.bridge.paths, ["/healthz"])

    def test_cross_site_embedding_is_refused(self):
        # An <img>/<audio> on another site sends no Origin; Fetch Metadata still says where it is from.
        for site in ("cross-site", "same-site"):
            for path in ("/reachy/mjpeg", "/reachy/audio.mp3", "/reachy/still.jpg"):
                self.assertEqual(self.get_json(path, {"Sec-Fetch-Site": site})[0], 403, (site, path))
        self.assertEqual(self.bridge.paths, [])
        for site in ("same-origin", "none"):
            _, response = self.get("/reachy/still.jpg", {"Sec-Fetch-Site": site})
            self.assertEqual(response.status, 200, site)

    def test_every_route_needs_the_relay_token(self):
        # Plain HTTP carries no Fetch Metadata, so over :8091 the token is the lock.
        wrong = (None, "", "wrong", serve_ui.RELAY_TOKEN[:-1], serve_ui.RELAY_TOKEN + "x", "é" * 32)
        for path in REACHY_ROUTES:
            for token in wrong:
                status, content_type, _ = self.get_json(path, token=token)
                self.assertEqual((status, content_type), (401, "application/json"), (path, token))
            # One token, not a list to pick the right one from.
            self.assertEqual(self.get_json(f"{path}?token=wrong")[0], 401, path)
        self.assertEqual(self.bridge.paths, [])
        for path in REACHY_ROUTES:
            _, response = self.get(path)
            self.assertEqual(response.status, 200, path)
            response.read()
        self.assertEqual(self.bridge.paths, ["/healthz", "/still.jpg", "/mjpeg", "/audio.mp3"])

    def test_a_rebound_host_name_is_misdirected(self):
        # DNS rebinding: the attacker's own name, now resolving to the Orin. The browser calls
        # that same-origin, so neither Origin nor Fetch Metadata objects; Host still names it.
        for host in ("rebind.attacker.example", f"rebind.attacker.example:{self.port}", "orin.local:8091"):
            for path in REACHY_ROUTES:
                status, content_type, _ = self.get_json(path, {"Host": host})
                self.assertEqual((status, content_type), (421, "application/json"), (host, path))
            _, response = self.get("/api/access", {"Host": host})
            self.assertEqual(response.status, 421, host)
            self.assertNotIn(serve_ui.RELAY_TOKEN.encode(), response.read())
        self.assertEqual(self.bridge.paths, [])

    def test_access_hands_the_token_only_to_this_servers_own_names(self):
        self.ui.https_port = 8443
        expected = {"https_port": 8443, "reachy_token": serve_ui.RELAY_TOKEN}
        for host in (f"127.0.0.1:{self.port}", "192.0.2.10:8091", "192.0.2.10", "[::1]:8091", "LocalHost",
                     "[2001:db8::1]:8443", "localhost:8091", "reachy.localhost:8091", "localhost."):
            self.assertEqual(self.get_json("/api/access", {"Host": host})[::2], (200, expected), host)
        # Any other name only once the operator lists it with --allowed-host.
        self.assertEqual(self.get_json("/api/access", {"Host": "orin.local:8091"})[0], 421)
        self.ui.allowed_hosts = frozenset({"orin.local"})
        for host in ("orin.local:8091", "ORIN.local", "orin.local."):
            self.assertEqual(self.get_json("/api/access", {"Host": host})[::2], (200, expected), host)
        self.assertEqual(self.get_json("/reachy/healthz", {"Host": "orin.local:8091"})[0], 200)
        for host in ("user@127.0.0.1", "127.0.0.1:99999", "orin.local/x", "orin.local.evil.example"):
            self.assertEqual(self.get_json("/api/access", {"Host": host})[0], 421, host)

    # ------------------------------------------------------------------ page links
    def test_reachy_link_is_served_without_the_camera_upgrade(self):
        # As a LAN browser asks: loopback is already a secure context and never upgraded.
        self.ui.https_port = 8443
        lan = {"Host": "192.0.2.10:8091"}
        _, response = self.get("/", lan)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.getheader("Location"), "https://192.0.2.10:8443/")
        _, response = self.get("/?source=reachy", lan)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/html; charset=utf-8")
        self.assertTrue(b'id="reachyImage"' in response.read(), "the page has no Reachy preview")


class BridgeAddressTest(unittest.TestCase):
    def test_accepts_plain_http(self):
        self.assertEqual(serve_ui.bridge_address("http://127.0.0.1:8099"), ("127.0.0.1", 8099))
        self.assertEqual(serve_ui.bridge_address("http://192.0.2.1:8099/"), ("192.0.2.1", 8099))
        self.assertEqual(serve_ui.bridge_address("http://localhost"), ("localhost", 80))

    def test_rejects_anything_else(self):
        for url in ("https://127.0.0.1:8099", "127.0.0.1:8099", "http://127.0.0.1:8099/mjpeg",
                    "http://user@127.0.0.1:8099", "http://127.0.0.1:99999", "http://:8099",
                    "http://127.0.0.1:8099?x=1"):
            with self.assertRaises(ValueError, msg=url):
                serve_ui.bridge_address(url)


class HostTest(unittest.TestCase):
    def test_camera_redirect_is_unchanged(self):
        # camera_redirect_url now shares parse_host() with host_allowed().
        cases = {("192.0.2.10:8091", 8443): "https://192.0.2.10:8443/",
                 ("orin.local", 8443): "https://orin.local:8443/",
                 ("[2001:db8::1]:8091", 8443): "https://[2001:db8::1]:8443/",
                 ("localhost:8091", 8443): None, ("app.localhost", 8443): None,
                 ("127.0.0.1:8091", 8443): None, ("[::1]:8091", 8443): None,
                 ("bad host", None): None}
        for (host, port), expected in cases.items():
            self.assertEqual(serve_ui.camera_redirect_url(host, port), expected, host)
        for host in (None, "", "bad host", "a\\b", "user@192.0.2.10", "192.0.2.10:99999", "-x.example"):
            with self.assertRaises(ValueError, msg=host):
                serve_ui.camera_redirect_url(host, 8443)

    def test_only_names_that_cannot_be_rebound_are_allowed(self):
        listed = frozenset({"orin.local"})
        for host in ("192.0.2.10", "192.0.2.10:8091", "[::1]:8091", "localhost", "a.localhost:1",
                     "orin.local", "Orin.Local.:8443"):
            self.assertTrue(serve_ui.host_allowed(host, listed), host)
        for host in (None, "", "evil.example", "orin.local.evil.example", "localhost.evil.example",
                     "user@orin.local", "orin.local:0x1f90", "orin local"):
            self.assertFalse(serve_ui.host_allowed(host, listed), host)
        self.assertFalse(serve_ui.host_allowed("orin.local"))

    def test_allowed_host_option(self):
        self.assertEqual(serve_ui.allowed_host_name(" Orin.Local. "), "orin.local")
        for value in ("", ".", "orin.local:8091", "http://orin.local", "orin local", "-orin", "*.local"):
            with self.assertRaises(ValueError, msg=value):
                serve_ui.allowed_host_name(value)


class LogTest(unittest.TestCase):
    def test_the_relay_token_stays_out_of_the_journal(self):
        lines = []
        handler = serve_ui.Handler.__new__(serve_ui.Handler)   # no socket needed to log
        handler.path = f"/reachy/mjpeg?stream=abcdefgh&token={serve_ui.RELAY_TOKEN}"
        handler.requestline = f"GET {handler.path} HTTP/1.1"
        handler.log_message = lambda fmt, *args: lines.append(fmt % args)
        handler.log_request(200)
        self.assertEqual(lines, ['"GET /reachy/mjpeg?stream=abcdefgh&token=<redacted> HTTP/1.1" 200 -'])


if __name__ == "__main__":
    unittest.main()
