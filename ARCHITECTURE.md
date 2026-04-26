
# Why json over protobuf? Value: serialized Edge record. Start with JSON for debuggability, switch to MessagePack/Protobuf when payload size matters.

# Why kafka/redpanda over jsonl or direct clickhouse?

# Why clickhouse?

# Why rust?

# Why sse to brodcast?

# what does Delta changes state machine model do?

Arcitecture
ingester → redpanda → ch-sink (persist)
                    → graph-engine (in-memory graphology-equivalent in Rust)
                                    ↓
                            snapshot + delta broadcast
                                    ↓
                    /graph/snapshot (initial) + /graph/stream (deltas SSE)
                                    ↓
                            frontend = thin renderer