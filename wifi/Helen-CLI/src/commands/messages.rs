//! `helen-cli messages` — list / send / edit / delete + --watch tail.
use std::time::Duration;

use serde::Deserialize;
use serde_json::json;

use crate::api::ApiClient;
use crate::error::{CliError, Result};
use crate::socket::SocketClient;
use crate::util::{print_message_bubble, MessageRow};

#[derive(Debug, Deserialize)]
struct CreatedMessage { id: String }

pub async fn list(api: ApiClient, channel_id: &str, limit: usize) -> Result<()> {
    let path = format!("/api/messages?channel_id={channel_id}&limit={limit}");
    let rows: Vec<MessageRow> = api.get_json(&path).await?;
    for m in rows.iter().rev() {
        print_message_bubble(m);
    }
    Ok(())
}

pub async fn send(api: ApiClient, channel_id: &str, body: &str) -> Result<()> {
    let payload = json!({"channel_id": channel_id, "content": body});
    let m: CreatedMessage = api.post_json("/api/messages", &payload).await?;
    println!("sent: {}", m.id);
    Ok(())
}

pub async fn edit(api: ApiClient, message_id: &str, body: &str) -> Result<()> {
    let payload = json!({"content": body});
    let _: serde_json::Value = api.post_json(
        &format!("/api/messages/{message_id}/edit"), &payload,
    ).await?;
    println!("edited");
    Ok(())
}

pub async fn delete(api: ApiClient, message_id: &str) -> Result<()> {
    api.delete(&format!("/api/messages/{message_id}")).await?;
    println!("deleted");
    Ok(())
}

pub async fn watch(api: ApiClient, sock: SocketClient, channel_id: String) -> Result<()> {
    list(api.clone(), &channel_id, 30).await?;
    let s2 = sock.clone();
    let rt = tokio::spawn(async move { s2.run_forever().await });
    let mut rx = sock.subscribe();
    println!("--- watching {channel_id} (Ctrl-C to exit) ---");
    loop {
        let evt = tokio::time::timeout(Duration::from_secs(60), rx.recv()).await;
        match evt {
            Ok(Ok(ev)) if ev.event == "message:new" => {
                let chan = ev.payload.get("channel_id")
                    .and_then(|v| v.as_str()).unwrap_or("");
                if chan != channel_id { continue; }
                let m = MessageRow {
                    id: ev.payload.get("id").and_then(|v| v.as_str()).unwrap_or("").into(),
                    channel_id: chan.into(),
                    sender_id: ev.payload.get("sender_id")
                        .and_then(|v| v.as_str()).unwrap_or("").into(),
                    sender_name: ev.payload.get("sender_name")
                        .and_then(|v| v.as_str()).map(str::to_string),
                    content: ev.payload.get("content")
                        .and_then(|v| v.as_str()).unwrap_or("").into(),
                    created_at: ev.payload.get("created_at")
                        .and_then(|v| v.as_str()).unwrap_or("").into(),
                };
                print_message_bubble(&m);
            }
            Ok(Ok(_)) => {}
            Ok(Err(_)) => {
                return Err(CliError::Socket("watch channel closed".into()));
            }
            Err(_) => {} // timeout — keep waiting
        }
        if rt.is_finished() { break; }
    }
    Ok(())
}
