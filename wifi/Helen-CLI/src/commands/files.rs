//! `helen-cli upload / download`.
use std::path::PathBuf;

use indicatif::{ProgressBar, ProgressStyle};

use crate::api::ApiClient;
use crate::error::{CliError, Result};
use crate::util::human_bytes;

pub async fn upload(api: ApiClient, channel_id: &str, path: PathBuf) -> Result<()> {
    if !path.exists() {
        return Err(CliError::InvalidArg(format!("file not found: {}", path.display())));
    }
    let size = std::fs::metadata(&path)?.len();
    let bar = ProgressBar::new(size);
    bar.set_style(ProgressStyle::with_template(
        "{spinner:.green} [{bar:40.cyan/blue}] {bytes}/{total_bytes} {wide_msg}"
    ).unwrap());
    bar.set_message(format!("uploading {}", path.display()));

    let res = api.upload_file(channel_id, &path).await?;
    bar.finish_with_message("done");
    println!("uploaded: {}", serde_json::to_string_pretty(&res).unwrap_or_default());
    Ok(())
}

pub async fn download(
    api: ApiClient, file_id: &str, out: PathBuf, resume: bool,
) -> Result<()> {
    println!("downloading {} → {}", file_id, out.display());
    let n = api.download_to(file_id, &out, resume).await?;
    println!("wrote {} ({})", out.display(), human_bytes(n));
    Ok(())
}
