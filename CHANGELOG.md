# Changelog

## Unreleased

- Import the Live Vision application from upstream commit `da489361cee0755f9215cccb7e76757f45728d3f`, retaining installer, integrity-checking and attribution improvements from the audited candidate.
- Target TensorRT-Edge-LLM 0.11.0 with the pinned Cosmos3-Edge revision and documented Jetson runtime configuration. Existing 0.10.1 engines require a fresh export and build.
- Add a tutorial README with an adapted Astra goal, automated source setup, and screenshots and silent GIFs from Alex Sotelo’s October 2 demonstration.
- Preserve the full engineering runbook in `docs/source-installation.md` and move the unchanged root Apache-2.0 text to `LICENSE.md`.
- Prepare a fixed-engine ARM64 container, separate verified engine bundle and launch helpers. A published prebuilt package and validation against its final artifacts are still pending.

See [SOURCE.json](SOURCE.json) for source provenance and [NOTICE.md](NOTICE.md) for attribution.
