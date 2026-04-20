FROM rust:1-slim-bookworm AS builder

WORKDIR /app

# Cache deps with a dummy main, then build real source
COPY Cargo.toml ./
RUN mkdir -p src && echo 'fn main() {}' > src/main.rs && \
    cargo build --release --bin server && \
    rm -rf src target/release/deps/multichain_engine* \
           target/release/multichain-engine* \
           target/release/server*

COPY src/ src/
RUN touch src/main.rs && cargo build --release --bin server

# ─── Runtime ────────────────────────────────────────────────────────────
FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/target/release/server /app/server

EXPOSE 8002

ENTRYPOINT ["/app/server"]
