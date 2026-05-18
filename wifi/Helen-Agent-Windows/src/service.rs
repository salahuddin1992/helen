//! Windows Service integration — install/uninstall hooks, SCM event loop, and
//! a foreground runner suitable for both service and console execution.

use std::sync::Arc;
use std::time::Duration;

use tokio::runtime::Runtime;
use tracing::{error, info};

use crate::config::ConfigStore;
use crate::error::{AgentError, Result};

pub const SERVICE_NAME: &str = "HelenAgent";
pub const SERVICE_DISPLAY: &str = "Helen Device Agent";
pub const SERVICE_DESCRIPTION: &str =
    "Registers the device with Helen-Server and provides managed remote operation.";

#[cfg(target_os = "windows")]
pub fn install() -> Result<()> {
    use windows_service::service::{
        ServiceAccess, ServiceErrorControl, ServiceInfo, ServiceStartType, ServiceType,
    };
    use windows_service::service_manager::{ServiceManager, ServiceManagerAccess};

    let current_exe = std::env::current_exe()
        .map_err(|e| AgentError::Service(format!("current_exe: {e}")))?;

    let manager_access = ServiceManagerAccess::CONNECT | ServiceManagerAccess::CREATE_SERVICE;
    let manager = ServiceManager::local_computer(None::<&str>, manager_access)
        .map_err(|e| AgentError::Service(format!("connect SCM: {e}")))?;

    let service_info = ServiceInfo {
        name: SERVICE_NAME.into(),
        display_name: SERVICE_DISPLAY.into(),
        service_type: ServiceType::OWN_PROCESS,
        start_type: ServiceStartType::AutoStart,
        error_control: ServiceErrorControl::Normal,
        executable_path: current_exe,
        launch_arguments: vec!["run".into(), "--service".into()],
        dependencies: vec![],
        account_name: None, // LocalSystem
        account_password: None,
    };
    let service = manager
        .create_service(&service_info, ServiceAccess::CHANGE_CONFIG | ServiceAccess::START)
        .map_err(|e| AgentError::Service(format!("create_service: {e}")))?;
    service
        .set_description(SERVICE_DESCRIPTION)
        .map_err(|e| AgentError::Service(format!("set_description: {e}")))?;
    info!(target: "service", "service installed");
    Ok(())
}

#[cfg(target_os = "windows")]
pub fn uninstall() -> Result<()> {
    use std::thread::sleep;
    use windows_service::service::{ServiceAccess, ServiceState};
    use windows_service::service_manager::{ServiceManager, ServiceManagerAccess};

    let manager = ServiceManager::local_computer(None::<&str>, ServiceManagerAccess::CONNECT)
        .map_err(|e| AgentError::Service(format!("connect SCM: {e}")))?;

    let service_access =
        ServiceAccess::QUERY_STATUS | ServiceAccess::STOP | ServiceAccess::DELETE;
    let service = manager
        .open_service(SERVICE_NAME, service_access)
        .map_err(|e| AgentError::Service(format!("open_service: {e}")))?;

    let status = service
        .query_status()
        .map_err(|e| AgentError::Service(format!("query_status: {e}")))?;
    if status.current_state != ServiceState::Stopped {
        let _ = service.stop();
        for _ in 0..30 {
            sleep(Duration::from_millis(500));
            if let Ok(s) = service.query_status() {
                if s.current_state == ServiceState::Stopped {
                    break;
                }
            }
        }
    }
    service
        .delete()
        .map_err(|e| AgentError::Service(format!("delete: {e}")))?;
    info!(target: "service", "service uninstalled");
    Ok(())
}

#[cfg(target_os = "windows")]
pub fn run_as_service() -> Result<()> {
    windows_service::service_dispatcher::start(SERVICE_NAME, ffi_service_main)
        .map_err(|e| AgentError::Service(format!("dispatcher start: {e}")))?;
    Ok(())
}

#[cfg(target_os = "windows")]
windows_service::define_windows_service!(ffi_service_main, my_service_main);

#[cfg(target_os = "windows")]
fn my_service_main(_args: Vec<std::ffi::OsString>) {
    use std::sync::atomic::{AtomicBool, Ordering};
    use windows_service::service::{
        ServiceControl, ServiceControlAccept, ServiceExitCode, ServiceState, ServiceStatus,
        ServiceType,
    };
    use windows_service::service_control_handler::{self, ServiceControlHandlerResult};

    let stop_requested = Arc::new(AtomicBool::new(false));
    let stop_clone = stop_requested.clone();

    let event_handler = move |control: ServiceControl| -> ServiceControlHandlerResult {
        match control {
            ServiceControl::Interrogate => ServiceControlHandlerResult::NoError,
            ServiceControl::Stop | ServiceControl::Shutdown => {
                stop_clone.store(true, Ordering::SeqCst);
                ServiceControlHandlerResult::NoError
            }
            _ => ServiceControlHandlerResult::NotImplemented,
        }
    };

    let status_handle = match service_control_handler::register(SERVICE_NAME, event_handler) {
        Ok(h) => h,
        Err(e) => {
            crate::logging::write_event_log(
                SERVICE_NAME,
                &format!("register handler failed: {e}"),
                tracing::Level::ERROR,
            );
            return;
        }
    };

    let _ = status_handle.set_service_status(ServiceStatus {
        service_type: ServiceType::OWN_PROCESS,
        current_state: ServiceState::Running,
        controls_accepted: ServiceControlAccept::STOP | ServiceControlAccept::SHUTDOWN,
        exit_code: ServiceExitCode::Win32(0),
        checkpoint: 0,
        wait_hint: Duration::from_secs(0),
        process_id: None,
    });

    let rt = match Runtime::new() {
        Ok(r) => r,
        Err(e) => {
            crate::logging::write_event_log(
                SERVICE_NAME,
                &format!("runtime build failed: {e}"),
                tracing::Level::ERROR,
            );
            return;
        }
    };

    let agent_result = rt.block_on(async move {
        let cfg = ConfigStore::load_or_create()?;
        run_agent_loop(cfg, stop_requested.clone()).await
    });
    if let Err(e) = agent_result {
        error!(target: "service", error = %e, "agent run failed");
    }

    let _ = status_handle.set_service_status(ServiceStatus {
        service_type: ServiceType::OWN_PROCESS,
        current_state: ServiceState::Stopped,
        controls_accepted: ServiceControlAccept::empty(),
        exit_code: ServiceExitCode::Win32(0),
        checkpoint: 0,
        wait_hint: Duration::from_secs(0),
        process_id: None,
    });
}

#[cfg(not(target_os = "windows"))]
pub fn install() -> Result<()> { Err(AgentError::Service("Windows only".into())) }
#[cfg(not(target_os = "windows"))]
pub fn uninstall() -> Result<()> { Err(AgentError::Service("Windows only".into())) }
#[cfg(not(target_os = "windows"))]
pub fn run_as_service() -> Result<()> { Err(AgentError::Service("Windows only".into())) }

pub async fn run_agent_loop(
    cfg: ConfigStore,
    #[cfg_attr(not(target_os = "windows"), allow(unused_variables))]
    stop: Arc<std::sync::atomic::AtomicBool>,
) -> Result<()> {
    use std::sync::atomic::Ordering;

    let snap = cfg.snapshot();
    let http = reqwest::Client::builder()
        .timeout(Duration::from_secs(snap.http_timeout_secs))
        .danger_accept_invalid_certs(!snap.verify_tls)
        .build()
        .map_err(AgentError::Http)?;
    let auth = crate::auth::AuthManager::new(cfg.clone(), http.clone());

    let _hb = crate::heartbeat::spawn(cfg.clone(), auth.clone(), http.clone());

    let ws = Arc::new(crate::ws_channel::ControlClient::new(
        cfg.clone(),
        auth.clone(),
        http.clone(),
    ));
    let ws_clone = ws.clone();
    tokio::spawn(async move { ws_clone.run_forever().await });

    info!(target: "agent", version = env!("CARGO_PKG_VERSION"), "agent up");
    loop {
        if stop.load(Ordering::SeqCst) {
            info!(target: "agent", "stop requested by SCM");
            break;
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
    Ok(())
}
