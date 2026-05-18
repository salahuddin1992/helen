//! REST API client wrapper — JSON + multipart + range download.
use std::path::Path;

use reqwest::{multipart, Body, Client, Method, RequestBuilder};
use serde::de::DeserializeOwned;
use serde::Serialize;
use tokio::fs::File;
use tokio_util::io::ReaderStream;
use tracing::debug;

use crate::auth::AuthManager;
use crate::config::ConfigStore;
use crate::error::{CliError, Result};

#[derive(Clone)]
pub struct ApiClient {
    cfg: ConfigStore,
    auth: AuthManager,
    http: Client,
}

impl ApiClient {
    pub fn new(cfg: ConfigStore, http: Client) -> Self {
        let auth = AuthManager::new(cfg.clone(), http.clone());
        Self { cfg, auth, http }
    }

    pub fn auth(&self) -> &AuthManager { &self.auth }

    fn url(&self, path: &str) -> Result<String> {
        Ok(format!("{}{}", self.cfg.server_url()?.trim_end_matches('/'), path))
    }

    async fn authed(&self, method: Method, path: &str) -> Result<RequestBuilder> {
        let tok = self.auth.access_token().await?;
        Ok(self.http.request(method, self.url(path)?)
            .bearer_auth(tok))
    }

    pub async fn get_json<R: DeserializeOwned>(&self, path: &str) -> Result<R> {
        debug!(%path, "GET");
        let resp = self.authed(Method::GET, path).await?.send().await?;
        Self::parse_json(resp).await
    }

    pub async fn post_json<B: Serialize, R: DeserializeOwned>(
        &self, path: &str, body: &B,
    ) -> Result<R> {
        debug!(%path, "POST");
        let resp = self.authed(Method::POST, path).await?
            .json(body).send().await?;
        Self::parse_json(resp).await
    }

    pub async fn delete(&self, path: &str) -> Result<()> {
        let resp = self.authed(Method::DELETE, path).await?.send().await?;
        if resp.status().is_success() { Ok(()) } else {
            Err(Self::http_err(resp).await)
        }
    }

    async fn parse_json<R: DeserializeOwned>(resp: reqwest::Response) -> Result<R> {
        if !resp.status().is_success() {
            return Err(Self::http_err(resp).await);
        }
        Ok(resp.json::<R>().await?)
    }

    async fn http_err(resp: reqwest::Response) -> CliError {
        let s = resp.status().as_u16();
        let body = resp.text().await.unwrap_or_default();
        CliError::Http { status: s, body }
    }

    // ── multipart upload ────────────────────────────

    pub async fn upload_file(
        &self, channel_id: &str, path: &Path,
    ) -> Result<serde_json::Value> {
        let file = File::open(path).await?;
        let meta = file.metadata().await?;
        let name = path.file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("upload.bin")
            .to_string();
        let stream = ReaderStream::new(file);
        let body = Body::wrap_stream(stream);
        let part = multipart::Part::stream_with_length(body, meta.len())
            .file_name(name.clone())
            .mime_str("application/octet-stream")
            .map_err(|e| CliError::Other(format!("mime: {e}")))?;
        let form = multipart::Form::new()
            .text("channel_id", channel_id.to_string())
            .part("file", part);

        let resp = self.authed(Method::POST, "/api/files/upload").await?
            .multipart(form).send().await?;
        Self::parse_json(resp).await
    }

    // ── ranged download (resumable) ─────────────────

    pub async fn download_to(
        &self, file_id: &str, dest: &Path, resume: bool,
    ) -> Result<u64> {
        let mut offset: u64 = 0;
        if resume {
            if let Ok(m) = tokio::fs::metadata(dest).await {
                offset = m.len();
            }
        }
        let mut req = self.authed(Method::GET, &format!("/api/files/{file_id}")).await?;
        if offset > 0 {
            req = req.header("Range", format!("bytes={offset}-"));
        }
        let resp = req.send().await?;
        if !resp.status().is_success() && resp.status().as_u16() != 206 {
            return Err(Self::http_err(resp).await);
        }
        let mut total = offset;
        let mut file = if offset > 0 {
            tokio::fs::OpenOptions::new().append(true).open(dest).await?
        } else {
            tokio::fs::File::create(dest).await?
        };
        let mut stream = resp.bytes_stream();
        use futures_util::StreamExt;
        use tokio::io::AsyncWriteExt;
        while let Some(chunk) = stream.next().await {
            let bytes = chunk?;
            file.write_all(&bytes).await?;
            total += bytes.len() as u64;
        }
        file.flush().await?;
        Ok(total)
    }
}
