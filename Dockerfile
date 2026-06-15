# Near-zero-dependency image. Stdlib only EXCEPT numpy, which V9 semantic search needs for fast
# nearest-neighbor scoring (musllinux wheel installs in seconds — no compiler). Semantic is opt-in;
# without CC_EMBED_MODEL set, numpy simply rides along unused.
FROM python:3.12-alpine

# tzdata so SQLite's 'localtime' (the cost "today"/daily rollups) honors the user's TZ instead of UTC.
RUN apk add --no-cache tzdata
# numpy: V9 semantic search NN scoring. cryptography: V13 encrypted exports (AES-256-GCM). Both ship
# musllinux wheels → install in seconds on alpine, no compiler.
RUN pip install --no-cache-dir numpy cryptography

WORKDIR /app
COPY server.py watcher.py costing.py store.py craft.py medals.py coach.py content_index.py version.py maintenance.py redact.py portable.py crypto.py fsperms.py ./
COPY web ./web

# Single self-contained process: serve the dashboard AND scan local transcripts in-process.
# Mount the host's transcripts read-only at /data/projects (see docker-compose.yml).
ENV CC_PORT=8099 \
    CC_LOCAL_SCAN=1 \
    CC_PROJECTS_DIR=/data/projects \
    CC_STATE_DIR=/data/state \
    CC_WINDOW_1M_MODELS=claude-opus-4-8

EXPOSE 8099 8100
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD wget -qO- http://127.0.0.1:8099/health >/dev/null 2>&1 || exit 1

CMD ["python", "server.py"]
