//! Token management — refresh→access exchange with auto-renewal and DPAPI
//! protection of persisted secrets.

use std::sync::Arc;
use std::time::{Duration, Instant};

use parking_lot::RwLock;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use tokio::sync::Mutex;
use tracing::{debug, info, warn};

use crate::config::ConfigStore;
use crate::error::{AgentError, Result};

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

    /// Returns a usable access token, refreshing if necessary.
    pub async fn access_token(&self) -> Result<String> {
        if let Some(entry) = self.cache.read().clone() {
            // 30 s safety margin
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
        let snap = self.cfg.snapshot();
        let refresh = snap
            .refresh_token
            .clone()
            .ok_or_else(|| AgentError::Auth("no refresh token — register first".into()))?;
        let url = format!("{}/api/agents/auth/refresh", snap.server_url.trim_end_matches('/'));
        debug!(target: "auth", url = %url, "refreshing access token");

        #[derive(Serialize)]
        struct Body<'a> {
            refresh_token: &'a str,
            agent_id: Option<&'a str>,
        }
        let body = Body {
            refresh_token: &refresh,
            agent_id: snap.agent_id.as_deref(),
        };
        let resp = self
            .http
            .post(&url)
            .json(&body)
            .send()
            .await
            .map_err(AgentError::from)?;
        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().await.unwrap_or_default();
            warn!(target: "auth", status = ?status, body = %text, "refresh failed");
            return Err(AgentError::Auth(format!("refresh failed: {status} — {text}")));
        }
        let parsed: AccessTokenResponse = resp.json().await.map_err(AgentError::from)?;
        // Rotate refresh if server issued a new one.
        if let Some(new_rt) = parsed.refresh_token.clone() {
            self.cfg.update(|c| c.refresh_token = Some(new_rt))?;
        }
        let expires_at = Instant::now() + Duration::from_secs(parsed.expires_in.max(60));
        {
            let mut w = self.cache.write();
            *w = Some(AccessCache {
                token: parsed.access_token.clone(),
                expires_at,
            });
        }
        info!(target: "auth", expires_in = parsed.expires_in, "access token refreshed");
        Ok(parsed.access_token)
    }

    pub fn store_refresh(&self, refresh: &str) -> Result<()> {
        self.cfg.update(|c| c.refresh_token = Some(refresh.to_string()))?;
        Ok(())
    }

    pub fn store_agent_id(&self, id: &str) -> Result<()> {
        self.cfg.update(|c| c.agent_id = Some(id.to_string()))?;
        Ok(())
    }
}

// ── DPAPI helpers ────────────────────────────────────────────────

#[cfg(target_os = "windows")]
pub fn dpapi_protect(plaintext: &[u8]) -> Result<Vec<u8>> {
    use windows::Win32::Foundation::LocalFree;
    use windows::Win32::Security::Cryptography::{
        CryptProtectData, CRYPT_INTEGER_BLOB, CRYPTPROTECT_LOCAL_MACHINE,
    };
    use windows::Win32::Foundation::HLOCAL;

    let mut in_blob = CRYPT_INTEGER_BLOB {
        cbData: plaintext.len() as u32,
        pbData: plaintext.as_ptr() as *mut u8,
    };
    let mut out_blob = CRYPT_INTEGER_BLOB::default();

    unsafe {
        CryptProtectData(
            &mut in_blob,
            None,
            None,
            None,
            None,
            CRYPTPROTECT_LOCAL_MACHINE,
            &mut out_blob,
        )
        .map_err(|e| AgentError::Auth(format!("DPAPI protect failed: {e}")))?;
        let slice = std::slice::from_raw_parts(out_blob.pbData, out_blob.cbData as usize);
        let v = slice.to_vec();
        let _ = LocalFree(HLOCAL(out_blob.pbData as *mut _));
        Ok(v)
    }
}

#[cfg(target_os = "windows")]
pub fn dpapi_unprotect(ciphertext: &[u8]) -> Result<Vec<u8>> {
    use windows::Win32::Foundation::LocalFree;
    use windows::Win32::Security::Cryptography::{
        CryptUnprotectData, CRYPT_INTEGER_BLOB, CRYPTPROTECT_LOCAL_MACHINE,
    };
    use windows::Win32::Foundation::HLOCAL;

    let mut in_blob = CRYPT_INTEGER_BLOB {
        cbData: ciphertext.len() as u32,
        pbData: ciphertext.as_ptr() as *mut u8,
    };
    let mut out_blob = CRYPT_INTEGER_BLOB::default();

    unsafe {
        CryptUnprotectData(
            &mut in_blob,
            None,
            None,
            None,
            None,
            CRYPTPROTECT_LOCAL_MACHINE,
            &mut out_blob,
        )
        .map_err(|e| AgentError::Auth(format!("DPAPI unprotect failed: {e}")))?;
        let slice = std::slice::from_raw_parts(out_blob.pbData, out_blob.cbData as usize);
        let v = slice.to_vec();
        let _ = LocalFree(HLOCAL(out_blob.pbData as *mut _));
        Ok(v)
    }
}

#[cfg(not(target_os = "windows"))]
pub fn dpapi_protect(plaintext: &[u8]) -> Result<Vec<u8>> { Ok(plaintext.to_vec()) }

#[cfg(not(target_os = "windows"))]
pub fn dpapi_unprotect(ciphertext: &[u8]) -> Result<Vec<u8>> { Ok(ciphertext.to_vec()) }
