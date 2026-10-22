FROM --platform=linux/amd64 python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /app
COPY requirements.txt wheels-sha256.txt /tmp/
COPY wheels/ /tmp/wheels/
RUN cd /tmp/wheels \
    && sha256sum -c /tmp/wheels-sha256.txt \
    && pip install --no-cache-dir --no-index --find-links=/tmp/wheels --no-deps -r /tmp/requirements.txt \
    && pip check
COPY src/ /app/src/
