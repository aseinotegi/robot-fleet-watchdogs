FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 watchdog \
    && mkdir -p /data && chown watchdog /data

USER watchdog
WORKDIR /data
EXPOSE 8080
ENTRYPOINT ["fleet-watchdogs"]
CMD ["-c", "/config/config.yaml"]
