FROM debian:bookworm-slim AS binary
ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && case "$TARGETARCH" in \
         amd64) arch=x86_64; sha=5690596c54ff9bed63fa3732f818a05dbc2db19ad36ed68f21ca5f64d5cfeeb7 ;; \
         arm64) arch=aarch64; sha=3953d231da3783e2bf8904b6dd72767c5c6e533e163d3742fd0437affa431bd3 ;; \
         *) echo "Unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
       esac \
    && curl --fail --show-error --location --retry 3 \
         "https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-${arch}.tgz" \
         --output /tmp/speedtest.tgz \
    && echo "$sha  /tmp/speedtest.tgz" | sha256sum --check - \
    && tar -xzf /tmp/speedtest.tgz -C /usr/local/bin speedtest

FROM python:3.13-slim-bookworm
COPY --from=binary /usr/local/bin/speedtest /usr/local/bin/speedtest
WORKDIR /app
COPY runner.py .
USER 65534:65534
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python", "runner.py"]
