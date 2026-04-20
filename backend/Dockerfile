FROM rust:1.87-slim AS builder

WORKDIR /app

# Copy manifests first for dependency caching
COPY Cargo.toml Cargo.lock ./

# Create dummy src to build dependencies
RUN mkdir -p src/bin data && \
    echo 'fn main() {}' > src/main.rs && \
    echo 'fn main() {}' > src/bin/domain_scan.rs && \
    echo '' > src/lib.rs && \
    echo '{}' > data/wordlist.json && \
    cargo build --release --bin server 2>/dev/null || true && \
    rm -rf src data && \
    rm -rf target/release/.fingerprint/justgetdomain-* \
           target/release/deps/justgetdomain-* \
           target/release/deps/libjustgetdomain-*

# Copy real source and data needed at compile time
COPY src/ src/
COPY data/wordlist.json data/wordlist.json

RUN cargo build --release --bin server

# ─── Runtime ────────────────────────────────────────────────────────────

FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/target/release/server /app/server

EXPOSE 3001

ENTRYPOINT ["/app/server"]
