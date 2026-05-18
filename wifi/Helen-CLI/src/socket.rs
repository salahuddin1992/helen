//! Socket.IO async client with auto-reconnect & exponential backoff.
use std::sync::Arc;
use std::time::Duration;

use rust_socketio::asynchronous::{Client as SioClient, ClientBuilder};
use rust_socketio::Payload;
use tokio::sync::{broadcast, RwLock};
use tracing::{error, info, warn};

use crate::auth::AuthManager;
use crate::config::ConfigStore;
use crate::error::{CliError, Result};

#[derive(Clone, Debug)]
pub struct SocketEvent {
    pub event: String,
    pub payload: serde_json::Value,
}

#[derive(Clone)]
pub struct SocketClient {
    cfg: ConfigStore,
    auth: AuthManager,
    inner: Arc<RwLock<Option<SioClient>>>,
    tx: broadcast::Sender<SocketEvent>,
}

impl SocketClient {
    pub fn new(cfg: ConfigStore, auth: AuthManager) -> Self {
        let (tx, _rx) = broadcast::channel(256);
        Self {
            cfg, auth,
            inner: Arc::new(RwLock::new(None)),
            tx,
        }
    }

    pub fn subscribe(&self) -> broadcast::Receiver<SocketEvent> { self.tx.subscribe() }

    pub async fn connect(&self) -> Result<()> {
        let url = self.cfg.server_url()?;
        let tok = self.auth.access_token().await?;
        let tx = self.tx.clone();

        let builder = ClientBuilder::new(url)
            .opening_header("Authorization", format!("Bearer {tok}"))
            .reconnect(true)
            .reconnect_on_disconnect(true)
            .max_reconnect_attempts(0)
            .reconnect_delay(500, 30_000)
            .on_any(move |ev, payload, _client| {
                let tx = tx.clone();
                Box::pin(async move {
                    let event = match ev {
                        rust_socketio::Event::Custom(e) => e,
                        rust_socketio::Event::Message => "message".into(),
                        rust_socketio::Event::Connect => "connect".into(),
                        rust_socketio::Event::Close => "disconnect".into(),
                        rust_socketio::Event::Error => "error".into(),
                        rust_socketio::Event::Open => "open".into(),
                    };
                    let value = match payload {
                        Payload::Text(t) => serde_json::Value::Array(t),
                        Payload::String(s) => serde_json::Value::String(s),
                        Payload::Binary(b) => serde_json::json!({"binary_len": b.len()}),
                    };
                    let _ = tx.send(SocketEvent { event, payload: value });
                })
            });

        let client = builder.connect().await
            .map_err(|e| CliError::Socket(e.to_string()))?;
        *self.inner.write().await = Some(client);
        info!("socket.io connected");
        Ok(())
    }

    pub async fn emit(&self, event: &str, payload: serde_json::Value) -> Result<()> {
        let g = self.inner.read().await;
        let c = g.as_ref().ok_or_else(|| CliError::Socket("not connected".into()))?;
        c.emit(event, payload).await
            .map_err(|e| CliError::Socket(e.to_string()))
    }

    pub async fn disconnect(&self) {
        let mut g = self.inner.write().await;
        if let Some(c) = g.take() {
            let _ = c.disconnect().await;
        }
    }

    /// Loop that reconnects whenever the inner client dies.
    pub async fn run_forever(self) {
        let mut backoff_ms: u64 = 500;
        loop {
            match self.connect().await {
                Ok(_) => {
                    backoff_ms = 500;
                    // wait until socket closes
                    let mut rx = self.tx.subscribe();
                    while let Ok(ev) = rx.recv().await {
                        if ev.event == "disconnect" || ev.event == "error" {
                            warn!("socket.io disconnected, will reconnect");
                            break;
                        }
                    }
                }
                Err(e) => {
                    error!("socket connect failed: {e}");
                }
            }
            tokio::time::sleep(Duration::from_millis(backoff_ms)).await;
            backoff_ms = (backoff_ms * 2).min(30_000);
        }
    }
}
