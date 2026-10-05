FROM python:3.12-slim

# Shown by /health, so you can see which version is running during a rollout.
ARG APP_VERSION=1.0
ENV APP_VERSION=${APP_VERSION} \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first: Docker caches this layer, so rebuilds after a code
# change do not reinstall every package.
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

# Do not run as root. A numeric user id is needed so Kubernetes can verify
# "runAsNonRoot".
RUN useradd --uid 10001 --create-home appuser
USER 10001

EXPOSE 5000

# One worker with threads: Prometheus metrics live in the process memory, so
# several worker processes would each report different numbers. We scale by
# running more containers (replicas) instead of more workers.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "app:app"]
