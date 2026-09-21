FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY yottio.py web.py ui.html config.toml ./

ENV YOTTIO_ARCHIVE_DIR=/archive \
    YOTTIO_CONFIG=/config/config.toml \
    PYTHONUNBUFFERED=1

VOLUME ["/archive", "/config"]
EXPOSE 8765

# Au premier démarrage, copie la config par défaut dans /config si elle est absente.
ENTRYPOINT ["sh", "-c", "[ -f \"$YOTTIO_CONFIG\" ] || cp /app/config.toml \"$YOTTIO_CONFIG\" 2>/dev/null || true; exec python3 /app/yottio.py \"$@\"", "--"]
# Interface web + archivage automatique. Pour un passage unique : `docker compose run --rm yottio download`.
CMD ["serve"]
