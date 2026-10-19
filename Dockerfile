FROM python:3.12-slim

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
CMD ["python", "-m", "nhako"]
