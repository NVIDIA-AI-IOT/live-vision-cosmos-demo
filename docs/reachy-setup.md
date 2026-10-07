# Optional Reachy Mini setup

Live Vision works with a browser camera and no robot. The installer prepares
Reachy's camera/microphone bridge and Piper speech dependencies either way.
Choose a robot when ready; skipping does not require another engine build later.
Home Assistant is not required.

## Choose during installation

Interactive setup offers **Discover**, a **manual address**, or **Skip**. Discovery
listens only for the Reachy Mini mDNS service (`_reachy-mini._tcp`) and verifies
candidates through their daemon. It does not scan a subnet or choose a robot for you.
The advertisement follows [Pollen's discovery implementation](https://github.com/pollen-robotics/reachy_mini/blob/main/src/reachy_mini/utils/discovery.py).
The installer adds the system packages `avahi-utils` and `avahi-daemon` for this
discovery path. Include them in the release dependency inventory; they are installed
dependencies, not third-party source copied into this repository. If an administrator
has disabled the daemon, use manual setup or skip rather than overriding that policy.

If the first search is empty, wait a few seconds and choose **Scan again** once.
An empty result does not by itself mean the robot needs rebooting. Discovery needs
both an advertisement and a valid daemon response; camera contention is a separate
check after connection. If discovery remains empty, use the manual fallback below.

If no robot appears, check its own Reachy dashboard or your router for its address.
The robot and Orin must be reachable on the same local network. A network may block
mDNS while still allowing a manual address. Enter the verified address or skip.
Do not connect to an unfamiliar robot simply because discovery lists it.

Noninteractive installs leave the choice for the UI. To explicitly skip connection:

```bash
REACHY_SETUP_SKIP=1 ./scripts/bootstrap.sh <jetson-user>@<orin-address>
```

An explicit `REACHY_MINI_IP` uses that address only after daemon validation. Robot
selection does not configure the Orin's Wi-Fi; use the separate
[network-switching procedure](network-switching.md) for that task.

## Add or change a robot later

Open **Reachy Mini setup** in Live Vision. Discover and select a listed robot, enter a
manual address, or select **Skip**. These controls remain available after skipping.
Connection changes update the local settings and restart only the camera bridge.
They do not rebuild, stop or restart the Cosmos engine.

The deployment account can use the same manager from an Orin terminal:

```bash
bash /opt/live-vision-cosmos-demo/scripts/set-reachy-ip.sh --discover
bash /opt/live-vision-cosmos-demo/scripts/set-reachy-ip.sh <verified-robot-address>
bash /opt/live-vision-cosmos-demo/scripts/set-reachy-ip.sh --skip
```

No argument shows status. Validation cannot be bypassed with `--force`. A failed
connection keeps the previous settings and gives an actionable error; an unsafe
previous target remains disconnected. Discovery and connection checks do not move
the robot or start an onboard app.

An older saved hostname or an excluded address is rejected by the new runtime.
As the deployment owner, run `set-reachy-ip.sh <verified-robot-address>` to validate
and pin the intended robot's current address. Do not bypass an exclusion to migrate.

## Prepare an older installation

Refresh this committed project at its existing installation directory, then run:

```bash
sudo -E bash /opt/live-vision-cosmos-demo/scripts/setup-orin.sh --reachy-only
```

This installs missing robot dependencies and updates UI/bridge units, configuration
ownership and restricted bridge permissions. It may restart the UI. It does not
run the engine pipeline, alter power settings or restart the inference shim.
Package downloads need internet. Never create engine-stage markers to add a robot.
The full installer starts model/UI inference before optional preparation. If the
optional downloads fail, inference stays available; fix the reported error and use
the command above to retry robot preparation.

The UI can configure only already-prepared components. It cannot install packages
or run arbitrary root commands. Its only robot-service privileges are restarting
and stopping `reachy-mjpeg-bridge.service`.

## Understand the status

- **Not configured:** dependencies can be ready; no robot has been selected.
- **Skipped:** the saved choice disables the bridge; browser-camera inference works.
- **Configured:** an address is saved; check daemon reachability and a fresh bridge
  frame before calling the camera feed live.
- **Discovery unavailable:** use a verified manual address or skip. Do not sweep
  the network or disable a firewall blindly.

Piper is an on-demand child of the UI, not a systemd service. The first speech
request starts its voice model; the process remains warm for later utterances.
Speech is useful only with a connected robot to play the audio. No Piper process
before the first speech request is expected.

## Agent acceptance checks

1. Confirm the intended new Orin and retain the working inference baseline.
2. Ask the user to select a discovered robot, provide a verified address, or skip.
   Keep addresses and device details in private deployment notes.
   The optional private `REACHY_EXCLUDED_ADDRESSES` setting excludes devices from
   discovery/connection; never publish its values or reuse another board's identity.
3. After connection, verify Reachy Mini setup, a fresh bridge frame and a real caption
   from that frame. A saved address alone does not prove a working video feed.
4. Test speech only when requested; report synthesis and audible playback separately.
5. Reopen settings after **Skip** to confirm adding a robot remains possible.
   Record performed checks separately from untested hardware behavior.

## Validation on 2026-10-06

The upgrade was applied to the new SD-based Orin Nano deployment using
`--reachy-only`. The inference process kept its PID, and both language and vision
engine hashes were unchanged. A fresh image upload still produced a coherent
panda caption through the HTTPS UI.

The bridge dependencies passed `pip check`. Starting the bridge with an empty
address cleanly skipped its execution rather than entering a restart loop. The
services panel showed **Not configured** for the bridge and **Idle · connect
Reachy Mini** for Piper. A bounded discovery request returned no verified robot;
manual entry and skip remained available. No robot was selected or controlled.

A later retry discovered one robot and verified its daemon as running and ready.
No robot reboot was performed. This confirmed the discovery path on the actual
network; physical camera and speaker checks still require selecting that robot.

Offline checks passed: 89 Python UI tests, 55 installer tests and 32 JavaScript
tests. Desktop and mobile browser checks used synthetic robot responses to test
selection, manual entry, persistent skip, later connection, malformed settings,
and preservation of a running webcam. Those checks do not establish a working
physical Reachy camera or audible speech; both remain pending robot selection.
