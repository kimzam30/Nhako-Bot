# Pinned by digest so a rebuild gets the exact base that was tested.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Kuala_Lumpur

RUN useradd --create-home --uid 1000 nhako
WORKDIR /app

# requirements.lock pins every package (including transitive ones like PyAV, whose surprise
# upgrade once broke voice notes). REQS=requirements.txt is only for regenerating the lock.
ARG REQS=requirements.lock
COPY requirements.txt requirements.lock ./
RUN pip install -r ${REQS}

# Only the package: .dockerignore keeps .env, tokens, data/ and the venv out of the image.
COPY nhako ./nhako

USER nhako

# The scheduler loop touches data/heartbeat every 20 s; older than 2 minutes means the bot is stuck.
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os, sys, time; sys.exit(time.time() - os.path.getmtime('/app/data/heartbeat') > 120)"]

CMD ["python", "-m", "nhako"]
