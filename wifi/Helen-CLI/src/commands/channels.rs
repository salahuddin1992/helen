//! `helen-cli channels` — list channels.
use crate::api::ApiClient;
use crate::error::Result;
use crate::util::{print_channels, ChannelRow};

pub async fn list(api: ApiClient) -> Result<()> {
    let rows: Vec<ChannelRow> = api.get_json("/api/channels").await?;
    print_channels(&rows);
    Ok(())
}
