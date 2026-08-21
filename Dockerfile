FROM python:3.12-slim

# Non-root: the tracker only needs to read config and write its database.
RUN useradd --create-home --uid 10001 tracker

WORKDIR /app

# Runtime dependencies only. pytest is for development and anthropic is an
# optional extra, so neither belongs in the image; add anthropic here if you
# enable the model judging stage.
RUN pip install --no-cache-dir requests pydantic \
 && rm -rf /root/.cache/pip

COPY src/ ./src/
COPY scripts/ ./scripts/

# A default config so a deploy with no bind mount still boots. It seeds the
# database on the volume on first run; after that the dashboard is where
# searches are managed and this file is not consulted again.
COPY profiles.toml.example ./config/profiles.toml

ENV PYTHONPATH=/app/src \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    TRACKER_DB=/data/tracker.db \
    WEB_HOST=0.0.0.0

RUN mkdir -p /data && chown tracker:tracker /data
USER tracker
VOLUME ["/data"]
EXPOSE 8000

# Honours PORT when the platform injects one (Railway, Heroku and similar),
# otherwise falls back to the port the config file sets.
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request,sys; p=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+p+'/healthz',timeout=4).status==200 else 1)"

ENTRYPOINT ["python", "-m", "tracker"]
CMD ["run", "-c", "/app/config/profiles.toml"]
