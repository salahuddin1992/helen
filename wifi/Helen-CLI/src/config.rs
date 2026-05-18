//! Persistent config at ``~/.helen/config.toml``.
use std::path::PathBuf;

use parking_lot::RwLock;
use serde::{Deserialize, Serialize};
use std::sync::Arc;

use crate::error::{CliError, Result};

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Config {
    pub server_url: Option<String>,
    pub last_user: Option<String>,
    pub default_channel: Option<String>,
    pub audio_input: Option<String>,
    pub audio_output: Option<String>,

    // Token storage. We persist refresh_token in keyring only, but cache
    // a hint here that the user *has* logged in.
    pub user_id: Option<String>,
    pub agent_id: Option<String>,

    // UI prefs
    pub color: bool,
}

#[derive(Clone)]
pub struct ConfigStore {
    path: PathBuf,
    inner: Arc<RwLock<Config>>,
}

impl ConfigStore {
    pub fn load_or_init() -> Result<Self> {
        let path = default_path()?;
        if let Some(p) = path.parent() {
            std::fs::create_dir_all(p).map_err(CliError::Io)?;
        }
        let cfg = if path.exists() {
            let raw = std::fs::read_to_string(&path).map_err(CliError::Io)?;
            toml::from_str::<Config>(&raw).unwrap_or_default()
        } else {
            let c = Config { color: true, ..Default::default() };
            std::fs::write(&path, toml::to_string_pretty(&c)?)
                .map_err(CliError::Io)?;
            c
        };
        Ok(Self { path, inner: Arc::new(RwLock::new(cfg)) })
    }

    pub fn path(&self) -> &PathBuf { &self.path }

    pub fn snapshot(&self) -> Config { self.inner.read().clone() }

    pub fn update<F>(&self, f: F) -> Result<()>
    where
        F: FnOnce(&mut Config),
    {
        {
            let mut g = self.inner.write();
            f(&mut *g);
        }
        let txt = toml::to_string_pretty(&*self.inner.read())?;
        std::fs::write(&self.path, txt).map_err(CliError::Io)
    }

    pub fn server_url(&self) -> Result<String> {
        self.inner
            .read()
            .server_url
            .clone()
            .ok_or_else(|| CliError::Config("server_url not set — run `helen-cli login --server <url>`".into()))
    }
}

pub fn default_path() -> Result<PathBuf> {
    let base = dirs::home_dir()
        .ok_or_else(|| CliError::Config("could not resolve home dir".into()))?;
    Ok(base.join(".helen").join("config.toml"))
}
