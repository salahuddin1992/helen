//! Helen-Agent-Windows — entry point.
//!
//! Sub-commands:
//!   install     — register as Windows Service (auto-start, LocalSystem)
//!   uninstall   — remove the service
//!   run         — foreground execution (also used by the service host)
//!   register    — first-time pairing with Helen-Server
//!   status      — print local config + connectivity
//!   update      — fetch latest binary from the server

use std::sync::atomic::AtomicBool;
use std::sync::Arc;

use clap::{Parser, Subcommand};
use tracing::{error, info};

mod auth;
mod commands;
mod config;
mod error;
mod files;
mod fingerprint;
mod heartbeat;
mod logging;
mod registration;
mod screen;
mod service;
mod sysinfo;
mod updater;
mod ws_channel;

use crate::error::{AgentError, Result};

#[derive(Debug, Parser)]
#[command(name = "helen-agent", version = env!("CARGO_PKG_VERSION"), about = "Helen Device Agent")]
struct Cli {
    /// Override config path (defaults to %ProgramData%/Helen-Agent/config.toml).
    #[arg(long, env = "HELEN_AGENT_CONFIG", global = true)]
    config: Option<String>,

    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Debug, Subcommand)]
enum Cmd {
    /// Install as a Windows Service.
    Install,
    /// Uninstall the Windows Service.
    Uninstall,
    /// Run the agent in the foreground (used by the service host too).
    Run {
        /// Indicates the process was launched by the SCM.
        #[arg(long)]
        service: bool,
    },
    /// First-time pairing with the Helen-Server.
    Register {
        /// Base URL of the server (e.g. https://helen.example.com).
        #[arg(long)]
        server: Option<String>,
    },
    /// Print local agent status.
    Status,
    /// Run a single update cycle.
    Update,
    /// Print the device fingerprint.
    Fingerprint,
}

fn main() {
    if let Err(e) = real_main() {
        eprintln!("FATAL: {e}");
        std::process::exit(1);
    }
}

fn real_main() -> Result<()> {
    let cli = Cli::parse();

    // Service-mode dispatcher must hand control back to the SCM before any
    // tokio runtime starts.
    if let Cmd::Run { service: true } = cli.cmd {
        let _guard = logging::init(&logging::default_log_dir(), "info", true);
        return service::run_as_service();
    }

    let cfg = config::ConfigStore::load_or_create()?;
    let snap = cfg.snapshot();
    let _guard = logging::init(&logging::default_log_dir(), &snap.log_level, false)?;

    match cli.cmd {
        Cmd::Install => service::install(),
        Cmd::Uninstall => service::uninstall(),
        Cmd::Run { service: false } => {
            let rt = tokio::runtime::Runtime::new()
                .map_err(|e| AgentError::Internal(format!("runtime: {e}")))?;
            rt.block_on(async {
                let stop = Arc::new(AtomicBool::new(false));
                let stop_clone = stop.clone();
                tokio::spawn(async move {
                    if tokio::signal::ctrl_c().await.is_ok() {
                        stop_clone.store(true, std::sync::atomic::Ordering::SeqCst);
                    }
                });
                service::run_agent_loop(cfg, stop).await
            })
        }
        Cmd::Run { service: true } => unreachable!(),
        Cmd::Register { server } => {
            let rt = tokio::runtime::Runtime::new()
                .map_err(|e| AgentError::Internal(format!("runtime: {e}")))?;
            rt.block_on(async {
                match registration::register(&cfg, server).await {
                    Ok(resp) => {
                        info!(target: "main", "registration ok: agent_id={}", resp.agent_id);
                        println!("{{\"agent_id\":\"{}\",\"status\":\"registered\"}}", resp.agent_id);
                        Ok::<(), AgentError>(())
                    }
                    Err(e) => {
                        error!(target: "main", error = %e, "registration failed");
                        Err(e)
                    }
                }
            })
        }
        Cmd::Status => {
            let snap = cfg.snapshot();
            let fp = fingerprint::current_or_compute().unwrap_or_default();
            println!(
                "{{\"server_url\":\"{}\",\"agent_id\":\"{}\",\"fingerprint\":\"{}\",\"version\":\"{}\",\"registered\":{}}}",
                snap.server_url,
                snap.agent_id.as_deref().unwrap_or(""),
                fp,
                env!("CARGO_PKG_VERSION"),
                snap.agent_id.is_some(),
            );
            Ok(())
        }
        Cmd::Update => {
            let rt = tokio::runtime::Runtime::new()
                .map_err(|e| AgentError::Internal(format!("runtime: {e}")))?;
            rt.block_on(async {
                let http = reqwest::Client::builder()
                    .timeout(std::time::Duration::from_secs(snap.http_timeout_secs))
                    .danger_accept_invalid_certs(!snap.verify_tls)
                    .build()
                    .map_err(AgentError::Http)?;
                let auth = auth::AuthManager::new(cfg.clone(), http.clone());
                let upd = updater::Updater::new(cfg.clone(), auth, http);
                upd.run().await
            })
        }
        Cmd::Fingerprint => {
            let fp = fingerprint::current_or_compute()?;
            println!("{fp}");
            Ok(())
        }
    }
}
