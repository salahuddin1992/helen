//! Command execution with strict whitelisting, timeout, and structured output.

use std::process::Stdio;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;
use tokio::sync::mpsc;
use tokio::time::timeout;
use tracing::{debug, warn};

use crate::config::AgentConfig;
use crate::error::{AgentError, Result};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CommandRequest {
    pub command_id: String,
    pub command: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub timeout_secs: Option<u64>,
    #[serde(default)]
    pub cwd: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CommandResult {
    pub command_id: String,
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
    pub duration_ms: u128,
    pub timed_out: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum CommandStreamEvent {
    Stdout { command_id: String, line: String },
    Stderr { command_id: String, line: String },
    Finished(CommandResult),
}

/// Verify command against the configured whitelist. Returns the executable
/// resolved (with `.exe` appended if needed for shell builtins).
pub fn check_whitelist(cfg: &AgentConfig, cmd: &str) -> Result<()> {
    let normalized = std::path::Path::new(cmd)
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or(cmd)
        .to_lowercase();
    if cfg
        .command_whitelist
        .iter()
        .any(|c| c.to_lowercase() == normalized)
    {
        Ok(())
    } else {
        Err(AgentError::CommandRejected(format!(
            "command '{cmd}' is not whitelisted"
        )))
    }
}

/// Run a command with bounded timeout. Streams stdout/stderr line-by-line into
/// the provided channel, then sends the final `Finished` event.
pub async fn execute(
    cfg: &AgentConfig,
    req: CommandRequest,
    tx: mpsc::Sender<CommandStreamEvent>,
) -> Result<CommandResult> {
    check_whitelist(cfg, &req.command)?;

    let limit = req
        .timeout_secs
        .unwrap_or(cfg.command_timeout_secs)
        .min(300);
    let started = std::time::Instant::now();

    let mut cmd = Command::new(&req.command);
    cmd.args(&req.args);
    if let Some(cwd) = req.cwd.as_ref() {
        cmd.current_dir(cwd);
    }
    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);

    debug!(target: "commands", command = %req.command, args = ?req.args, "spawning");
    let mut child = cmd
        .spawn()
        .map_err(|e| AgentError::CommandRejected(format!("spawn failed: {e}")))?;

    let stdout = child.stdout.take().expect("stdout piped");
    let stderr = child.stderr.take().expect("stderr piped");

    let id_clone = req.command_id.clone();
    let tx_out = tx.clone();
    let out_task = tokio::spawn(async move {
        let mut acc = String::new();
        let mut lines = BufReader::new(stdout).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            acc.push_str(&line);
            acc.push('\n');
            let _ = tx_out
                .send(CommandStreamEvent::Stdout {
                    command_id: id_clone.clone(),
                    line,
                })
                .await;
        }
        acc
    });

    let id_clone = req.command_id.clone();
    let tx_err = tx.clone();
    let err_task = tokio::spawn(async move {
        let mut acc = String::new();
        let mut lines = BufReader::new(stderr).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            acc.push_str(&line);
            acc.push('\n');
            let _ = tx_err
                .send(CommandStreamEvent::Stderr {
                    command_id: id_clone.clone(),
                    line,
                })
                .await;
        }
        acc
    });

    let mut timed_out = false;
    let exit = match timeout(Duration::from_secs(limit), child.wait()).await {
        Ok(Ok(status)) => status.code().unwrap_or(-1),
        Ok(Err(e)) => {
            warn!(target: "commands", error = %e, "wait failed");
            -1
        }
        Err(_) => {
            warn!(target: "commands", id = %req.command_id, "command timed out");
            let _ = child.kill().await;
            timed_out = true;
            124
        }
    };
    let stdout_acc = out_task.await.unwrap_or_default();
    let stderr_acc = err_task.await.unwrap_or_default();

    let result = CommandResult {
        command_id: req.command_id.clone(),
        exit_code: exit,
        stdout: stdout_acc,
        stderr: stderr_acc,
        duration_ms: started.elapsed().as_millis(),
        timed_out,
    };
    let _ = tx.send(CommandStreamEvent::Finished(result.clone())).await;
    Ok(result)
}
