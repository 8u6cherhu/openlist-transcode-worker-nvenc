FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    OPENLIST_CONFIG_FILE=/var/lib/openlist-worker/config.json \
    OPENLIST_WORKER=/usr/local/bin/openlist-transcode-worker \
    OPENLIST_REAL_FFMPEG=/usr/bin/ffmpeg \
    OPENLIST_UI_ADDR=0.0.0.0:8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       ca-certificates \
       tzdata \
       python3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1001 --create-home --shell /usr/sbin/nologin worker \
    && mkdir -p /var/lib/openlist-worker /opt/openlist-worker/web \
    && chown -R 1001:1001 /var/lib/openlist-worker /opt/openlist-worker

COPY openlist-transcode-worker /usr/local/bin/openlist-transcode-worker
COPY entrypoint.py /opt/openlist-worker/entrypoint.py
COPY scripts/ffmpeg-nvenc-wrapper.py /usr/local/bin/ffmpeg-nvenc-wrapper
COPY web/index.html /opt/openlist-worker/web/index.html
COPY config/config.example.json /opt/openlist-worker/config.example.json
RUN chmod 0755 /usr/local/bin/openlist-transcode-worker \
    /usr/local/bin/ffmpeg-nvenc-wrapper \
    /opt/openlist-worker/entrypoint.py

USER 1001:1001
WORKDIR /var/lib/openlist-worker
EXPOSE 8080
ENTRYPOINT ["/opt/openlist-worker/entrypoint.py"]
