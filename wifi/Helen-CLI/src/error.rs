//! Error hierarchy for helen-cli.
use thiserror::Error;

#[derive(Error, Debug)]
pub enum CliError {
    #[error("config error: {0}")]
    Config(String),

    #[error("auth error: {0}")]
    Auth(String),

    #[error("HTTP {status}: {body}")]
    Http { status: u16, body: String },

    #[error("network error: {0}")]
    Network(#[from] reqwest::Error),

    #[error("socket error: {0}")]
    Socket(String),

    #[error("audio error: {0}")]
    Audio(String),

    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    #[error("serde error: {0}")]
    Serde(#[from] serde_json::Error),

    #[error("toml-de error: {0}")]
    TomlDe(#[from] toml::de::Error),

    #[error("toml-ser error: {0}")]
    TomlSer(#[from] toml::ser::Error),

    #[error("keyring error: {0}")]
    Keyring(String),

    #[error("pair error: {0}")]
    Pair(String),

    #[error("invalid argument: {0}")]
    InvalidArg(String),

    #[error("not logged in")]
    NotLoggedIn,

    #[error("operation cancelled")]
    Cancelled,

    #[error("other: {0}")]
    Other(String),
}

pub type Result<T> = std::result::Result<T, CliError>;

impl From<keyring::Error> for CliError {
    fn from(e: keyring::Error) -> Self { CliError::Keyring(e.to_string()) }
}

impl From<anyhow::Error> for CliError {
    fn from(e: anyhow::Error) -> Self { CliError::Other(e.to_string()) }
}
