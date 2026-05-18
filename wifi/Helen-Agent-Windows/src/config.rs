//! Persistent agent configuration.
//!
//! Stored under `%ProgramData%/Helen-Agent/config.toml` with restrictive ACLs.
//! Sensitive fields (refresh token) are also mirrored to the registry under
//! `HKLM\SOFTWARE\Helen-Agent` for the service to bootstrap before its working
//! directory exists.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use parking_lot::RwLock;
use serde::{Deserialize, Serialize};

use crate::error::{AgentError, Result};

pub const APP_DIR_NAME: &str = "Helen-Agent";
pub const CONFIG_FILE_NAME: &str = "config.toml";

#[cfg(target_os = "windows")]
pub const REGISTRY_KEY: &str = r"SOFTWARE\Helen-Agent";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentConfig {
    /// Base URL of the Helen-Server, including scheme & port.
    pub server_url: String,

    /// Server-assigned agent identifier. None until registration completes.
    #[serde(default)]
    pub agent_id: Option<String>,

    /// Long-lived refresh token; access tokens are obtained per session.
    #[serde(default)]
    pub refresh_token: Option<String>,

    /// Heartbeat interval in seconds.
    #[serde(default = "default_heartbeat")]
    pub heartbeat_interval_secs: u64,

    /// Optional override for command whitelist.
    #[serde(default = "default_whitelist")]
    pub command_whitelist: Vec<String>,

    /// Maximum command timeout in seconds.
    #[serde(default = "default_cmd_timeout")]
    pub command_timeout_secs: u64,

    /// Logging level filter.
    #[serde(default = "default_log_level")]
    pub log_level: String,

    /// Whether the agent verifies server TLS chain.
    #[serde(default = "default_true")]
    pub verify_tls: bool,

    /// HTTP request timeout (seconds).
    #[serde(default = "default_http_timeout")]
    pub http_timeout_secs: u64,

    /// Whether screen capture requires consent dialog. Default: true.
    #[serde(default = "default_true")]
    pub screen_capture_requires_consent: bool,
}

fn default_heartbeat() -> u64 { 30 }
fn default_cmd_timeout() -> u64 { 30 }
fn default_log_level() -> String { "info".into() }
fn default_http_timeout() -> u64 { 30 }
fn default_true() -> bool { true }
fn default_whitelist() -> Vec<String> {
    vec![
        "ipconfig".into(),
        "tasklist".into(),
        "systeminfo".into(),
        "netstat".into(),
        "whoami".into(),
        "hostname".into(),
        "ping".into(),
        "tracert".into(),
        "nslookup".into(),
        "getmac".into(),
        "wmic".into(),
        "powershell".into(),
    ]
}

impl Default for AgentConfig {
    fn default() -> Self {
        Self {
            server_url: "https://helen-server.local".into(),
            agent_id: None,
            refresh_token: None,
            heartbeat_interval_secs: default_heartbeat(),
            command_whitelist: default_whitelist(),
            command_timeout_secs: default_cmd_timeout(),
            log_level: default_log_level(),
            verify_tls: true,
            http_timeout_secs: default_http_timeout(),
            screen_capture_requires_consent: true,
        }
    }
}

#[derive(Clone)]
pub struct ConfigStore {
    inner: Arc<RwLock<AgentConfig>>,
    path: PathBuf,
}

impl ConfigStore {
    pub fn load_or_create() -> Result<Self> {
        let path = Self::default_path();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let cfg = if path.exists() {
            let raw = std::fs::read_to_string(&path)?;
            toml::from_str::<AgentConfig>(&raw)?
        } else {
            let c = AgentConfig::default();
            let s = toml::to_string_pretty(&c)?;
            std::fs::write(&path, s)?;
            c
        };
        let store = Self {
            inner: Arc::new(RwLock::new(cfg)),
            path,
        };
        store.validate()?;
        Ok(store)
    }

    pub fn default_path() -> PathBuf {
        if let Some(data) = std::env::var_os("ProgramData") {
            PathBuf::from(data).join(APP_DIR_NAME).join(CONFIG_FILE_NAME)
        } else {
            PathBuf::from(".").join(CONFIG_FILE_NAME)
        }
    }

    pub fn snapshot(&self) -> AgentConfig {
        self.inner.read().clone()
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn update<F: FnOnce(&mut AgentConfig)>(&self, f: F) -> Result<()> {
        {
            let mut guard = self.inner.write();
            f(&mut guard);
        }
        self.persist()
    }

    pub fn persist(&self) -> Result<()> {
        let s = toml::to_string_pretty(&*self.inner.read())?;
        std::fs::write(&self.path, s)?;
        Ok(())
    }

    pub fn validate(&self) -> Result<()> {
        let cfg = self.inner.read();
        if cfg.server_url.is_empty() {
            return Err(AgentError::Config("server_url is empty".into()));
        }
        url::Url::parse(&cfg.server_url)
            .map_err(|e| AgentError::Config(format!("invalid server_url: {e}")))?;
        if cfg.heartbeat_interval_secs < 5 {
            return Err(AgentError::Config(
                "heartbeat_interval_secs must be >= 5".into(),
            ));
        }
        if cfg.command_timeout_secs == 0 || cfg.command_timeout_secs > 300 {
            return Err(AgentError::Config(
                "command_timeout_secs must be in (0, 300]".into(),
            ));
        }
        Ok(())
    }

    pub fn sandbox_dir() -> PathBuf {
        if let Some(data) = std::env::var_os("ProgramData") {
            PathBuf::from(data).join(APP_DIR_NAME).join("sandbox")
        } else {
            PathBuf::from(".").join("sandbox")
        }
    }
}

// ── Registry helpers (Windows only) ──────────────────────────────

#[cfg(target_os = "windows")]
pub fn registry_get(name: &str) -> Result<Option<String>> {
    use winreg::enums::*;
    use winreg::RegKey;

    let hklm = RegKey::predef(HKEY_LOCAL_MACHINE);
    match hklm.open_subkey_with_flags(REGISTRY_KEY, KEY_READ) {
        Ok(key) => match key.get_value::<String, _>(name) {
            Ok(v) => Ok(Some(v)),
            Err(_) => Ok(None),
        },
        Err(_) => Ok(None),
    }
}

#[cfg(target_os = "windows")]
pub fn registry_set(name: &str, value: &str) -> Result<()> {
    use winreg::enums::*;
    use winreg::RegKey;

    let hklm = RegKey::predef(HKEY_LOCAL_MACHINE);
    let (key, _) = hklm
        .create_subkey_with_flags(REGISTRY_KEY, KEY_ALL_ACCESS)
        .map_err(|e| AgentError::Fingerprint(format!("registry create: {e}")))?;
    key.set_value(name, &value.to_string())
        .map_err(|e| AgentError::Fingerprint(format!("registry set: {e}")))?;
    Ok(())
}

#[cfg(not(target_os = "windows"))]
pub fn registry_get(_name: &str) -> Result<Option<String>> { Ok(None) }

#[cfg(not(target_os = "windows"))]
pub fn registry_set(_name: &str, _value: &str) -> Result<()> { Ok(()) }
