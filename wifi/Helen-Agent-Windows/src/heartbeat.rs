//! Heartbeat task — periodic POST of system snapshot, exponential backoff on
//! failure, observable state transitions.

use std::sync::Arc;
use std::time::Duration;

use parking_lot::Mutex;
use reqwest::Client;
use serde::Serialize;
use tokio::sync::watch;
use tokio::time::{sleep, Instant};
use tracing::{debug, info, warn};

use crate::auth::AuthManager;
use crate::config::ConfigStore;
use crate::error::{AgentError, Result};
use crate::sysinfo::{Collector, SystemSnapshot};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LinkState {
    Connected,
    Reconnecting,
    Disconnected,
}

#[derive(Clone)]
pub struct HeartbeatHandle {
    state_rx: watch::Receiver<LinkState>,
}

impl HeartbeatHandle {
    pub fn state(&self) -> LinkState {
        *self.state_rx.borrow()
    }
}

pub fn spawn(cfg: ConfigStore, auth: AuthManager, http: Client) -> HeartbeatHandle {
    let (state_tx, state_rx) = watch::channel(LinkState::Disconnected);
    let collector = Arc::new(Mutex::new(Collector::new()));
    let h = HeartbeatHandle { state_rx };

    tokio::spawn(async move {
        let mut backoff = Duration::from_secs(1);
        let max_backoff = Duration::from_secs(60);
        loop {
            let snapshot_cfg = cfg.snapshot();
            let snap = {
                let mut c = collector.lock();
                c.snapshot()
            };
            match send_heartbeat(
                &http,
                &auth,
                &snapshot_cfg.server_url,
                &snap,
                &snapshot_cfg.agent_id,
            )
            .await
            {
                Ok(_) => {
                    if *state_tx.borrow() != LinkState::Connected {
                        info!(target: "heartbeat", "link state -> connected");
                    }
                    let _ = state_tx.send(LinkState::Connected);
                    backoff = Duration::from_secs(1);
                    let next = Duration::from_secs(snapshot_cfg.heartbeat_interval_secs);
                    sleep(next).await;
                }
                Err(err) => {
                    warn!(target: "heartbeat", error = %err, "heartbeat failed; will retry");
                    let _ = state_tx.send(LinkState::Reconnecting);
                    let wait = backoff;
                    backoff = (backoff * 2).min(max_backoff);
                    if backoff >= max_backoff {
                        let _ = state_tx.send(LinkState::Disconnected);
                    }
                    sleep(wait).await;
                }
            }
        }
    });
    h
}

async fn send_heartbeat(
    http: &Client,
    auth: &AuthManager,
    server_url: &str,
    snap: &SystemSnapshot,
    agent_id: &Option<String>,
) -> Result<()> {
    let agent_id = agent_id
        .clone()
        .ok_or_else(|| AgentError::Config("agent_id missing".into()))?;
    let url = format!(
        "{}/api/agents/{}/heartbeat",
        server_url.trim_end_matches('/'),
        agent_id
    );
    let token = auth.access_token().await?;
    let started = Instant::now();
    debug!(target: "heartbeat", url = %url, "sending");
    let resp = http
        .post(&url)
        .bearer_auth(token)
        .json(snap)
        .send()
        .await?;
    if !resp.status().is_success() {
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        return Err(AgentError::Internal(format!(
            "heartbeat http {} — {}",
            status, text
        )));
    }
    debug!(target: "heartbeat", duration_ms = started.elapsed().as_millis(), "heartbeat ok");
    Ok(())
}
