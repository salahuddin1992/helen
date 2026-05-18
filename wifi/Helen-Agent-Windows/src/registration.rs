//! First-time agent registration with the Helen-Server.

use reqwest::Client;
use serde::{Deserialize, Serialize};
use tracing::info;

use crate::config::ConfigStore;
use crate::error::{AgentError, Result};
use crate::fingerprint;
use crate::sysinfo::Collector;

#[derive(Debug, Clone, Serialize)]
struct RegisterRequest {
    fingerprint: String,
    hostname: String,
    os_name: String,
    os_version: String,
    agent_version: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RegisterResponse {
    pub agent_id: String,
    pub refresh_token: String,
    #[serde(default)]
    pub message: Option<String>,
}

pub async fn register(cfg: &ConfigStore, server_url: Option<String>) -> Result<RegisterResponse> {
    if let Some(url) = server_url {
        cfg.update(|c| c.server_url = url)?;
    }
    let snap = cfg.snapshot();
    let http = Client::builder()
        .timeout(std::time::Duration::from_secs(snap.http_timeout_secs))
        .danger_accept_invalid_certs(!snap.verify_tls)
        .build()
        .map_err(AgentError::Http)?;

    let fp = fingerprint::current_or_compute()?;
    let mut collector = Collector::new();
    let snapshot = collector.snapshot();
    let req = RegisterRequest {
        fingerprint: fp.clone(),
        hostname: snapshot.hostname.clone(),
        os_name: snapshot.os.name.clone(),
        os_version: snapshot.os.long_os_version.clone().unwrap_or(snapshot.os.version.clone()),
        agent_version: env!("CARGO_PKG_VERSION").into(),
    };
    let url = format!(
        "{}/api/agents/register",
        snap.server_url.trim_end_matches('/')
    );
    let resp = http.post(&url).json(&req).send().await?;
    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(AgentError::Auth(format!("register failed {status}: {body}")));
    }
    let parsed: RegisterResponse = resp.json().await?;
    cfg.update(|c| {
        c.agent_id = Some(parsed.agent_id.clone());
        c.refresh_token = Some(parsed.refresh_token.clone());
    })?;
    info!(target: "register", agent_id = %parsed.agent_id, fingerprint = %fp, "registered");
    Ok(parsed)
}
