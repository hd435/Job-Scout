# syntax=docker/dockerfile:1

# Job-Scout hosted service. No inbound port: the bot long-polls Telegram, so there is nothing to
# route and no certificate to manage. The only state is the SQLite database on /data.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    JOBSCOUT_CONFIG=/app/service-config.toml \
    JOBSCOUT_DATABASE=/data/service.db

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

# The hosted selection lives with the deployment, not in the package: which employers this
# deployment offers is an operator decision (D-094).
COPY deploy/service-config.toml ./service-config.toml

RUN useradd --create-home --uid 10001 jobscout \
    && mkdir -p /data \
    && chown -R jobscout:jobscout /data /app
USER jobscout

VOLUME ["/data"]

# Liveness is "monitoring is still happening", which is what actually breaks. The check reads the
# database only, so it also proves the volume is mounted and writable.
HEALTHCHECK --interval=10m --timeout=60s --start-period=15m --retries=3 \
    CMD ["python", "-m", "jobscout", "service", "check"]

CMD ["python", "-m", "jobscout", "bot"]
