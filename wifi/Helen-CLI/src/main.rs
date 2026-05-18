//! helen-cli — headless Helen client.
//!
//! ```text
//! helen-cli login --server https://helen.lan
//! helen-cli channels
//! helen-cli send --channel general "hello world"
//! helen-cli upload --channel general ./report.pdf
//! helen-cli call --channel general --audio
//! helen-cli agent                        # interactive REPL
//! ```

mod api;
mod auth;
mod audio;
mod commands;
mod config;
mod error;
mod logging;
mod socket;
mod util;

use std::path::PathBuf;

use clap::{Parser, Subcommand};
use colored::*;
use reqwest::Client;

use crate::api::ApiClient;
use crate::config::ConfigStore;
use crate::error::Result;
use crate::socket::SocketClient;

#[derive(Parser, Debug)]
#[command(
    name = "helen-cli",
    version = env!("HELEN_CLI_VERSION"),
    about = "Helen CLI — headless client for the Helen/CommClient platform.",
    long_about = None,
)]
struct Cli {
    /// Increase log verbosity (-v, -vv).
    #[arg(short, long, action = clap::ArgAction::Count, global = true)]
    verbose: u8,

    /// Override config file path.
    #[arg(long, global = true)]
    config: Option<PathBuf>,

    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand, Debug)]
enum Cmd {
    /// Authenticate against a Helen server.
    Login {
        #[arg(long)] server: Option<String>,
        #[arg(long)] username: Option<String>,
        #[arg(long)] password: Option<String>,
    },
    /// Forget cached credentials.
    Logout,
    /// Print current identity.
    Whoami,

    /// List channels you can see.
    Channels,

    /// List messages in a channel.
    Messages {
        #[arg(long)] channel: String,
        #[arg(long, default_value_t = 30)] limit: usize,
        /// Live tail (subscribe to socket.io message:new).
        #[arg(long, default_value_t = false)] watch: bool,
    },

    /// Send a message.
    Send {
        #[arg(long)] channel: String,
        body: String,
    },

    /// Edit a message.
    Edit { message_id: String, body: String },

    /// Delete a message.
    Delete { message_id: String },

    /// Upload a file to a channel.
    Upload {
        #[arg(long)] channel: String,
        path: PathBuf,
    },

    /// Download a file by id.
    Download {
        file_id: String,
        #[arg(long)] out: PathBuf,
        #[arg(long, default_value_t = true)] resume: bool,
    },

    /// Mobile-style pairing (Module O).
    Pair {
        /// Pairing code to redeem; omit to request a new one.
        #[arg(long)] code: Option<String>,
    },

    /// Start an audio call in a channel.
    Call {
        #[arg(long)] channel: String,
        #[arg(long, default_value_t = true)] audio: bool,
        #[arg(long)] seconds: Option<u64>,
    },

    /// Open an interactive REPL.
    Agent,

    /// Show or edit configuration.
    Config {
        /// Set a key=value.
        #[arg(long)] set: Option<String>,
    },
}

#[tokio::main]
async fn main() {
    if let Err(e) = real_main().await {
        eprintln!("{} {e}", "error:".red().bold());
        std::process::exit(1);
    }
}

async fn real_main() -> Result<()> {
    let cli = Cli::parse();
    logging::init(cli.verbose);

    let cfg = ConfigStore::load_or_init()?;
    let http = Client::builder()
        .user_agent(format!("helen-cli/{}", env!("HELEN_CLI_VERSION")))
        .timeout(std::time::Duration::from_secs(60))
        .build()?;
    let api = ApiClient::new(cfg.clone(), http.clone());
    let sock = SocketClient::new(cfg.clone(), api.auth().clone());

    match cli.cmd {
        Cmd::Login { server, username, password } =>
            commands::login::login(cfg.clone(), api, server, username, password).await?,
        Cmd::Logout => commands::login::logout(api).await?,
        Cmd::Whoami => commands::login::whoami(cfg, api).await?,
        Cmd::Channels => commands::channels::list(api).await?,
        Cmd::Messages { channel, limit, watch } => {
            if watch {
                commands::messages::watch(api, sock, channel).await?;
            } else {
                commands::messages::list(api, &channel, limit).await?;
            }
        }
        Cmd::Send { channel, body } =>
            commands::messages::send(api, &channel, &body).await?,
        Cmd::Edit { message_id, body } =>
            commands::messages::edit(api, &message_id, &body).await?,
        Cmd::Delete { message_id } =>
            commands::messages::delete(api, &message_id).await?,
        Cmd::Upload { channel, path } =>
            commands::files::upload(api, &channel, path).await?,
        Cmd::Download { file_id, out, resume } =>
            commands::files::download(api, &file_id, out, resume).await?,
        Cmd::Pair { code } => {
            match code {
                Some(c) => commands::pair::redeem(api, &c).await?,
                None => commands::pair::request_code(api).await?,
            }
        }
        Cmd::Call { channel, audio: _, seconds } =>
            commands::call::call(api, sock, channel, seconds).await?,
        Cmd::Agent => commands::agent::run(cfg, api, sock).await?,
        Cmd::Config { set } => {
            if let Some(kv) = set {
                let (k, v) = kv.split_once('=')
                    .ok_or_else(|| error::CliError::InvalidArg("expected key=value".into()))?;
                let k = k.trim(); let v = v.trim().to_string();
                cfg.update(|c| match k {
                    "server_url" => c.server_url = Some(v.clone()),
                    "default_channel" => c.default_channel = Some(v.clone()),
                    "audio_input" => c.audio_input = Some(v.clone()),
                    "audio_output" => c.audio_output = Some(v.clone()),
                    _ => {}
                })?;
                println!("set {k} = {v}");
            } else {
                println!("{}", toml::to_string_pretty(&cfg.snapshot()).unwrap_or_default());
            }
        }
    }
    Ok(())
}
