pub mod auth;
pub mod drive;
pub mod fs;
pub mod model;
pub mod planner;
pub mod store;
pub mod transfer;

pub use model::*;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("{0}")]
    Invalid(String),
    #[error("The operation was cancelled.")]
    Cancelled,
    #[error("Files changed since this scan. Scan again before syncing.")]
    Stale,
    #[error("An unfinished transfer needs review in Activity before syncing again.")]
    RecoveryRequired,
    #[error("A filesystem operation failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("The local database could not be accessed: {0}")]
    Database(#[from] rusqlite::Error),
    #[error("Stored data could not be read: {0}")]
    Json(#[from] serde_json::Error),
    #[error("Google could not be reached. Check your connection and try again.")]
    Network(#[from] reqwest::Error),
    #[error("Google rejected the operation (HTTP {0}).")]
    Provider(u16),
    #[error("Sign in to Google to continue.")]
    SignedOut,
    #[error("Secure credential storage is unavailable. Your session was not saved.")]
    CredentialStore,
}

pub type Result<T> = std::result::Result<T, Error>;
#[cfg(test)]
mod sync_tests;
