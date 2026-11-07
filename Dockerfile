FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg nodejs fonts-dejavu-core fontconfig ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces runs as uid 1000 and expects port 7860
RUN useradd -m -u 1000 user
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=user . .
USER user
RUN mkdir -p temp outputs

ENV PORT=7860
EXPOSE 7860
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
