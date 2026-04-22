# Small init image that truncates ClickHouse + seeks Kafka consumer groups
# before the api service boots. See scripts/dev/reset-state.sh for the
# motivation and exit criterion.
FROM alpine:3.20

RUN apk add --no-cache bash curl unzip

# rpk (Redpanda CLI) — auto-detect arch for amd64/arm64 builds.
RUN set -eux; \
    arch="$(uname -m)"; \
    case "$arch" in \
      x86_64)  rpk_arch="amd64" ;; \
      aarch64) rpk_arch="arm64" ;; \
      *) echo "unsupported arch $arch"; exit 1 ;; \
    esac; \
    curl -fsSLO "https://github.com/redpanda-data/redpanda/releases/download/v24.2.7/rpk-linux-${rpk_arch}.zip"; \
    unzip "rpk-linux-${rpk_arch}.zip" -d /usr/local/bin/; \
    rm "rpk-linux-${rpk_arch}.zip"; \
    chmod +x /usr/local/bin/rpk

COPY scripts/dev/reset-state.sh /reset-state.sh
RUN chmod +x /reset-state.sh

ENTRYPOINT ["/reset-state.sh"]
