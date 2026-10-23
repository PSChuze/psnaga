# The server image: every Python service (connsv, auth, world) and the game data builder
# (extract) in one. Which one runs is picked by the compose `command:`.
#
# Stdlib only: no requirements file, nothing to pip install.
#
# Build context is the repository root (not server/): the name tables are read from
# overlay/text (our English) and /assets/text (built by extract from your disc). What gets in
# is limited by server/Dockerfile.dockerignore.
#
# All mutable state lives under /data (chars.json, auth.json, news.json, reports.jsonl, backups/),
# logs under /logs and the game data under /assets; compose mounts them from the state folder.
# The image itself is stateless.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NOBU_DATA=/data \
    NOBU_LOG_DIR=/logs \
    NOBU_ASSETS=/assets \
    NOBU_OVERLAY=/overlay

WORKDIR /app
COPY server/nobu /app/nobu
COPY extract /app/extract
COPY overlay /overlay

# Prove the package imports and the self-tests pass in the image's own Python. The tests get a
# throwaway NOBU_DATA / NOBU_LOG_DIR (some seed files on first use, which must not be baked into
# the image) and no game data (the tests that need it skip themselves).
RUN t=$(mktemp -d) \
    && NOBU_DATA=$t/data NOBU_LOG_DIR=$t/logs NOBU_ASSETS=$t/assets \
       python -m unittest discover -s /app -p "test_*.py" -t /app \
    && rm -rf "$t" \
    && find /app -name __pycache__ -prune -exec rm -rf {} + \
    && test -z "$(ls -A /data /logs /assets 2>/dev/null)"

VOLUME ["/data", "/logs"]

# The commit this image is built from and when, for the panel's update notice (nobu/updates.py).
# Setup passes them (compose.yml's build args); last, so a new commit rebuilds only this layer.
ARG NOBU_REVISION=
ARG NOBU_BUILT_AT=
ENV NOBU_REVISION=$NOBU_REVISION     NOBU_BUILT_AT=$NOBU_BUILT_AT

# connsv 9070, world 9110, auth 18080 (plain HTTP behind the TLS terminator).
EXPOSE 9070 9110 18080

CMD ["python", "-m", "nobu.world"]
