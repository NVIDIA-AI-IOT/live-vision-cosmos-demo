# Troubleshooting Live Vision

[Back to the tutorial](../README.md).

## The installer stopped

Read the failing stage's message, fix its reported cause and rerun the same
`bootstrap.sh` command. Completed stages have markers under
`/opt/live-vision-cosmos-demo/.setup-state/`; do not create markers or lower space
checks to skip work. An interrupted download or build is not a reason to reflash
a working JetPack installation.

For failures after export, use the [installer recovery guidance](first-boot-recovery.md#if-the-demo-installer-stops).
For device identity or account problems, start with [first-boot recovery](first-boot-recovery.md).
The [source guide](source-installation.md) preserves the manual build reference.

## The page opens, but the camera does not

Open the printed **HTTPS** URL on the device whose camera you want to use. Trust
your own Jetson's certificate, allow camera permission in the browser and click
**Start camera**. Close other apps that may hold the camera. If the browser still
refuses a self-signed certificate, use a certificate it trusts for the Jetson's
actual hostname or address.

**Choose image** can test captioning independently of camera access. A browser
on a smartphone uses that phone's camera; a browser on your laptop uses the
laptop's selected camera.

## Readiness passes, but captions are wrong

`/health/ready` confirms that the backend loaded. It does not prove model accuracy
or that INT4 weights were interpreted correctly. Test coherent text, then known
images and changing live frames. Save the inputs, outputs and engine identity
when reporting a repeatable failure.

The pinned 0.11 installer applies a guarded HWC visual-export repair and validates
the provider Jinja template. Do not substitute a 0.10.1 engine or its JSON template
repair. See [SDK compatibility](edgellm-0.11-upgrade.md) and
[quantization/build details](source-installation.md#setup) before rebuilding.

## Reachy is missing or silent

Browser-camera captioning works without a robot. In a source installation, open
**Reachy Mini setup**, verify that the robot is on the same local network and use
its known address if mDNS discovery does not find it. Choosing **Skip** leaves the
bridge stopped. See [Reachy setup](reachy-setup.md) for the supported recovery path.

Flite runs once per speech request; it is not a separate systemd service. An idle
speech status between utterances is expected. Check that the UI points to
`/opt/live-vision-cosmos-demo/flite/bin/flite`. The container excludes robot camera
bridging and speech. Use the
source installation for those features.

## I want to change Wi-Fi or restart

Complete a real-image inference test over Ethernet before following the
[network-switching guide](network-switching.md). Keep device addresses and secrets
out of source control. After any restart, verify a meaningful caption and changing
frames, as well as the readiness endpoint.

For the container, stop with Ctrl-C and rerun its launcher; the engine bundle and
certificates remain on the host. See [the runtime guide](../container/README.md)
for download recovery, compatibility checks and its current artifact status.
