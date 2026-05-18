//! Tracing setup — daily-rolling log files plus stdout. The Windows Event Log
//! is integrated through a lightweight bridge so service-mode runs still leave
//! a forensic trail.

use std::path::{Path, PathBuf};

use tracing_appender::non_blocking::WorkerGuard;
use tracing_subscriber::{fmt, prelude::*, EnvFilter};

use crate::error::{AgentError, Result};

pub struct LoggingGuards {
    _file_guard: WorkerGuard,
}

pub fn init(log_dir: &Path, level: &str, service_mode: bool) -> Result<LoggingGuards> {
    std::fs::create_dir_all(log_dir).map_err(AgentError::Io)?;

    let file_appender = tracing_appender::rolling::daily(log_dir, "helen-agent.log");
    let (file_writer, file_guard) = tracing_appender::non_blocking(file_appender);

    let filter = EnvFilter::try_new(level)
        .unwrap_or_else(|_| EnvFilter::new("info,helen_agent=debug"));

    let file_layer = fmt::layer()
        .with_writer(file_writer)
        .with_ansi(false)
        .with_target(true)
        .with_thread_ids(true)
        .json();

    let registry = tracing_subscriber::registry()
        .with(filter)
        .with(file_layer);

    if service_mode {
        // No stdout — service has no console.
        registry.try_init().map_err(|e| AgentError::Internal(e.to_string()))?;
    } else {
        let stdout_layer = fmt::layer()
            .with_ansi(true)
            .with_target(false)
            .compact();
        registry
            .with(stdout_layer)
            .try_init()
            .map_err(|e| AgentError::Internal(e.to_string()))?;
    }

    Ok(LoggingGuards { _file_guard: file_guard })
}

pub fn default_log_dir() -> PathBuf {
    if let Some(data) = std::env::var_os("ProgramData") {
        PathBuf::from(data).join("Helen-Agent").join("logs")
    } else {
        PathBuf::from(".").join("logs")
    }
}

#[cfg(target_os = "windows")]
pub fn write_event_log(source: &str, message: &str, level: tracing::Level) {
    use windows::core::PCWSTR;
    use windows::Win32::System::EventLog::{
        DeregisterEventSource, RegisterEventSourceW, ReportEventW,
        EVENTLOG_ERROR_TYPE, EVENTLOG_INFORMATION_TYPE, EVENTLOG_WARNING_TYPE,
        REPORT_EVENT_TYPE,
    };

    fn to_wstr(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    let evt_type: REPORT_EVENT_TYPE = match level {
        tracing::Level::ERROR => EVENTLOG_ERROR_TYPE,
        tracing::Level::WARN => EVENTLOG_WARNING_TYPE,
        _ => EVENTLOG_INFORMATION_TYPE,
    };

    unsafe {
        let src_w = to_wstr(source);
        let handle = RegisterEventSourceW(PCWSTR::null(), PCWSTR(src_w.as_ptr()));
        if let Ok(h) = handle {
            let msg_w = to_wstr(message);
            let msg_ptr = PCWSTR(msg_w.as_ptr());
            let strings = [msg_ptr];
            let _ = ReportEventW(
                h,
                evt_type,
                0,
                0,
                None,
                0,
                Some(&strings),
                None,
            );
            let _ = DeregisterEventSource(h);
        }
    }
}

#[cfg(not(target_os = "windows"))]
pub fn write_event_log(_source: &str, _message: &str, _level: tracing::Level) {}
