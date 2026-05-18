//! File transfer — sandboxed upload/download with SHA-256 verification.

use std::path::{Path, PathBuf};

use reqwest::Client;
use sha2::{Digest, Sha256};
use tokio::fs;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tracing::{debug, info, warn};

use crate::auth::AuthManager;
use crate::config::{AgentConfig, ConfigStore};
use crate::error::{AgentError, Result};

#[derive(Clone)]
pub struct FileService {
    http: Client,
    auth: AuthManager,
    cfg: AgentConfig,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct UploadReceipt {
    pub file_id: String,
    pub sha256: String,
    pub bytes: u64,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct DownloadMeta {
    pub token: String,
    pub destination: String,
    pub expected_sha256: Option<String>,
}

impl FileService {
    pub fn new(http: Client, auth: AuthManager, cfg: AgentConfig) -> Self {
        Self { http, auth, cfg }
    }

    pub async fn upload(&self, source: &Path) -> Result<UploadReceipt> {
        // Reading any file the service can access is allowed (we run as
        // LocalSystem). The sandbox restriction applies only to writes.
        let abs = fs::canonicalize(source).await?;
        info!(target: "files", path = %abs.display(), "uploading");

        let mut f = fs::File::open(&abs).await?;
        let mut buf = Vec::with_capacity(8192);
        f.read_to_end(&mut buf).await?;
        let bytes = buf.len() as u64;

        let mut hasher = Sha256::new();
        hasher.update(&buf);
        let sha = hex::encode(hasher.finalize());

        let agent_id = self.agent_id()?;
        let url = format!(
            "{}/api/agents/{}/files/upload",
            self.cfg.server_url.trim_end_matches('/'),
            agent_id
        );
        let token = self.auth.access_token().await?;
        let filename = abs
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_else(|| "blob.bin".into());
        let form = reqwest::multipart::Form::new()
            .text("sha256", sha.clone())
            .text("size", bytes.to_string())
            .part(
                "file",
                reqwest::multipart::Part::bytes(buf)
                    .file_name(filename.clone())
                    .mime_str("application/octet-stream")
                    .map_err(AgentError::from)?,
            );
        let resp = self
            .http
            .post(&url)
            .bearer_auth(token)
            .multipart(form)
            .send()
            .await?;
        if !resp.status().is_success() {
            return Err(AgentError::FileTransfer(format!(
                "upload http {}",
                resp.status()
            )));
        }
        let receipt: UploadReceipt = resp.json().await?;
        if receipt.sha256 != sha {
            return Err(AgentError::IntegrityMismatch {
                expected: sha,
                actual: receipt.sha256,
            });
        }
        Ok(receipt)
    }

    pub async fn download(&self, meta: DownloadMeta) -> Result<PathBuf> {
        let dest = sandbox_resolve(&meta.destination)?;
        if let Some(parent) = dest.parent() {
            fs::create_dir_all(parent).await?;
        }
        let agent_id = self.agent_id()?;
        let url = format!(
            "{}/api/agents/{}/files/download/{}",
            self.cfg.server_url.trim_end_matches('/'),
            agent_id,
            meta.token
        );
        debug!(target: "files", url = %url, dest = %dest.display(), "downloading");
        let token = self.auth.access_token().await?;

        let resume_offset = if dest.exists() {
            fs::metadata(&dest).await?.len()
        } else {
            0
        };
        let mut req = self.http.get(&url).bearer_auth(token);
        if resume_offset > 0 {
            req = req.header("Range", format!("bytes={}-", resume_offset));
        }
        let resp = req.send().await?;
        if !resp.status().is_success() && resp.status().as_u16() != 206 {
            return Err(AgentError::FileTransfer(format!(
                "download http {}",
                resp.status()
            )));
        }

        let mut hasher = Sha256::new();
        if resume_offset > 0 {
            // Hash existing bytes so the running digest matches the final file.
            let existing = fs::read(&dest).await?;
            hasher.update(&existing);
        }
        let mut out = fs::OpenOptions::new()
            .create(true)
            .append(resume_offset > 0)
            .write(true)
            .truncate(resume_offset == 0)
            .open(&dest)
            .await?;
        let mut stream = resp.bytes_stream();
        use futures_util::StreamExt;
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(AgentError::from)?;
            hasher.update(&chunk);
            out.write_all(&chunk).await?;
        }
        out.flush().await?;
        drop(out);
        let actual = hex::encode(hasher.finalize());
        if let Some(expected) = &meta.expected_sha256 {
            if expected != &actual {
                warn!(target: "files", expected = %expected, actual = %actual, "checksum mismatch");
                return Err(AgentError::IntegrityMismatch {
                    expected: expected.clone(),
                    actual,
                });
            }
        }
        info!(target: "files", path = %dest.display(), "download complete");
        Ok(dest)
    }

    fn agent_id(&self) -> Result<String> {
        self.cfg
            .agent_id
            .clone()
            .ok_or_else(|| AgentError::Config("agent_id missing — register first".into()))
    }
}

/// Resolve a user-supplied destination strictly inside the sandbox dir.
/// Rejects any path that escapes via `..`, absolute roots, or symlinks.
pub fn sandbox_resolve(rel: &str) -> Result<PathBuf> {
    let root = ConfigStore::sandbox_dir();
    std::fs::create_dir_all(&root).ok();
    let candidate = root.join(rel);
    // Canonicalise root, then check candidate begins with it.
    let real_root = std::fs::canonicalize(&root).unwrap_or(root.clone());
    let parent = candidate
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or(candidate.clone());
    let canonical_parent = if parent.exists() {
        std::fs::canonicalize(&parent).unwrap_or(parent.clone())
    } else {
        parent.clone()
    };
    if !canonical_parent.starts_with(&real_root) {
        return Err(AgentError::PathDenied(format!(
            "destination escapes sandbox: {}",
            candidate.display()
        )));
    }
    Ok(candidate)
}
