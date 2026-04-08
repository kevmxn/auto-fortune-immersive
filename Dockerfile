# ── Docena Signal Bot — Dockerfile ────────────────────────────────────────────
FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libfreetype6-dev libpng-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    pyTelegramBotAPI==4.21.0 \
    websockets==12.0 \
    flask==3.0.3 \
    matplotlib==3.9.2 \
    numpy==2.1.3

WORKDIR /app
COPY main.py .

ENV PORT=10001
EXPOSE 10001

CMD ["python", "main.py"]
