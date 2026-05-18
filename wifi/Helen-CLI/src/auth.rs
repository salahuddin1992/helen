//! Token management for helen-cli.
//!
//! - Refresh token stored in OS keyring (`keyring` crate).
//! - Access token cached in memory and auto-refreshed before expiry.
use std::sync::Arc;
use std::time::{Duration, Instant};

use parking_lot::RwLock;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use tokio::sync::Mutex;
use tracing::{debug, info, warn};

use crate::config::ConfigStore;
use crate::error::{CliError, Result};

const KEYRING_SERVICE: &str = "helen-cli";
const KEYRING_USER: &str = "refresh_token";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AccessTokenResponse {
    pub access_token: String,
    pub token_type: String,
    pub expires_in: u64,
    #[serde(default)]
    pub refresh_token: Option<String>,
}

#[derive(Debug, Clone)]
struct AccessCache {
    token: String,
    expires_at: Instant,
}

#[derive(Clone)]
pub struct AuthManager {
    cfg: ConfigStore,
    http: Client,
    cache: Arc<RwLock<Option<AccessCache>>>,
    refresh_lock: Arc<Mutex<()>>,
}

impl AuthManager {
    pub fn new(cfg: ConfigStore, http: Client) -> Self {
        Self {
            cfg,
            http,
            cache: Arc::new(RwLock::new(None)),
            refresh_lock: Arc::new(Mutex::new(())),
        }
    }

    pub fn keyring_entry() -> Result<keyring::Entry> {
        Ok(keyring::Entry::new(KEYRING_SERVICE, KEYRING_USER)?)
    }

    pub fn store_refresh_token(rt: &str) -> Result<()> {
        let e = Self::keyring_entry()?;
        e.set_password(rt)?;
        Ok(())
    }

    pub fn load_refresh_token() -> Result<Option<String>> {
        let e = Self::keyring_entry()?;
        match e.get_password() {
            Ok(v) => Ok(Some(v)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    pub fn clear_refresh_token() -> Result<()> {
        let e = Self::keyring_entry()?;
        match e.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(e) => Err(e.into()),
        }
    }

    pub async fn login_password(
        &self, username: &str, password: &str,
    ) -> Result<AccessTokenResponse> {
        let url = format!("{}/api/auth/login", self.cfg.server_url()?.trim_end_matches('/'));
        #[derive(Serialize)]
        struct Body<'a> { username: &'a str, password: &'a str }
        let resp = self.http.post(&url)
            .json(&Body { username, password })
            .send().await?;
        if !resp.status().is_success() {
            let s = resp.status().as_u16();
            let body = resp.text().await.unwrap_or_default();
            return Err(CliError::Http { status: s, body });
        }
        let tok: AccessTokenResponse = resp.json().await?;
        if let Some(rt) = &tok.refresh_token {
            Self::store_refresh_token(rt)?;
        }
        self.cache.write().replace(AccessCache {
            token: tok.access_token.clone(),
            expires_at: Instant::now() + Duration::from_secs(tok.expires_in.max(60)),
        });
        info!("logged in as {username}");
        Ok(tok)
    }

    /// Returns a usable access token, refreshing if necessary.
    pub async fn access_token(&self) -> Result<String> {
        if let Some(entry) = self.cache.read().clone() {
            if entry.expires_at > Instant::now() + Duration::from_secs(30) {
                return Ok(entry.token);
            }
        }
        let _g = self.refresh_lock.lock().await;
        if let Some(entry) = self.cache.read().clone() {
            if entry.expires_at > Instant::now() + Duration::from_secs(30) {
                return Ok(entry.token);
            }
        }
        self.refresh_inner().await
    }

    async fn refresh_inner(&self) -> Result<String> {
        let rt = Self::load_refresh_token()?
            .ok_or(CliError::NotLoggedIn)?;
        let url = format!("{}/api/auth/refresh", self.cfg.server_url()?.trim_end_matches('/'));
        debug!(%url, "refreshing access token");

        #[derive(Serialize)]
        struct Body<'a> { refresh_token: &'a str }
        let resp = self.http.post(&url).json(&Body { refresh_token: &rt })
            .send().await?;
        if !resp.status().is_success() {
            let s = resp.status();
            let t = resp.text().await.unwrap_or_default();
            warn!(status=?s, body=%t, "refresh failed");
            return Err(CliError::Auth(format!("refresh failed: {s} — {t}")));
        }
        let parsed: AccessTokenResponse = resp.json().await?;
        if let Some(new_rt) = parsed.refresh_token.clone() {
            Self::store_refresh_token(&new_rt)?;
        }
        let expires_at = Instant::now() + Duration::from_secs(parsed.expires_in.max(60));
        self.cache.write().replace(AccessCache {
            token: parsed.access_token.clone(), expires_at,
        });
        info!(expires_in=parsed.expires_in, "access token refreshed");
        Ok(parsed.access_token)
    }

    pub async fn logout(&self) -> Result<()> {
        Self::clear_refresh_token()?;
        self.cache.write().take();
        Ok(())
    }
}
