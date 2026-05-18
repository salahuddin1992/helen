//! Self-update mechanism — downloads the latest binary into %TEMP%, verifies
//! integrity, and hands off to a helper that replaces the running binary and
//! restarts the service.

use std::path::{Path, PathBuf};

use reqwest::Client;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tokio::fs;
use tokio::io::AsyncWriteExt;
use tracing::{info, warn};

use crate::auth::AuthManager;
use crate::config::ConfigStore;
use crate::error::{AgentError, Result};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UpdateManifest {
    pub version: String,
    pub url: String,
    pub sha256: String,
    #[serde(default)]
    pub signature: Option<String>,
    #[serde(default)]
    pub notes: Option<String>,
}

pub struct Updater {
    cfg: ConfigStore,
    auth: AuthManager,
    http: Client,
}

impl Updater {
    pub fn new(cfg: ConfigStore, auth: AuthManager, http: Client) -> Self {
        Self { cfg, auth, http }
    }

    pub async fn check(&self) -> Result<Option<UpdateManifest>> {
        let snap = self.cfg.snapshot();
        let url = format!(
            "{}/api/agents/update/manifest",
            snap.server_url.trim_end_matches('/')
        );
        let token = self.auth.access_token().await?;
        let resp = self.http.get(&url).bearer_auth(token).send().await?;
        if !resp.status().is_success() {
            return Err(AgentError::Update(format!("manifest http {}", resp.status())));
        }
        let m: UpdateManifest = resp.json().await?;
        let current = env!("CARGO_PKG_VERSION");
        if version_gt(&m.version, current) {
            Ok(Some(m))
        } else {
            Ok(None)
        }
    }

    pub async fn run(&self) -> Result<()> {
        if let Some(manifest) = self.check().await? {
            info!(target: "updater", version = %manifest.version, "newer version available");
            let new_bin = self.download(&manifest).await?;
            self.swap_and_restart(&new_bin)?;
        } else {
            info!(target: "updater", "no update available");
        }
        Ok(())
    }

    async fn download(&self, m: &UpdateManifest) -> Result<PathBuf> {
        let token = self.auth.access_token().await?;
        let mut resp = self.http.get(&m.url).bearer_auth(token).send().await?;
        if !resp.status().is_success() {
            return Err(AgentError::Update(format!("download http {}", resp.status())));
        }
        let tmp_dir = std::env::temp_dir().join("Helen-Agent-Update");
        fs::create_dir_all(&tmp_dir).await?;
        let dest = tmp_dir.join("helen-agent-update.exe");
        let mut file = fs::File::create(&dest).await?;
        let mut hasher = Sha256::new();
        while let Some(chunk) = resp.chunk().await? {
            hasher.update(&chunk);
            file.write_all(&chunk).await?;
        }
        file.flush().await?;
        drop(file);
        let actual = hex::encode(hasher.finalize());
        if actual != m.sha256 {
            warn!(target: "updater", expected = %m.sha256, actual = %actual, "checksum mismatch");
            return Err(AgentError::IntegrityMismatch {
                expected: m.sha256.clone(),
                actual,
            });
        }
        Ok(dest)
    }

    fn swap_and_restart(&self, new_bin: &Path) -> Result<()> {
        let current = std::env::current_exe().map_err(AgentError::Io)?;
        let bak = current.with_extension("bak.exe");
        // Stage helper script that performs the swap after the process exits.
        let script = format!(
            "@echo off\r\ntimeout /t 2 /nobreak > nul\r\ncopy /Y \"{cur}\" \"{bak}\"\r\ncopy /Y \"{new}\" \"{cur}\"\r\nsc start HelenAgent\r\n",
            cur = current.display(),
            bak = bak.display(),
            new = new_bin.display()
        );
        let script_path = std::env::temp_dir().join("helen-agent-swap.cmd");
        std::fs::write(&script_path, script)?;
        std::process::Command::new("cmd.exe")
            .arg("/C")
            .arg(&script_path)
            .spawn()
            .map_err(AgentError::Io)?;
        Ok(())
    }
}

pub async fn run_update_from_url(_manifest_url: &str) -> Result<()> {
    // Lightweight path used by WebSocket-triggered updates — relies on the
    // standard manifest endpoint, regardless of the URL provided.
    let cfg = ConfigStore::load_or_create()?;
    let http = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(60))
        .build()
        .map_err(AgentError::Http)?;
    let auth = AuthManager::new(cfg.clone(), http.clone());
    let u = Updater::new(cfg, auth, http);
    u.run().await
}

fn version_gt(a: &str, b: &str) -> bool {
    let pa = parse_version(a);
    let pb = parse_version(b);
    pa > pb
}

fn parse_version(s: &str) -> Vec<u32> {
    s.split('.')
        .filter_map(|p| p.split('-').next().and_then(|s| s.parse::<u32>().ok()))
        .collect()
}
