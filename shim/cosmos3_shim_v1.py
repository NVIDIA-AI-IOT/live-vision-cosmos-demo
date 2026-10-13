#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""OpenAI-compatible shim for Cosmos3-Edge — RESIDENT runtime (v1).

v0 spawned llm_inference as a subprocess per request, reloading the engines
every time. v1 loads LLMRuntime once at startup via the pybind11
bindings and serves every request against the warm, resident model.
"""
import asyncio
import base64
import json
import os
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# All four paths below are build outputs of NVIDIA's TensorRT-Edge-LLM SDK (see docs/source-installation.md
# "1. Build TensorRT-Edge-LLM and the Cosmos3-Edge engine") - not part of this repo. Defaults assume you built it under
# EDGELLM_ROOT as documented; override any of the four env vars if yours lives elsewhere.
EDGELLM_ROOT = os.environ.get("EDGELLM_ROOT", "/opt/live-vision-cosmos-demo/external/TensorRT-Edge-LLM")
os.environ.setdefault("EDGELLM_PLUGIN_PATH", f"{EDGELLM_ROOT}/build/libNvInfer_edgellm_plugin.so")
sys.path.insert(0, os.environ.get("EDGELLM_PYBIND_DIR", f"{EDGELLM_ROOT}/build/pybind"))

import _edgellm_runtime as rt  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402

MODEL_ID = "nvidia/Cosmos3-Edge"
ENGINE_DIR = os.environ.get("ENGINE_DIR", "/opt/live-vision-cosmos-demo/engine-link")

DATA_URL_RE = re.compile(r"^data:(?P<mime>[\w/+.-]+);base64,(?P<data>.+)$", re.DOTALL)

app = FastAPI()
_lock = asyncio.Lock()
_pool = ThreadPoolExecutor(max_workers=1)
_runtime = None


def _init_runtime():
    global _runtime
    template = Path(ENGINE_DIR) / "processed_chat_template.json"
    try:
        data = json.loads(template.read_text())
        image_format = data["content_types"]["image"]["format"]
        roles = data["roles"]
        valid = (image_format == "<|vision_start|><|image_pad|><|vision_end|>"
                 and data.get("generation_prompt")
                 and all(roles.get(role, {}).get("prefix") and roles.get(role, {}).get("suffix")
                         for role in ("system", "user", "assistant"))
                 and not roles["user"]["prefix"].startswith(
                     roles["system"]["prefix"] + roles["system"]["suffix"]))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise RuntimeError("Engine lacks a valid processed chat template; rebuild the Cosmos3-Edge "
                           "LLM and vision engines with TensorRT-Edge-LLM v0.10.0.")
    print(f"[shim] engine = {os.path.realpath(ENGINE_DIR)}", flush=True)
    t0 = time.time()
    _runtime = rt.LLMRuntime(ENGINE_DIR, ENGINE_DIR, {})
    print(f"[shim] LLMRuntime constructed in {time.time()-t0:.2f}s", flush=True)
    t0 = time.time()
    _runtime.capture_decoding_cuda_graph()
    print(f"[shim] CUDA graphs captured in {time.time()-t0:.2f}s", flush=True)
    # Warm-up so the first real request doesn't pay first-call cost.
    try:
        t0 = time.time()
        req = _build_request([{"role": "user", "content": "hi"}], 4, 0, 0.95)
        _runtime.handle_request(req)
        print(f"[shim] warm-up inference in {time.time()-t0:.2f}s", flush=True)
    except Exception as e:  # warm-up is best-effort
        print(f"[shim] warm-up skipped: {e}", flush=True)
    print("[shim] ready", flush=True)


@app.on_event("startup")
def _startup():
    _init_runtime()


@app.get("/health/ready")
def health_ready():
    """Readiness, as the Live Vision UI defines it.

    That UI refuses to run inference until this returns 200 with status "ready" - it polls
    /health/ready, /v1/models and /api/runtime together and treats a failure of the first two
    as "backend unavailable". This shim only ever served /v1/models and /v1/chat/completions,
    so the UI sat there reporting the backend down while the model was loaded and answering.

    Reports the real thing: _runtime is None until _init_runtime() finishes constructing
    LLMRuntime, so 503 during that loading window is accurate
    rather than a formality.

    /api/runtime is deliberately NOT implemented. The UI tolerates it (null-checked, and
    applyRuntime is try/caught), and answering it means publishing engine_id, an image-token
    limit, a static_clocks flag and an encoder cache size. The first is knowable here; the
    rest are not, and inventing them would push wrong limits into the UI's controls. Missing
    endpoint costs only the image-token presets falling back to defaults.
    """
    if _runtime is None:
        return JSONResponse({"status": "loading"}, status_code=503)
    return JSONResponse({"status": "ready"})


def _build_request(messages, max_tokens, temperature, top_p, top_k=50):
    """Translate OpenAI-style messages into an LLMGenerationRequest."""
    images = []
    rt_messages = []
    # The model expects one leading system block, even when it is empty.
    # Export validation removes the SDK's copy from every user-message prefix.
    if not messages or messages[0].get("role") != "system":
        rt_messages.append(rt.Message("system", [rt.MessageContent("text", "")]))
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")
        if isinstance(content, str):
            rt_messages.append(rt.Message(role, [rt.MessageContent("text", content)]))
            continue
        parts = []
        for item in content or []:
            itype = item.get("type")
            if itype == "text":
                parts.append(rt.MessageContent("text", item.get("text", "")))
            elif itype == "image_url":
                url = (item.get("image_url") or {}).get("url", "")
                m = DATA_URL_RE.match(url)
                if m:
                    raw = base64.b64decode(m.group("data"))
                    images.append(rt.load_image_from_bytes(raw))
                    parts.append(rt.MessageContent("image", "inline"))
                else:
                    images.append(rt.load_image_from_path(url))
                    parts.append(rt.MessageContent("image", url))
        rt_messages.append(rt.Message(role, parts))

    inner = rt.Request(rt_messages)
    if images:
        inner.image_buffers = images
    req = rt.LLMGenerationRequest()
    req.requests = [inner]
    req.max_generate_length = int(max_tokens)
    req.temperature = float(temperature)
    req.top_p = float(top_p)
    req.top_k = int(top_k)
    # Use the processed model template. Keep the live-caption path
    # concise; do not inherit future runtime changes to these request defaults.
    req.apply_chat_template = True
    req.add_generation_prompt = True
    req.enable_thinking = False
    return req


@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local"}]}


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, separators=(",", ":")) + "\n\n"


async def _stream_completion(req, body, t_start):
    """Server-sent events, for clients that will not accept a single JSON body.

    The Live Vision UI only ever sends stream=true and refuses anything that is not SSE, so
    without this the model would answer correctly and its proxy would discard the result as a
    502. Non-streaming OpenAI clients (AGENTS.md's curl check, for one) send no stream flag and
    must keep getting exactly the body they always got, which is why this is a separate path
    rather than a rewrite.

    The runtime streams through a StreamChannel attached to the request: handle_request runs
    on the same single-slot pool as before, while this coroutine pops chunks as they are
    produced. wait_pop is blocking, so it goes to the default executor rather than the
    inference pool - putting it on _pool would deadlock against the generation it is waiting on.
    """
    channel = rt.StreamChannel.create()
    channel.set_stream_interval(1)
    req.stream_channels = [channel]

    cid = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    head = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID}
    want_usage = bool((body.get("stream_options") or {}).get("include_usage"))

    async def frames():
        loop = asyncio.get_running_loop()
        gen_tok = prompt_tok = 0
        reason = "stop"
        await _lock.acquire()
        try:
            yield _sse({**head, "choices": [{"index": 0, "delta": {"role": "assistant"},
                                             "finish_reason": None}]})
            # The native boundary the UI insists on. Started here, after _build_request has
            # already decoded and loaded the JPEG, so this measures generation and not image
            # work or transport - which is exactly the distinction its comments demand.
            t_native = time.monotonic()
            first_text_at = None
            fut = loop.run_in_executor(_pool, _runtime.handle_request, req)
            while True:
                chunk = await loop.run_in_executor(None, channel.wait_pop, 250)
                if chunk is None:
                    # No chunk within the timeout. Only stop if generation is actually over,
                    # otherwise this is just a slow token and the stream continues.
                    if fut.done() and channel.is_finished():
                        break
                    continue
                if getattr(chunk, "prompt_token_count", 0):
                    prompt_tok = chunk.prompt_token_count
                if chunk.text:
                    if first_text_at is None:
                        first_text_at = time.monotonic()
                    gen_tok += len(chunk.token_ids) if chunk.token_ids else 1
                    yield _sse({**head, "choices": [{"index": 0, "delta": {"content": chunk.text},
                                                     "finish_reason": None}]})
                if chunk.finished:
                    if getattr(chunk, "reason", None) == rt.FinishReason.LENGTH:
                        reason = "length"
                    break
            try:
                await fut          # surface an inference error rather than ending the stream silently
            except Exception as e:
                yield _sse({**head, "choices": [{"index": 0, "delta": {},
                                                 "finish_reason": "error"}],
                            "error": {"message": f"inference failed: {e}"}})
                yield "data: [DONE]\n\n"
                return
            # Live Vision reads data.cosmos_metrics and validates every field before it will
            # display a latency at all - wrong types or a missing key silently render "-".
            # timing_source must say server_monotonic because these come from time.monotonic().
            native_ms = (time.monotonic() - t_native) * 1000.0
            metrics = {"timing_boundary": "native_inference",
                       "timing_source": "server_monotonic",
                       "request_id": cid,
                       "completion_tokens": int(gen_tok),
                       "prompt_tokens": int(prompt_tok),
                       "native_inference_ms": round(native_ms, 3)}
            if first_text_at is not None:
                metrics["first_text_timing_boundary"] = "native_start_to_server_text"
                metrics["server_first_text_ms"] = round((first_text_at - t_native) * 1000.0, 3)
            yield _sse({**head, "cosmos_metrics": metrics,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]})
            if want_usage:
                yield _sse({**head, "choices": [],
                            "usage": {"prompt_tokens": prompt_tok, "completion_tokens": gen_tok,
                                      "total_tokens": prompt_tok + gen_tok}})
            yield "data: [DONE]\n\n"
            elapsed = (time.time() - t_start) * 1000.0
            mspt = (elapsed / gen_tok) if gen_tok else float("nan")
            print(f"[perf] stream elapsed_ms={elapsed:.0f} prompt_tok={prompt_tok} "
                  f"gen_tok={gen_tok} ms_per_tok={mspt:.1f}", flush=True)
        finally:
            if not channel.is_finished():
                channel.cancel()
            _lock.release()

    return StreamingResponse(frames(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    _t_start = time.time()
    body = await request.json()
    try:
        req = _build_request(
            body.get("messages", []),
            body.get("max_tokens", 256),
            body.get("temperature", 0.7),
            body.get("top_p", 0.95),
            body.get("top_k", 50),
        )
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": {"message": f"bad request: {e}"}})

    if body.get("stream"):
        return await _stream_completion(req, body, _t_start)

    loop = asyncio.get_running_loop()
    async with _lock:
        try:
            resp = await loop.run_in_executor(_pool, _runtime.handle_request, req)
        except Exception as e:
            return JSONResponse(status_code=502, content={"error": {"message": f"inference failed: {e}"}})

    text = resp.output_texts[0] if resp.output_texts else ""
    # Token counts feed both the [perf] log line and the OpenAI `usage` field, so they are
    # computed outside the instrumentation try-block.
    _gen = len(resp.output_ids[0]) if resp.output_ids else 0
    _prompt = resp.prompt_token_counts[0] if resp.prompt_token_counts else 0
    # --- latency instrumentation: normalize by generated tokens ---
    try:
        _elapsed_ms = (time.time() - _t_start) * 1000.0
        _mspt = (_elapsed_ms / _gen) if _gen else float("nan")
        print(f"[perf] elapsed_ms={_elapsed_ms:.0f} prompt_tok={_prompt} "
              f"gen_tok={_gen} ms_per_tok={_mspt:.1f}", flush=True)
    except Exception as _e:
        print(f"[perf] instrumentation error: {_e}", flush=True)
    reason = "stop"
    try:
        fr = resp.finish_reasons[0]
        reason = "length" if fr == rt.FinishReason.LENGTH else "stop"
    except Exception:
        pass

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": reason}
        ],
        "usage": {
            "prompt_tokens": _prompt,
            "completion_tokens": _gen,
            "total_tokens": _prompt + _gen,
        },
    }


if __name__ == "__main__":
    import uvicorn

    # Matches --backend-port in live-vision-cosmos-demo-ui.service; override with PORT if you
    # need a different port (e.g. running more than one engine's shim on the same box).
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8001)), log_level="warning")
