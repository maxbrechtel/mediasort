FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    MEDIASORT_CONFIG=/config \
    MEDIASORT_PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
# Nicht editierbar: Vorlagen und statische Dateien liegen als Paketdaten im Paket.
RUN pip install --no-cache-dir . \
    && rm -rf /app

# Der Container läuft unter der PUID/PGID aus der Compose-Datei, /config muss
# für jede Benutzerkennung beschreibbar sein.
RUN mkdir -p /config && chmod 0777 /config
WORKDIR /config

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://localhost:%s/health' % os.environ.get('MEDIASORT_PORT', '8080'), timeout=4)"]

CMD ["mediasort"]
