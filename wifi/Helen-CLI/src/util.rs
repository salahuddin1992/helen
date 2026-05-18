//! Pretty-printers and small helpers.
use colored::*;
use comfy_table::{presets::UTF8_FULL, Cell, Table};
use serde::Deserialize;

#[derive(Debug, Deserialize)]
pub struct ChannelRow {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub topic: Option<String>,
    #[serde(default)]
    pub member_count: Option<i64>,
    #[serde(default)]
    pub last_message_at: Option<String>,
}

pub fn print_channels(channels: &[ChannelRow]) {
    let mut t = Table::new();
    t.load_preset(UTF8_FULL);
    t.set_header(vec!["ID", "Name", "Members", "Topic", "Last activity"]);
    for c in channels {
        t.add_row(vec![
            Cell::new(&c.id),
            Cell::new(c.name.bold().to_string()),
            Cell::new(c.member_count.unwrap_or(0).to_string()),
            Cell::new(c.topic.clone().unwrap_or_default()),
            Cell::new(c.last_message_at.clone().unwrap_or_default()),
        ]);
    }
    println!("{t}");
}

#[derive(Debug, Deserialize)]
pub struct MessageRow {
    pub id: String,
    pub channel_id: String,
    pub sender_id: String,
    #[serde(default)]
    pub sender_name: Option<String>,
    pub content: String,
    pub created_at: String,
}

pub fn print_message_bubble(m: &MessageRow) {
    let who = m.sender_name.clone().unwrap_or_else(|| m.sender_id.clone());
    let ts = m.created_at.trim_end_matches('Z');
    println!(
        "{} {} {}",
        format!("[{ts}]").dimmed(),
        who.cyan().bold(),
        format!("│ {}", m.content),
    );
}

pub fn print_kv(rows: &[(&str, String)]) {
    let mut t = Table::new();
    t.load_preset(UTF8_FULL);
    for (k, v) in rows {
        t.add_row(vec![Cell::new((*k).bold().to_string()), Cell::new(v)]);
    }
    println!("{t}");
}

pub fn human_bytes(b: u64) -> String {
    const U: [&str; 5] = ["B", "KiB", "MiB", "GiB", "TiB"];
    let mut v = b as f64;
    let mut i = 0;
    while v >= 1024.0 && i < U.len() - 1 {
        v /= 1024.0;
        i += 1;
    }
    format!("{v:.2} {}", U[i])
}
