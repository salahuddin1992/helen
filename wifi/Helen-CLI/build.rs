//! helen-cli build script — emit version + build-time metadata.
fn main() {
    let version = env!("CARGO_PKG_VERSION");
    println!("cargo:rustc-env=HELEN_CLI_VERSION={version}");

    let build_time = chrono_local_iso8601();
    println!("cargo:rustc-env=HELEN_CLI_BUILD_TIME={build_time}");

    println!("cargo:rerun-if-changed=build.rs");
}

fn chrono_local_iso8601() -> String {
    // Avoid the chrono crate at build-time — just use `SystemTime`.
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format!("epoch:{secs}")
}
