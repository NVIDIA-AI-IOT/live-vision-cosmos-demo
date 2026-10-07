# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Supply an approved NVIDIA JetPack development image by immutable digest.
# No image is published by this Dockerfile or the build helper.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG BASE_IMAGE
ARG CANDIDATE_COMMIT
LABEL org.opencontainers.image.title="Live Vision Cosmos3-Edge (internal candidate)" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.revision="${CANDIDATE_COMMIT}"
ENV EDGELLM_ROOT=/opt/TensorRT-Edge-LLM \
    CANDIDATE_COMMIT=${CANDIDATE_COMMIT} \
    EDGELLM_PLUGIN_PATH=/opt/TensorRT-Edge-LLM/build/libNvInfer_edgellm_plugin.so \
    EDGELLM_PYBIND_DIR=/opt/TensorRT-Edge-LLM/build/pybind \
    PATH=/opt/TensorRT-Edge-LLM/.venv/bin:/usr/local/cuda-13.2/bin:${PATH} \
    LD_LIBRARY_PATH=/usr/lib/aarch64-linux-gnu:/usr/local/cuda-13.2/lib64 \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    XDG_CACHE_HOME=/tmp/cache CUDA_CACHE_PATH=/tmp/cuda
WORKDIR /opt/live-vision-cosmos-demo
COPY container/ container/
RUN printf '%s\n' "$BASE_IMAGE" > /opt/base-image-reference.txt \
    && bash container/build-runtime.sh
COPY LICENSE.md NOTICE.md README.md SOURCE.json ./
COPY ui/ ui/
COPY shim/ shim/
COPY reachy/reachy.py reachy/reachy.py
EXPOSE 8443
HEALTHCHECK --interval=15s --timeout=5s --start-period=180s \
  CMD python -c "import json,ssl,urllib.request; assert json.load(urllib.request.urlopen('https://127.0.0.1:8443/health/ready',context=ssl._create_unverified_context(),timeout=3))['status']=='ready'"
ENTRYPOINT ["python", "container/runtime.py"]
