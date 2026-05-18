// Build script — embeds Windows resources (icon, version info, manifest)
// into the produced binary when targeting the MSVC toolchain.

fn main() {
    #[cfg(target_os = "windows")]
    {
        let mut res = winres::WindowsResource::new();
        res.set("FileDescription", "Helen Device Agent");
        res.set("ProductName", "Helen-Agent-Windows");
        res.set("CompanyName", "Helen Project");
        res.set("LegalCopyright", "© Helen Project");
        res.set("OriginalFilename", "helen-agent.exe");
        res.set("InternalName", "helen-agent");
        res.set("FileVersion", env!("CARGO_PKG_VERSION"));
        res.set("ProductVersion", env!("CARGO_PKG_VERSION"));
        // Embed the side-by-side manifest (DPI awareness, asInvoker).
        let manifest_path = std::path::Path::new("agent.manifest");
        if manifest_path.exists() {
            res.set_manifest_file("agent.manifest");
        }
        if let Err(e) = res.compile() {
            eprintln!("winres compile failed: {}", e);
        }
    }
}
