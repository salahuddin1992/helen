//! `helen-cli login` / `logout` / `whoami` implementations.
use colored::*;

use crate::api::ApiClient;
use crate::auth::AuthManager;
use crate::config::ConfigStore;
use crate::error::Result;

pub async fn login(
    cfg: ConfigStore, api: ApiClient,
    server: Option<String>, username: Option<String>, password: Option<String>,
) -> Result<()> {
    if let Some(s) = server {
        cfg.update(|c| c.server_url = Some(s))?;
    }
    let username = match username {
        Some(u) => u,
        None => rpassword::prompt_password("username: ").unwrap_or_default(),
    };
    let password = match password {
        Some(p) => p,
        None => rpassword::prompt_password("password: ").unwrap_or_default(),
    };
    let resp = api.auth().login_password(&username, &password).await?;
    cfg.update(|c| c.last_user = Some(username.clone()))?;
    println!("{} logged in (expires in {}s)", "✓".green(), resp.expires_in);
    Ok(())
}

pub async fn logout(api: ApiClient) -> Result<()> {
    api.auth().logout().await?;
    AuthManager::clear_refresh_token()?;
    println!("{} logged out", "✓".green());
    Ok(())
}

pub async fn whoami(cfg: ConfigStore, api: ApiClient) -> Result<()> {
    let me: serde_json::Value = api.get_json("/api/users/me").await?;
    let user = me.get("username")
        .and_then(|v| v.as_str())
        .unwrap_or("?")
        .to_string();
    println!("{} {} (server: {})",
        "you are:".dimmed(), user.cyan().bold(),
        cfg.server_url().unwrap_or_default().dimmed());
    Ok(())
}

// rpassword shim — keep an internal mod so we don't add another crate.
// Reads from stdin echoing for simplicity; production uses rpassword.
mod rpassword {
    use std::io::{self, BufRead, Write};
    pub fn prompt_password(prompt: &str) -> std::io::Result<String> {
        print!("{prompt}");
        io::stdout().flush()?;
        let stdin = io::stdin();
        let mut line = String::new();
        stdin.lock().read_line(&mut line)?;
        Ok(line.trim_end_matches(['\n', '\r']).to_string())
    }
}
