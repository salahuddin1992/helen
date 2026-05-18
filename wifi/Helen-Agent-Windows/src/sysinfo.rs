//! System snapshot collection — CPU, RAM, disks, network, GPU, battery.

use std::collections::HashMap;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use sysinfo::{Components, Disks, Networks, System, MINIMUM_CPU_UPDATE_INTERVAL};

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct SystemSnapshot {
    pub captured_at: String,
    pub hostname: String,
    pub os: OsInfo,
    pub cpu: CpuInfo,
    pub memory: MemoryInfo,
    pub disks: Vec<DiskInfo>,
    pub network: Vec<NetworkInterfaceInfo>,
    pub gpu: Vec<GpuInfo>,
    pub battery: Option<BatteryInfo>,
    pub uptime_secs: u64,
    pub boot_time_secs: u64,
    pub logged_in_user: Option<String>,
    pub locked: bool,
    pub process_count: usize,
    pub temperature: Vec<TemperatureInfo>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct OsInfo {
    pub name: String,
    pub version: String,
    pub kernel: String,
    pub long_os_version: Option<String>,
    pub host_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct CpuInfo {
    pub brand: String,
    pub vendor: String,
    pub physical_cores: usize,
    pub logical_cores: usize,
    pub frequency_mhz: u64,
    pub usage_percent: f32,
    pub per_core_usage: Vec<f32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct MemoryInfo {
    pub total_bytes: u64,
    pub used_bytes: u64,
    pub free_bytes: u64,
    pub available_bytes: u64,
    pub swap_total_bytes: u64,
    pub swap_used_bytes: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct DiskInfo {
    pub name: String,
    pub mount_point: String,
    pub filesystem: String,
    pub kind: String,
    pub total_bytes: u64,
    pub available_bytes: u64,
    pub is_removable: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct NetworkInterfaceInfo {
    pub name: String,
    pub mac: String,
    pub ipv4: Vec<String>,
    pub ipv6: Vec<String>,
    pub received_bytes: u64,
    pub transmitted_bytes: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct GpuInfo {
    pub name: String,
    pub vendor: Option<String>,
    pub driver_version: Option<String>,
    pub vram_bytes: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct BatteryInfo {
    pub state: String,
    pub percentage: f32,
    pub ac_powered: bool,
    pub seconds_remaining: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct TemperatureInfo {
    pub label: String,
    pub temperature_celsius: f32,
}

pub struct Collector {
    sys: System,
    nets: Networks,
    disks: Disks,
    components: Components,
}

impl Collector {
    pub fn new() -> Self {
        let mut sys = System::new_all();
        sys.refresh_all();
        let mut nets = Networks::new_with_refreshed_list();
        nets.refresh();
        let mut disks = Disks::new_with_refreshed_list();
        disks.refresh();
        let components = Components::new_with_refreshed_list();
        Self { sys, nets, disks, components }
    }

    pub fn snapshot(&mut self) -> SystemSnapshot {
        // Refresh in a way that respects the minimum tick required for CPU.
        std::thread::sleep(MINIMUM_CPU_UPDATE_INTERVAL.min(Duration::from_millis(250)));
        self.sys.refresh_all();
        self.nets.refresh();
        self.disks.refresh();
        self.components.refresh();

        let cpu = CpuInfo {
            brand: self.sys.cpus().first().map(|c| c.brand().to_string()).unwrap_or_default(),
            vendor: self.sys.cpus().first().map(|c| c.vendor_id().to_string()).unwrap_or_default(),
            physical_cores: System::physical_core_count().unwrap_or(0),
            logical_cores: self.sys.cpus().len(),
            frequency_mhz: self.sys.cpus().first().map(|c| c.frequency()).unwrap_or(0),
            usage_percent: self.sys.global_cpu_info().cpu_usage(),
            per_core_usage: self.sys.cpus().iter().map(|c| c.cpu_usage()).collect(),
        };
        let memory = MemoryInfo {
            total_bytes: self.sys.total_memory(),
            used_bytes: self.sys.used_memory(),
            free_bytes: self.sys.free_memory(),
            available_bytes: self.sys.available_memory(),
            swap_total_bytes: self.sys.total_swap(),
            swap_used_bytes: self.sys.used_swap(),
        };
        let disks: Vec<DiskInfo> = self
            .disks
            .iter()
            .map(|d| DiskInfo {
                name: d.name().to_string_lossy().to_string(),
                mount_point: d.mount_point().to_string_lossy().to_string(),
                filesystem: d.file_system().to_string_lossy().to_string(),
                kind: format!("{:?}", d.kind()),
                total_bytes: d.total_space(),
                available_bytes: d.available_space(),
                is_removable: d.is_removable(),
            })
            .collect();
        let network = collect_network(&self.nets);
        let gpu = collect_gpu();
        let battery = collect_battery();
        let temperature: Vec<TemperatureInfo> = self
            .components
            .iter()
            .map(|c| TemperatureInfo {
                label: c.label().to_string(),
                temperature_celsius: c.temperature(),
            })
            .collect();

        SystemSnapshot {
            captured_at: chrono::Utc::now().to_rfc3339(),
            hostname: System::host_name().unwrap_or_default(),
            os: OsInfo {
                name: System::name().unwrap_or_else(|| "Windows".into()),
                version: System::os_version().unwrap_or_default(),
                kernel: System::kernel_version().unwrap_or_default(),
                long_os_version: System::long_os_version(),
                host_id: System::host_name(),
            },
            cpu,
            memory,
            disks,
            network,
            gpu,
            battery,
            uptime_secs: System::uptime(),
            boot_time_secs: System::boot_time(),
            logged_in_user: current_user(),
            locked: detect_locked(),
            process_count: self.sys.processes().len(),
            temperature,
        }
    }
}

impl Default for Collector {
    fn default() -> Self { Self::new() }
}

fn collect_network(nets: &Networks) -> Vec<NetworkInterfaceInfo> {
    let mut map: HashMap<String, NetworkInterfaceInfo> = HashMap::new();

    for (name, data) in nets.iter() {
        let mac = data.mac_address().to_string();
        let entry = map.entry(name.clone()).or_insert_with(|| NetworkInterfaceInfo {
            name: name.clone(),
            mac,
            ipv4: vec![],
            ipv6: vec![],
            received_bytes: 0,
            transmitted_bytes: 0,
        });
        entry.received_bytes = data.total_received();
        entry.transmitted_bytes = data.total_transmitted();
    }

    // Pull IP addresses via ipconfig crate (Windows-specific) when available.
    #[cfg(target_os = "windows")]
    {
        if let Ok(adapters) = ipconfig::get_adapters() {
            for a in adapters {
                let key = a.friendly_name().to_string();
                let entry = map.entry(key.clone()).or_insert_with(|| NetworkInterfaceInfo {
                    name: key.clone(),
                    mac: a
                        .physical_address()
                        .map(|m| {
                            m.iter()
                                .map(|b| format!("{:02X}", b))
                                .collect::<Vec<_>>()
                                .join(":")
                        })
                        .unwrap_or_default(),
                    ipv4: vec![],
                    ipv6: vec![],
                    received_bytes: 0,
                    transmitted_bytes: 0,
                });
                for ip in a.ip_addresses() {
                    if ip.is_ipv4() {
                        entry.ipv4.push(ip.to_string());
                    } else {
                        entry.ipv6.push(ip.to_string());
                    }
                }
            }
        }
    }

    map.into_values().collect()
}

#[cfg(target_os = "windows")]
fn collect_gpu() -> Vec<GpuInfo> {
    // WMI is not available without extra deps; fall back to registry walk.
    use winreg::enums::*;
    use winreg::RegKey;
    let hklm = RegKey::predef(HKEY_LOCAL_MACHINE);
    let base = match hklm
        .open_subkey_with_flags(
            r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}",
            KEY_READ | KEY_WOW64_64KEY,
        ) {
        Ok(k) => k,
        Err(_) => return vec![],
    };
    let mut gpus = Vec::new();
    for sub_name in base.enum_keys().flatten() {
        if let Ok(sub) = base.open_subkey(&sub_name) {
            let name: Option<String> = sub.get_value("DriverDesc").ok();
            let driver: Option<String> = sub.get_value("DriverVersion").ok();
            let vendor: Option<String> = sub.get_value("ProviderName").ok();
            let vram: Option<u32> = sub.get_value("HardwareInformation.qwMemorySize").ok().or_else(|| sub.get_value("HardwareInformation.MemorySize").ok());
            if let Some(n) = name {
                gpus.push(GpuInfo {
                    name: n,
                    vendor,
                    driver_version: driver,
                    vram_bytes: vram.map(|v| v as u64),
                });
            }
        }
    }
    gpus
}

#[cfg(not(target_os = "windows"))]
fn collect_gpu() -> Vec<GpuInfo> { vec![] }

#[cfg(target_os = "windows")]
fn collect_battery() -> Option<BatteryInfo> {
    use windows::Win32::System::Power::{GetSystemPowerStatus, SYSTEM_POWER_STATUS};
    let mut status = SYSTEM_POWER_STATUS::default();
    unsafe {
        if GetSystemPowerStatus(&mut status).is_err() {
            return None;
        }
    }
    if status.BatteryFlag == 128 {
        // No battery
        return None;
    }
    let percentage = if status.BatteryLifePercent == 255 {
        -1.0
    } else {
        status.BatteryLifePercent as f32
    };
    let state = match status.ACLineStatus {
        0 => "Discharging",
        1 => "Charging",
        _ => "Unknown",
    };
    Some(BatteryInfo {
        state: state.into(),
        percentage,
        ac_powered: status.ACLineStatus == 1,
        seconds_remaining: if status.BatteryLifeTime == u32::MAX {
            None
        } else {
            Some(status.BatteryLifeTime as i64)
        },
    })
}

#[cfg(not(target_os = "windows"))]
fn collect_battery() -> Option<BatteryInfo> { None }

#[cfg(target_os = "windows")]
fn current_user() -> Option<String> {
    std::env::var("USERNAME").ok()
}

#[cfg(not(target_os = "windows"))]
fn current_user() -> Option<String> { std::env::var("USER").ok() }

#[cfg(target_os = "windows")]
fn detect_locked() -> bool {
    // Best-effort — checks whether `LogonUI.exe` is foreground.
    use windows::Win32::UI::WindowsAndMessaging::{GetForegroundWindow, GetWindowThreadProcessId};
    use windows::Win32::System::ProcessStatus::{GetModuleBaseNameW};
    use windows::Win32::System::Threading::{OpenProcess, PROCESS_QUERY_INFORMATION, PROCESS_VM_READ};
    use windows::Win32::Foundation::CloseHandle;

    unsafe {
        let hwnd = GetForegroundWindow();
        if hwnd.0.is_null() {
            return false;
        }
        let mut pid: u32 = 0;
        GetWindowThreadProcessId(hwnd, Some(&mut pid));
        if pid == 0 {
            return false;
        }
        if let Ok(handle) = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, false, pid) {
            let mut buf = [0u16; 260];
            let n = GetModuleBaseNameW(handle, None, &mut buf);
            let _ = CloseHandle(handle);
            if n > 0 {
                let name = String::from_utf16_lossy(&buf[..n as usize]);
                return name.eq_ignore_ascii_case("LogonUI.exe");
            }
        }
        false
    }
}

#[cfg(not(target_os = "windows"))]
fn detect_locked() -> bool { false }
