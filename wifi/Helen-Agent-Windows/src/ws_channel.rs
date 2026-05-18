//! Persistent control WebSocket — receives commands from the server and emits
//! events back. Auto-reconnect with exponential backoff and keepalive pings.

use std::sync::Arc;
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use parking_lot::Mutex;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use tokio::sync::mpsc;
use tokio::time::{sleep, Instant};
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{connect_async, MaybeTlsStream, WebSocketStream};
use tracing::{debug, error, info, warn};
use url::Url;

use crate::auth::AuthManager;
use crate::commands::{self, CommandRequest, CommandStreamEvent};
use crate::config::{AgentConfig, ConfigStore};
use crate::error::{AgentError, Result};
use crate::files::{DownloadMeta, FileService};

type WsStream = WebSocketStream<MaybeTlsStream<tokio::net::TcpStream>>;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum InboundFrame {
    Ping { ts: i64 },
    Exec(CommandRequest),
    DownloadFile(DownloadMeta),
    UploadFile { source_path: String },
    ScreenCapture { session_id: String },
    Update { manifest_url: String },
    Shutdown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum OutboundFrame {
    Pong { ts: i64 },
    CommandStream(CommandStreamEvent),
    UploadComplete { source_path: String, file_id: String, sha256: String, bytes: u64 },
    UploadFailed { source_path: String, error: String },
    DownloadComplete { destination: String },
    DownloadFailed { destination: String, error: String },
    ScreenChunk { session_id: String, sequence: u64, png_base64: String },
    ScreenFinished { session_id: String },
    Error { context: String, message: String },
    AgentInfo { version: String },
}

pub struct ControlClient {
    cfg: ConfigStore,
    auth: AuthManager,
    http: Client,
}

impl ControlClient {
    pub fn new(cfg: ConfigStore, auth: AuthManager, http: Client) -> Self {
        Self { cfg, auth, http }
    }

    pub async fn run_forever(self: Arc<Self>) {
        let mut backoff = Duration::from_secs(1);
        let max_backoff = Duration::from_secs(60);
        loop {
            match self.connect_once().await {
                Ok(()) => {
                    info!(target: "ws", "control channel closed cleanly; reconnecting");
                    backoff = Duration::from_secs(1);
                }
                Err(e) => {
                    error!(target: "ws", error = %e, "control channel error");
                }
            }
            sleep(backoff).await;
            backoff = (backoff * 2).min(max_backoff);
        }
    }

    async fn connect_once(&self) -> Result<()> {
        let snap = self.cfg.snapshot();
        let agent_id = snap
            .agent_id
            .clone()
            .ok_or_else(|| AgentError::Config("agent_id missing".into()))?;
        let mut url = Url::parse(&snap.server_url)?;
        let scheme = match url.scheme() {
            "http" => "ws",
            "https" => "wss",
            other => return Err(AgentError::Config(format!("unsupported scheme: {other}"))),
        };
        url.set_scheme(scheme)
            .map_err(|_| AgentError::Config("scheme set failed".into()))?;
        url.set_path(&format!("/api/agents/{}/control", agent_id));

        let token = self.auth.access_token().await?;
        let mut request = url.as_str().into_client_request().map_err(AgentError::from)?;
        let auth_value = HeaderValue::from_str(&format!("Bearer {token}"))
            .map_err(|e| AgentError::Internal(e.to_string()))?;
        request
            .headers_mut()
            .insert("Authorization", auth_value);

        info!(target: "ws", url = %url, "connecting control channel");
        let (ws_stream, _resp) = connect_async(request).await.map_err(AgentError::from)?;
        let (mut writer, mut reader) = ws_stream.split();

        // Outbound channel — events from various subsystems serialise here.
        let (tx, mut rx) = mpsc::channel::<OutboundFrame>(256);

        // Send hello / version
        let _ = tx
            .send(OutboundFrame::AgentInfo {
                version: env!("CARGO_PKG_VERSION").into(),
            })
            .await;

        // Spawn writer task
        let writer_task = tokio::spawn(async move {
            let mut last_ping = Instant::now();
            loop {
                tokio::select! {
                    item = rx.recv() => {
                        match item {
                            Some(frame) => {
                                let body = match serde_json::to_string(&frame) {
                                    Ok(s) => s,
                                    Err(e) => { warn!(target: "ws", error = %e, "serialize"); continue; }
                                };
                                if writer.send(Message::Text(body)).await.is_err() { break; }
                            }
                            None => break,
                        }
                    }
                    _ = sleep(Duration::from_secs(15)) => {
                        if last_ping.elapsed() > Duration::from_secs(15) {
                            if writer.send(Message::Ping(vec![])).await.is_err() { break; }
                            last_ping = Instant::now();
                        }
                    }
                }
            }
            debug!(target: "ws", "writer task exit");
        });

        let cfg = self.cfg.snapshot();
        let files = FileService::new(self.http.clone(), self.auth.clone(), cfg.clone());
        let inflight: Arc<Mutex<Vec<tokio::task::JoinHandle<()>>>> = Arc::new(Mutex::new(vec![]));

        while let Some(msg) = reader.next().await {
            let msg = msg.map_err(AgentError::from)?;
            match msg {
                Message::Text(txt) => {
                    let frame: InboundFrame = match serde_json::from_str(&txt) {
                        Ok(v) => v,
                        Err(e) => {
                            warn!(target: "ws", error = %e, raw = %txt, "bad frame");
                            continue;
                        }
                    };
                    let tx2 = tx.clone();
                    let cfg2 = cfg.clone();
                    let files_clone = files.clone();
                    let inflight2 = inflight.clone();
                    let h = tokio::spawn(async move {
                        handle_inbound(frame, cfg2, files_clone, tx2).await;
                    });
                    inflight2.lock().push(h);
                }
                Message::Binary(_) => {}
                Message::Ping(p) => {
                    debug!(target: "ws", "received ping");
                    let _ = tx
                        .send(OutboundFrame::Pong {
                            ts: chrono::Utc::now().timestamp(),
                        })
                        .await;
                    let _ = p;
                }
                Message::Pong(_) => {}
                Message::Close(_) => {
                    info!(target: "ws", "server closed channel");
                    break;
                }
                Message::Frame(_) => {}
            }
        }
        writer_task.abort();
        Ok(())
    }
}

async fn handle_inbound(
    frame: InboundFrame,
    cfg: AgentConfig,
    files: FileService,
    tx: mpsc::Sender<OutboundFrame>,
) {
    match frame {
        InboundFrame::Ping { ts } => {
            let _ = tx.send(OutboundFrame::Pong { ts }).await;
        }
        InboundFrame::Exec(req) => {
            let (etx, mut erx) = mpsc::channel::<CommandStreamEvent>(256);
            let tx2 = tx.clone();
            let stream_task = tokio::spawn(async move {
                while let Some(evt) = erx.recv().await {
                    let _ = tx2.send(OutboundFrame::CommandStream(evt)).await;
                }
            });
            if let Err(e) = commands::execute(&cfg, req.clone(), etx).await {
                let _ = tx
                    .send(OutboundFrame::Error {
                        context: format!("exec:{}", req.command_id),
                        message: e.to_string(),
                    })
                    .await;
            }
            let _ = stream_task.await;
        }
        InboundFrame::DownloadFile(meta) => {
            let dest = meta.destination.clone();
            match files.download(meta).await {
                Ok(path) => {
                    let _ = tx
                        .send(OutboundFrame::DownloadComplete {
                            destination: path.display().to_string(),
                        })
                        .await;
                }
                Err(e) => {
                    let _ = tx
                        .send(OutboundFrame::DownloadFailed {
                            destination: dest,
                            error: e.to_string(),
                        })
                        .await;
                }
            }
        }
        InboundFrame::UploadFile { source_path } => {
            let p = std::path::PathBuf::from(&source_path);
            match files.upload(&p).await {
                Ok(r) => {
                    let _ = tx
                        .send(OutboundFrame::UploadComplete {
                            source_path,
                            file_id: r.file_id,
                            sha256: r.sha256,
                            bytes: r.bytes,
                        })
                        .await;
                }
                Err(e) => {
                    let _ = tx
                        .send(OutboundFrame::UploadFailed {
                            source_path,
                            error: e.to_string(),
                        })
                        .await;
                }
            }
        }
        InboundFrame::ScreenCapture { session_id } => {
            match crate::screen::capture_with_consent(&cfg).await {
                Ok(png) => {
                    use base64::Engine;
                    let encoded =
                        base64::engine::general_purpose::STANDARD.encode(&png);
                    let _ = tx
                        .send(OutboundFrame::ScreenChunk {
                            session_id: session_id.clone(),
                            sequence: 0,
                            png_base64: encoded,
                        })
                        .await;
                    let _ = tx
                        .send(OutboundFrame::ScreenFinished { session_id })
                        .await;
                }
                Err(e) => {
                    let _ = tx
                        .send(OutboundFrame::Error {
                            context: format!("screen:{session_id}"),
                            message: e.to_string(),
                        })
                        .await;
                }
            }
        }
        InboundFrame::Update { manifest_url } => {
            if let Err(e) = crate::updater::run_update_from_url(&manifest_url).await {
                let _ = tx
                    .send(OutboundFrame::Error {
                        context: "update".into(),
                        message: e.to_string(),
                    })
                    .await;
            }
        }
        InboundFrame::Shutdown => {
            info!(target: "ws", "server requested shutdown");
            std::process::exit(0);
        }
    }
}

