//! Centralised error hierarchy for the agent.
//!
//! Every fallible path in the codebase returns [`AgentError`] (or wraps it in
//! `anyhow::Result` at the very top of `main`). Domain errors keep their own
//! variant so that retry / shutdown decisions are explicit.

use std::io;
use thiserror::Error;

pub type Result<T> = std::result::Result<T, AgentError>;

#[derive(Debug, Error)]
pub enum AgentError {
    #[error("configuration error: {0}")]
    Config(String),

    #[error("registry / fingerprint error: {0}")]
    Fingerprint(String),

    #[error("HTTP transport error: {0}")]
    Http(#[from] reqwest::Error),

    #[error("WebSocket error: {0}")]
    Ws(String),

    #[error("authentication / token error: {0}")]
    Auth(String),

    #[error("command rejected: {0}")]
    CommandRejected(String),

    #[error("command timed out after {0}s")]
    CommandTimeout(u64),

    #[error("path sandboxing denied: {0}")]
    PathDenied(String),

    #[error("file transfer failed: {0}")]
    FileTransfer(String),

    #[error("integrity mismatch (expected {expected}, got {actual})")]
    IntegrityMismatch { expected: String, actual: String },

    #[error("update failed: {0}")]
    Update(String),

    #[error("service error: {0}")]
    Service(String),

    #[error("io error: {0}")]
    Io(#[from] io::Error),

    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("toml error: {0}")]
    Toml(String),

    #[error("url parse error: {0}")]
    Url(#[from] url::ParseError),

    #[error("internal error: {0}")]
    Internal(String),
}

impl From<toml::de::Error> for AgentError {
    fn from(err: toml::de::Error) -> Self {
        AgentError::Toml(err.to_string())
    }
}

impl From<toml::ser::Error> for AgentError {
    fn from(err: toml::ser::Error) -> Self {
        AgentError::Toml(err.to_string())
    }
}

impl From<tokio_tungstenite::tungstenite::Error> for AgentError {
    fn from(err: tokio_tungstenite::tungstenite::Error) -> Self {
        AgentError::Ws(err.to_string())
    }
}

impl AgentError {
    pub fn is_retryable(&self) -> bool {
        matches!(
            self,
            AgentError::Http(_)
                | AgentError::Ws(_)
                | AgentError::Auth(_)
                | AgentError::Io(_)
        )
    }
}
