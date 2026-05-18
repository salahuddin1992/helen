//! `helen-cli call` — degraded-path audio call.
//!
//! Full WebRTC ICE/DTLS/SRTP is out of scope for this binary. Instead we
//! implement a server-relayed audio path:
//!   * Server has an existing "audio relay" REST endpoint per channel
//!     (mounted by the calls module).
//!   * We open a Socket.IO room for the call, encode 20 ms PCM frames
//!     with Opus, and push them as base64 in `audio:frame` events.
//!   * Incoming frames go through the inverse pipeline.
//!
//! This keeps the CLI useful behind tight NATs where direct media paths
//! fail — the same fallback the mobile/desktop clients use.

use std::time::Duration;

use base64::{engine::general_purpose, Engine as _};
use colored::*;
use serde_json::json;
use tracing::{info, warn};

use crate::api::ApiClient;
use crate::audio::{AudioCapture, AudioPlayback, OpusCodec};
use crate::error::Result;
use crate::socket::SocketClient;

pub async fn call(
    api: ApiClient,
    sock: SocketClient,
    channel_id: String,
    duration_secs: Option<u64>,
) -> Result<()> {
    // 1) Tell server we're starting an audio session — server returns a
    //    relay token that scopes our Socket.IO events.
    let join: serde_json::Value = api.post_json(
        "/api/calls/audio/start",
        &json!({"channel_id": channel_id}),
    ).await?;
    let call_id = join.get("call_id")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    info!("call_id = {call_id}");
    println!("{} call started in channel {} (call_id {})",
        "✓".green(), channel_id, call_id);

    // 2) Capture/playback streams + codec.
    let mut cap = AudioCapture::start(None)?;
    let play = AudioPlayback::start(None)?;
    let codec = std::sync::Arc::new(parking_lot::Mutex::new(OpusCodec::new()?));

    // 3) Subscribe to socket and run the connect loop.
    let sock_run = sock.clone();
    let runner = tokio::spawn(async move { sock_run.run_forever().await });

    // Out: capture → opus → emit
    let s_out = sock.clone();
    let c_out = codec.clone();
    let chan_out = channel_id.clone();
    let call_out = call_id.to_string();
    let out_task = tokio::spawn(async move {
        while let Some(pcm) = cap.frames.recv().await {
            let pkt = match c_out.lock().encode(&pcm) {
                Ok(p) => p,
                Err(e) => { warn!("encode: {e}"); continue; }
            };
            let b64 = general_purpose::STANDARD.encode(&pkt);
            let _ = s_out.emit("audio:frame", json!({
                "call_id": call_out, "channel_id": chan_out, "data": b64,
            })).await;
        }
    });

    // In: socket → opus decode → playback
    let s_in = sock.clone();
    let c_in = codec.clone();
    let play_tx = play.tx.clone();
    let in_task = tokio::spawn(async move {
        let mut rx = s_in.subscribe();
        while let Ok(ev) = rx.recv().await {
            if ev.event != "audio:frame" { continue; }
            let Some(data) = ev.payload.get("data").and_then(|v| v.as_str()) else { continue; };
            let Ok(pkt) = general_purpose::STANDARD.decode(data) else { continue; };
            let frame = match c_in.lock().decode(&pkt) {
                Ok(f) => f,
                Err(e) => { warn!("decode: {e}"); continue; }
            };
            let _ = play_tx.send(frame).await;
        }
    });

    // 4) Wait for either a manual timeout or Ctrl-C.
    let timeout = duration_secs.unwrap_or(3600);
    println!("--- talking ({}s max). Ctrl-C to hang up ---", timeout);
    let _ = tokio::time::timeout(
        Duration::from_secs(timeout),
        tokio::signal::ctrl_c(),
    ).await;

    out_task.abort(); in_task.abort(); runner.abort();
    let _ = api.post_json::<_, serde_json::Value>(
        "/api/calls/audio/stop",
        &json!({"call_id": call_id}),
    ).await;
    println!("{} call ended", "✓".green());
    Ok(())
}
