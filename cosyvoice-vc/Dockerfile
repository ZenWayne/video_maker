FROM nvcr.io/nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ARG VENV_NAME="cosyvoice"
ENV VENV=$VENV_NAME
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
SHELL ["/bin/bash", "--login", "-c"]

# System dependencies
RUN apt-get update -y --fix-missing && \
    apt-get install -y --no-install-recommends \
        git build-essential curl wget ffmpeg unzip sox libsox-dev git-lfs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    git lfs install

# Install miniforge (conda)
RUN wget --quiet https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh -O ~/miniforge.sh && \
    /bin/bash ~/miniforge.sh -b -p /opt/conda && \
    rm ~/miniforge.sh && \
    ln -s /opt/conda/etc/profile.d/conda.sh /etc/profile.d/conda.sh && \
    echo "source /opt/conda/etc/profile.d/conda.sh" >> ~/.bashrc && \
    echo "conda activate ${VENV}" >> ~/.bashrc

ENV PATH /opt/conda/bin:/opt/conda/envs/${VENV}/bin:$PATH

# Create conda env with Python 3.10
RUN conda create -y -n ${VENV} python=3.10 && \
    conda config --add channels conda-forge && \
    conda config --set channel_priority strict && \
    conda activate ${VENV} && conda install -y -c conda-forge pynini==2.1.5

WORKDIR /workspace

# Clone CosyVoice and install deps
RUN git clone --recursive https://github.com/FunAudioLLM/CosyVoice.git
RUN conda activate ${VENV} && cd CosyVoice && \
    pip install -r requirements.txt --no-cache-dir

ENV PYTHONPATH="/workspace/CosyVoice:/workspace/CosyVoice/third_party/Matcha-TTS:${PYTHONPATH}"
ENV COSYVOICE_ROOT=/workspace/CosyVoice

# Download model (bake into image)
RUN conda activate ${VENV} && python -c \
    "from cosyvoice.cli.cosyvoice import AutoModel; AutoModel(model_dir='FunAudioLLM/Fun-CosyVoice3-0.5B-2512')"

# Install service dependencies
RUN conda activate ${VENV} && pip install fastapi uvicorn python-multipart --no-cache-dir

# Copy service app
COPY app.py /workspace/app.py

EXPOSE 9880

CMD ["conda", "run", "--no-capture-output", "-n", "cosyvoice", \
     "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9880"]
