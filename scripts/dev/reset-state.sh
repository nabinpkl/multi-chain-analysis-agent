#!/bin/sh
# Free-tier workaround: wipe derived state before boot.
#
# The Solana RPC rate limit (~5 req/sec) makes catch-up after downtime
# impractical, so on every restart we start fresh from chain tip. The
# Rust code itself is written as proper Kappa (resume-from-committed-offset);
# this script is what actually causes "fresh on restart" in dev/free-tier.
#
# EXIT CRITERION: when we move to a paid RPC (or otherwise can afford to
# catch up), remove this container from docker-compose.yml and set
# KAFKA_AUTO_OFFSET_RESET=earliest on the api service. No Rust changes.

set -eu

# Wait for ClickHouse auth to be ready. The /ping healthcheck passes before
# the default user is fully initialized on a fresh volume, so we probe with
# a real auth'd query.
echo "[reset] waiting for ClickHouse auth..."
i=0
until curl -sS --fail -u "${CLICKHOUSE_USER}:${CLICKHOUSE_PASSWORD}" \
  "${CLICKHOUSE_URL}/?query=SELECT+1" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge 30 ]; then
        echo "[reset] ClickHouse auth never came up, giving up"
        exit 1
    fi
    sleep 1
done

echo "[reset] truncating ClickHouse..."
curl -sS --fail -u "${CLICKHOUSE_USER}:${CLICKHOUSE_PASSWORD}" \
  --data "TRUNCATE TABLE IF EXISTS multichain.edges" \
  "${CLICKHOUSE_URL}/"
curl -sS --fail -u "${CLICKHOUSE_USER}:${CLICKHOUSE_PASSWORD}" \
  --data "TRUNCATE TABLE IF EXISTS multichain.ingestion_state" \
  "${CLICKHOUSE_URL}/"

echo "[reset] ensuring topic exists..."
rpk -X brokers="${REDPANDA_BROKERS}" topic create solana.raw-edges \
  --partitions 1 --replicas 1 \
  --config retention.ms=604800000 \
  --config cleanup.policy=delete \
  2>/dev/null || true

echo "[reset] seeking consumer groups to end (noop if groups don't exist yet)..."
rpk -X brokers="${REDPANDA_BROKERS}" group seek live-state --topics solana.raw-edges --to end 2>/dev/null || true
rpk -X brokers="${REDPANDA_BROKERS}" group seek ch-sink    --topics solana.raw-edges --to end 2>/dev/null || true

echo "[reset] done"
