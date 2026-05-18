//! `helen-cli agent` — interactive REPL powered by rustyline.
use rustyline::completion::{Completer, Pair};
use rustyline::error::ReadlineError;
use rustyline::highlight::Highlighter;
use rustyline::hint::Hinter;
use rustyline::validate::Validator;
use rustyline::{Context, Editor, Helper};
use rustyline::history::DefaultHistory;
use std::borrow::Cow;

use crate::api::ApiClient;
use crate::config::ConfigStore;
use crate::error::Result;
use crate::socket::SocketClient;

const COMMANDS: &[&str] = &[
    "help", "quit", "exit", "whoami", "channels", "send", "list",
    "watch", "upload", "download", "pair", "call", "config",
];

struct ReplHelper;

impl Helper for ReplHelper {}
impl Hinter for ReplHelper { type Hint = String; }
impl Validator for ReplHelper {}
impl Highlighter for ReplHelper {
    fn highlight_prompt<'b, 's: 'b, 'p: 'b>(
        &'s self, prompt: &'p str, _default: bool,
    ) -> Cow<'b, str> {
        Cow::Owned(format!("\x1b[1;36m{prompt}\x1b[0m"))
    }
}
impl Completer for ReplHelper {
    type Candidate = Pair;
    fn complete(&self, line: &str, pos: usize, _ctx: &Context<'_>)
        -> rustyline::Result<(usize, Vec<Pair>)>
    {
        let head = &line[..pos];
        let token_start = head.rfind(char::is_whitespace).map(|i| i + 1).unwrap_or(0);
        let stub = &head[token_start..];
        let mut out = Vec::new();
        for c in COMMANDS {
            if c.starts_with(stub) {
                out.push(Pair { display: (*c).into(), replacement: (*c).into() });
            }
        }
        Ok((token_start, out))
    }
}

pub async fn run(cfg: ConfigStore, api: ApiClient, sock: SocketClient) -> Result<()> {
    let mut rl: Editor<ReplHelper, DefaultHistory> = Editor::new()
        .map_err(|e| crate::error::CliError::Other(e.to_string()))?;
    rl.set_helper(Some(ReplHelper));
    println!("Helen interactive agent. Type `help` for commands, `quit` to exit.");

    loop {
        match rl.readline("helen> ") {
            Ok(line) => {
                let line = line.trim().to_string();
                if line.is_empty() { continue; }
                let _ = rl.add_history_entry(line.as_str());
                if line == "quit" || line == "exit" { break; }
                if let Err(e) = dispatch(&cfg, &api, &sock, &line).await {
                    eprintln!("err: {e}");
                }
            }
            Err(ReadlineError::Interrupted | ReadlineError::Eof) => break,
            Err(e) => { eprintln!("readline: {e}"); break; }
        }
    }
    Ok(())
}

async fn dispatch(
    cfg: &ConfigStore, api: &ApiClient, sock: &SocketClient, line: &str,
) -> Result<()> {
    let parts: Vec<&str> = line.splitn(3, char::is_whitespace).collect();
    let cmd = parts.first().copied().unwrap_or("");
    match cmd {
        "help" => {
            println!("commands:");
            for c in COMMANDS { println!("  {c}"); }
        }
        "whoami" => super::login::whoami(cfg.clone(), api.clone()).await?,
        "channels" => super::channels::list(api.clone()).await?,
        "list" => {
            let ch = parts.get(1).copied().unwrap_or_default();
            super::messages::list(api.clone(), ch, 30).await?;
        }
        "send" => {
            let ch = parts.get(1).copied().unwrap_or("");
            let body = parts.get(2).copied().unwrap_or("");
            super::messages::send(api.clone(), ch, body).await?;
        }
        "watch" => {
            let ch = parts.get(1).copied().unwrap_or("").to_string();
            super::messages::watch(api.clone(), sock.clone(), ch).await?;
        }
        "config" => {
            let snap = cfg.snapshot();
            println!("{}", toml::to_string_pretty(&snap).unwrap_or_default());
        }
        _ => println!("unknown command: {cmd}"),
    }
    Ok(())
}
