//! Tracing initialiser — colour console + optional JSON log file.
use tracing_subscriber::{fmt, EnvFilter};

pub fn init(verbose: u8) {
    let level = match verbose {
        0 => "info",
        1 => "debug",
        _ => "trace",
    };
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new(format!("helen_cli={level},reqwest=warn")));

    let _ = fmt()
        .with_env_filter(filter)
        .with_target(false)
        .with_level(true)
        .compact()
        .try_init();
}
