# From One Goal to a Local Vision Demo

[Back to the tutorial](../README.md).

Alex Sotelo presented the original application in **A New Way to Build Edge AI:
Agentic Development on NVIDIA Jetson**, livestreamed on **September 30, 2026, at
9:00 a.m. Pacific**. [Watch the demonstration](https://www.youtube.com/live/OF_ryVZmB3w?t=1927).

One initial agentic goal produced a working FP16 baseline. Later iterations added
INT4 quantization, optimization, interface improvements and accuracy fixes. The
current repository packages that continuing work; the final INT4 implementation
was not all produced in a single unattended step.

The README's images and GIFs come from Alex's **October 2 follow-up**, a 3-minute,
45-second demonstration. They show the workflow and preserve the model's recorded
captions. They do not qualify the current SDK build or forthcoming runtime package.

## Follow the five chapters

| Time | Chapter | Try it in this repository |
|---|---|---|
| 00:00 | Introduction and hardware | [Choose a supported Jetson and camera](../README.md#what-you-need). |
| 00:38 | Live image captioning | [Present a football, cap or microphone](../README.md#try-your-first-caption) and watch the caption change. |
| 01:02 | Performance and resources | Read the UI's device, process and inference measurements; compare identical workloads. |
| 01:22 | Reachy Mini | [Connect the optional robot](../README.md#optional-reachy-mini), switch cameras and try source-install speech. |
| 02:18 | Coding agents and prompting | [Give Astra a verifiable goal](../README.md#build-with-astra), then inspect and validate what it builds. |

These are the follow-up's chapter markers, not timestamps into the longer public
livestream. The original video has not been bundled into this repository.

## The original goal

![The original Astra goal as shown in the follow-up recording](media/astra-goal-prompt-03m25s.png)

*Recorded at 03:25. This is historical context; use the adapted prompt in the
[README](../README.md#build-with-astra) for the current pinned implementation.*

<details>
<summary>Read the original on-screen prompt</summary>

```text
/goal install jetpack on the connected Jetson Orin Nano on the USB-C
port. When that's done, I want a performant and working version of
Cosmos3-Edge (https://huggingface.co/nvidia/Cosmos3-Edge), using
TRT-Edge-LLM as the backend
(https://github.com/NVIDIA/TensorRT-Edge-LLM) and a streaming web
UI like (https://github.com/nvidia-ai-iot/live-vlm-webui). Once you have
signs of life, optimize for the lowest possible VRAM footprint with the
fastest latency. Explore acceleration techniques such as CuTeDSL
(https://github.com/nvidia/cutlass). On a live working setup on Orin, we
have seen demonstrated RAM loads of 4.63GB and TTFT of 209 ms,
with an average latency of 509 ms. Attempt to get within 10% of those
performance parameters and RAM load. When done, serve the website,
test fully working inference and serve me the URL.
```

The referenced projects are [Cosmos3-Edge](https://huggingface.co/nvidia/Cosmos3-Edge),
[TensorRT-Edge-LLM](https://github.com/NVIDIA/TensorRT-Edge-LLM),
[Live VLM WebUI](https://github.com/NVIDIA-AI-IOT/live-vlm-webui) and
[CUTLASS/CuTeDSL](https://github.com/NVIDIA/cutlass).

</details>

The 4.63 GB RAM, 209 ms TTFT and 509 ms average latency values were **targets in
that historical prompt**. They are not promises for this repository, its current
SDK revision, or the packaged runtime. The follow-up shows its own live values;
those are observations from that recording, not a controlled benchmark.

## Read the measurements in context

The UI reports Jetson resources and inference timing separately from browser
capture and transport. TTFT measures native inference start to the first nonempty
server text, including server scheduling. Latency and Average cover native
inference; they exclude JPEG encoding and decoding, network transfer and admission
queueing. Browser timing diagnostics cover those additional parts of the path.

For a useful comparison, hold the image, prompt, capture settings, output limit,
engine and power configuration constant. Inspect actual INT4 structure, coherent
text and known-image captions before interpreting speed. Record the candidate and
artifact identities, restart behavior and memory during a sustained camera run.
The [source guide](source-installation.md) and [SDK upgrade record](edgellm-0.11-upgrade.md)
explain the engineering constraints and validation limits.

Media credits and third-party notices are in [NOTICE.md](../NOTICE.md).
