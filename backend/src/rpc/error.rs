use thiserror::Error;

// `Clone` so a `Result<T, RpcError>` can be stored once in the
// `Singleflight` cell and handed back to every concurrent caller for
// the same key without taking ownership of the source error. All
// variants are owned (String / unit), so the derive is mechanical.
#[derive(Debug, Clone, Error)]
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
