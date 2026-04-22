FROM rust:1-slim-bookworm AS chef
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        cmake build-essential pkg-config libsasl2-dev zlib1g-dev libcurl4-openssl-dev && \
    rm -rf /var/lib/apt/lists/*
RUN cargo install cargo-chef --locked
WORKDIR /app

FROM chef AS planner
COPY Cargo.toml Cargo.lock* ./
COPY src/ src/
RUN cargo chef prepare --recipe-path recipe.json

FROM chef AS builder
COPY --from=planner /app/recipe.json recipe.json
RUN cargo chef cook --release --recipe-path recipe.json
COPY Cargo.toml Cargo.lock* ./
COPY src/ src/
RUN cargo build --release --bin server

# ─── Runtime ────────────────────────────────────────────────────────────
FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/target/release/server /app/server

EXPOSE 8002

ENTRYPOINT ["/app/server"]
