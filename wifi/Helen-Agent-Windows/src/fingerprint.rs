//! Stable device fingerprint generation.
//!
//! Combines several Windows-specific hardware/OS anchors to derive a SHA-256
//! identifier that remains constant across reboots and updates but rotates if
//! the device is meaningfully reimaged.

use sha2::{Digest, Sha256};

use crate::config::{registry_get, registry_set};
use crate::error::{AgentError, Result};

pub const FINGERPRINT_REG_NAME: &str = "Fingerprint";

pub fn current_or_compute() -> Result<String> {
    if let Some(stored) = registry_get(FINGERPRINT_REG_NAME)? {
        if stored.len() == 64 && stored.chars().all(|c| c.is_ascii_hexdigit()) {
            return Ok(stored);
        }
    }
    let fp = compute()?;
    let _ = registry_set(FINGERPRINT_REG_NAME, &fp);
    Ok(fp)
}

pub fn compute() -> Result<String> {
    let mut hasher = Sha256::new();
    hasher.update(b"helen-agent:v1\0");

    let machine_guid = read_machine_guid().unwrap_or_else(|| "no-guid".to_string());
    hasher.update(machine_guid.as_bytes());
    hasher.update(b"\0");

    let host = hostname::get()
        .ok()
        .and_then(|h| h.into_string().ok())
        .unwrap_or_else(|| "no-host".to_string());
    hasher.update(host.as_bytes());
    hasher.update(b"\0");

    let mac = primary_mac().unwrap_or_else(|| "no-mac".to_string());
    hasher.update(mac.as_bytes());
    hasher.update(b"\0");

    let install_date = read_install_date().unwrap_or_default();
    hasher.update(install_date.as_bytes());

    let digest = hasher.finalize();
    Ok(hex::encode(digest))
}

#[cfg(target_os = "windows")]
fn read_machine_guid() -> Option<String> {
    use winreg::enums::*;
    use winreg::RegKey;
    let hklm = RegKey::predef(HKEY_LOCAL_MACHINE);
    let key = hklm
        .open_subkey_with_flags(r"SOFTWARE\Microsoft\Cryptography", KEY_READ | KEY_WOW64_64KEY)
        .ok()?;
    key.get_value::<String, _>("MachineGuid").ok()
}

#[cfg(not(target_os = "windows"))]
fn read_machine_guid() -> Option<String> {
    std::fs::read_to_string("/etc/machine-id").ok()
}

#[cfg(target_os = "windows")]
fn read_install_date() -> Option<String> {
    use winreg::enums::*;
    use winreg::RegKey;
    let hklm = RegKey::predef(HKEY_LOCAL_MACHINE);
    let key = hklm
        .open_subkey_with_flags(
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
            KEY_READ | KEY_WOW64_64KEY,
        )
        .ok()?;
    key.get_value::<u32, _>("InstallDate").ok().map(|v| v.to_string())
}

#[cfg(not(target_os = "windows"))]
fn read_install_date() -> Option<String> { Some("0".into()) }

fn primary_mac() -> Option<String> {
    let mac = mac_address::get_mac_address().ok().flatten()?;
    Some(mac.to_string())
}

#[allow(dead_code)]
pub fn verify(fp: &str) -> Result<()> {
    if fp.len() != 64 {
        return Err(AgentError::Fingerprint("fingerprint length != 64".into()));
    }
    if !fp.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err(AgentError::Fingerprint("non-hex characters".into()));
    }
    Ok(())
}
