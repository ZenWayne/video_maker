FROM nvcr.io/nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

# Use Aliyun mirror for faster apt in China
RUN echo "deb https://mirrors.aliyun.com/ubuntu/ jammy main restricted universe multiverse" > /etc/apt/sources.list && \
    echo "deb https://mirrors.aliyun.com/ubuntu/ jammy-updates main restricted universe multiverse" >> /etc/apt/sources.list && \
    echo "deb https://mirrors.aliyun.com/ubuntu/ jammy-backports main restricted universe multiverse" >> /etc/apt/sources.list && \
    echo "deb https://mirrors.aliyun.com/ubuntu/ jammy-security main restricted universe multiverse" >> /etc/apt/sources.list

RUN apt-get update -y && \
    apt-get install -y --no-install-recommends \
        python3.10 python3.10-venv \
        ffmpeg libsndfile1 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Create venv
RUN uv venv --python 3.10 --seed /opt/venv
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy and install vc2 package (torch + torchaudio + onnxruntime + fastapi)
COPY CosyVoice/vc2_pkg /workspace/vc2_pkg
RUN uv pip install \
    --index-url https://mirrors.aliyun.com/pypi/simple/ \
    --extra-index-url https://mirrors.nju.edu.cn/pytorch/whl/cu121 \
    "/workspace/vc2_pkg[service]"

# Bake exported ONNX models into the image (~2.6 GB)
COPY CosyVoice/exported_vc2 /workspace/exported_vc2
ENV MODEL_DIR=/workspace/exported_vc2

EXPOSE 9880
CMD ["uvicorn", "vc2.service:app", "--host", "0.0.0.0", "--port", "9880"]
