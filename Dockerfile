# ---- 构建 ----
FROM quay.io/ascend/vllm-ascend:v0.19.1rc1

# ---- 1. 运行时系统库 ----
RUN apt-get update && \
    apt-get install -y --no-install-recommends libopus0 libsndfile1 && \
    rm -rf /var/lib/apt/lists/*

# ---- 2. 预编译 OpenFst ----
COPY weights/openfst/arm64/lib/* /usr/local/lib/
COPY weights/openfst/arm64/include/ /usr/local/include/
RUN echo /usr/local/lib >> /etc/ld.so.conf.d/openfst.conf && ldconfig

# ---- 3. 安装 pynini + WeTextProcessing ----
RUN pip install pynini==2.1.6 && \
    GIT_SSL_NO_VERIFY=1 pip install 'git+https://github.com/wenet-e2e/WeTextProcessing.git'

# ---- 4. 安装 Qwen3-ASR 音频处理依赖（必须，否则 vLLM 处理 audio_url 返回 400） ----
RUN pip install --no-deps 'qwen-asr[vllm]'

# ---- 5. 安装项目 Python 依赖（torch/vllm 已内置，不要重装以免破坏兼容） ----
RUN pip install \
    "librosa" \
    "torchaudio>=2.0.0" \
    "fastapi>=0.115.0" \
    "websockets>=12.0" \
    "uvicorn[standard]>=0.30.0" \
    "pydantic>=2.5.0" \
    "numpy==1.26.4" \
    "httpx>=0.27.0" \
    "prometheus-client>=0.21.0" \
    "soundfile>=0.12.0"

# ---- 6. 复制项目 ----
WORKDIR /app
COPY main.py .
COPY src/ ./src/
COPY weights/ ./weights/

# ---- 7. VAD 路径（ARM aarch64），替换 C 库内部的相对路径 onnx_model ----
RUN ln -sf weights/vad/ten-vad/onnx_model /app/onnx_model
ENV LD_LIBRARY_PATH=/app/weights/vad/ten-vad/lib/Linux/aarch64:/usr/local/lib

# VL模型ssl
COPY connections.py /vllm-workspace/vllm/vllm/connections.py

# 翻译代理
COPY translation_proxy.py /workspace/translation_proxy.py

# 语言模型健康检查脚本
COPY healthcheck-vl-7b.sh /healthcheck-vl-7b.sh
RUN chmod +x /healthcheck-vl-7b.sh