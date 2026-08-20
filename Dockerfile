FROM python:3.12-slim

# Non-root: the tracker only needs to read config and write its database.
RUN useradd --create-home --uid 10001 tracker

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir requests pydantic \
 && rm -rf /root/.cache/pip

COPY src/ ./src/
COPY scripts/ ./scripts/

ENV PYTHONPATH=/app/src \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    TRACKER_DB=/data/tracker.db

RUN mkdir -p /data && chown tracker:tracker /data
USER tracker
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=4).status==200 else 1)"

ENTRYPOINT ["python", "-m", "tracker"]
CMD ["run", "-c", "/app/config/profiles.toml"]
