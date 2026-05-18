//! `helen-cli pair` — Module O (mobile pairing v2) integration.
//!
//! Two modes:
//!   * Initiator: request a 6-digit pairing code, print + render QR.
//!   * Joiner   : redeem a code provided by another device.
use colored::*;
use qrcode::QrCode;
use serde::Deserialize;
use serde_json::json;

use crate::api::ApiClient;
use crate::error::{CliError, Result};

#[derive(Debug, Deserialize)]
struct PairRequest {
    pairing_code: String,
    expires_at: String,
    qr_url: Option<String>,
}

#[derive(Debug, Deserialize)]
struct PairRedeem {
    access_token: String,
    refresh_token: Option<String>,
    user_id: String,
    expires_in: u64,
}

pub async fn request_code(api: ApiClient) -> Result<()> {
    let r: PairRequest = api.post_json("/api/pair/v2/request", &json!({})).await?;
    println!("pairing code: {}", r.pairing_code.green().bold());
    println!("expires at  : {}", r.expires_at.dimmed());
    if let Some(url) = &r.qr_url {
        if let Ok(q) = QrCode::new(url.as_bytes()) {
            let s = q.render::<qrcode::render::unicode::Dense1x2>()
                .dark_color(qrcode::render::unicode::Dense1x2::Light)
                .light_color(qrcode::render::unicode::Dense1x2::Dark)
                .build();
            println!("\n{s}");
        }
    }
    Ok(())
}

pub async fn redeem(api: ApiClient, code: &str) -> Result<()> {
    let body = json!({"pairing_code": code});
    let r: PairRedeem = api.post_json("/api/pair/v2/redeem", &body).await?;
    if let Some(rt) = &r.refresh_token {
        crate::auth::AuthManager::store_refresh_token(rt)?;
    }
    println!("paired as user_id={} (expires_in {}s)", r.user_id, r.expires_in);
    Ok(())
}
