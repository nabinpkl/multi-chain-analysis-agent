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

# Ship 1 of agent-observability (ADR 13). The OTel collector's
# clickhouseexporter creates its own tables with `create_schema: true`
# but won't create the database itself in v0.118.0; pre-create here so
# the collector boots cleanly. The exporter then owns the schema of
# `otel_traces` (and `otel_logs` if logs ever flow). This database
# lives alongside `multichain` on the same instance (CH-A) so SQL can
# join across them, per the architectural decision in ADR 13.
echo "[reset] ensuring otel database exists..."
curl -sS --fail -u "${CLICKHOUSE_USER}:${CLICKHOUSE_PASSWORD}" \
  --data "CREATE DATABASE IF NOT EXISTS otel" \
  "${CLICKHOUSE_URL}/"

echo "[reset] ensuring topic exists..."
rpk -X brokers="${REDPANDA_BROKERS}" topic create solana.raw-edges \
  --partitions 1 --replicas 1 \
  --config retention.ms=604800000 \
  --config cleanup.policy=delete \
  2>/dev/null || true

# Seek both groups to the topic's end so the new api doesn't replay a
# committed-offset backlog from a previous uptime. rpk rejects seek on a
# non-empty group (active members), which happens when the old api
# container hasn't fully unregistered yet  wait it out. We stop caring
# once the group is empty OR we've retried enough; any failure here just
# means the next run will replay its backlog (ugly but not fatal).
seek_group_to_end() {
    group=$1
    i=0
    while [ "$i" -lt 30 ]; do
        state=$(rpk -X brokers="${REDPANDA_BROKERS}" group describe "$group" 2>/dev/null \
            | awk '/^STATE/ {print $2}')
        # Group doesn't exist yet  first boot or deleted. Nothing to seek.
        if [ -z "$state" ]; then
            echo "[reset] $group: not yet created, skipping"
            return 0
        fi
        if [ "$state" = "Empty" ] || [ "$state" = "Dead" ]; then
            rpk -X brokers="${REDPANDA_BROKERS}" group seek "$group" \
                --topics solana.raw-edges --to end >/dev/null 2>&1 \
                && { echo "[reset] $group: seeked to end"; return 0; }
            echo "[reset] $group: seek failed unexpectedly"
            return 1
        fi
        i=$((i + 1))
        sleep 1
    done
    echo "[reset] $group: still non-empty after 30s, giving up (will replay backlog)"
    return 1
}

echo "[reset] seeking consumer groups to end..."
seek_group_to_end graph-engine || true
seek_group_to_end ch-sink      || true

echo "[reset] done"
