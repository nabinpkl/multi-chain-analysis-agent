use thiserror::Error;

#[derive(Debug, Error)]
pub enum RpcError {
    #[error("slot was skipped (not produced)")]
    SkippedSlot,

    #[error("block not yet available at tip")]
    NotYetAvailable,

    #[error("rate limited by provider")]
    RateLimited,

    #[error("transient: {0}")]
    Transient(String),

    #[error("fatal: {0}")]
    Fatal(String),
}
