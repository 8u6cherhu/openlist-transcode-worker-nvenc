FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       ca-certificates \
       tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1001 --create-home --shell /usr/sbin/nologin worker \
    && mkdir -p /var/lib/openlist-worker \
    && chown -R 1001:1001 /var/lib/openlist-worker

COPY openlist-transcode-worker /usr/local/bin/openlist-transcode-worker
RUN chmod 0755 /usr/local/bin/openlist-transcode-worker

USER 1001:1001
WORKDIR /var/lib/openlist-worker

ENTRYPOINT ["/usr/local/bin/openlist-transcode-worker"]
