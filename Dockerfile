FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models ANONYMIZED_TELEMETRY=False PROMPT_DIR=/app/prompts
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# CPU-only torch keeps the image ~1.5 GB smaller than the default CUDA build
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY triage ./triage
COPY prompts ./prompts
COPY eval ./eval
RUN useradd -m -u 1000 app && mkdir -p /data /models && chown -R app /data /models /app
USER app
