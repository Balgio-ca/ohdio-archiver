FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY ohdio.py web.py ui.html config.toml ./

ENV OHDIO_ARCHIVE_DIR=/archive \
    OHDIO_CONFIG=/config/config.toml \
    PYTHONUNBUFFERED=1

VOLUME ["/archive", "/config"]
EXPOSE 8765

# Au premier démarrage, copie la config par défaut dans /config si elle est absente.
ENTRYPOINT ["sh", "-c", "[ -f \"$OHDIO_CONFIG\" ] || cp /app/config.toml \"$OHDIO_CONFIG\" 2>/dev/null || true; exec python3 /app/ohdio.py \"$@\"", "--"]
# Interface web + archivage automatique. Pour un passage unique : `docker compose run --rm ohdio download`.
CMD ["serve"]
