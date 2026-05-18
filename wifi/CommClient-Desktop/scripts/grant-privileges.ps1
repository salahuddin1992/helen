#Requires -RunAsAdministrator
<#
  grant-privileges.ps1 — one-shot Windows privacy + firewall grant for Helen.

  Flips every Windows capability-access toggle to "Allow" for the current
  user, then writes explicit per-exe allow entries for Helen (installed
  builds + dev Electron), then opens the LAN ports Helen needs.

  Safe to re-run; each step is idempotent. Must be elevated.
#>

param(
  [string[]] $ExtraExePaths = @()
)

$ErrorActionPreference = 'Stop'
function Write-Step([string] $msg) { Write-Host "[*] $msg" -ForegroundColor Cyan }

# ── Capabilities to unlock ───────────────────────────────────
# Cover every privacy surface Windows exposes — camera, mic, location, plus
# every library/device surface so future features (screen capture, contacts,
# bluetooth pairing) don't hit a permission wall.
$Capabilities = @(
  'webcam', 'microphone', 'location', 'bluetoothSync', 'phoneCall',
  'phoneCallHistory', 'contacts', 'appointments', 'email',
  'userAccountInformation', 'radios', 'humanInterfaceDevice', 'gazeInput',
  'documentsLibrary', 'picturesLibrary', 'videosLibrary', 'musicLibrary',
  'broadFileSystemAccess', 'graphicsCaptureProgrammatic',
  'graphicsCaptureWithoutBorder', 'activity', 'cellularData', 'sensors.custom'
)

function Set-RegValue {
  param([string] $Path, [string] $Name, $Value, [string] $Type = 'String')
  if (-not (Test-Path $Path)) { New-Item -Path $Path -Force | Out-Null }
  New-ItemProperty -Path $Path -Name $Name -Value $Value -PropertyType $Type -Force | Out-Null
}

Write-Step 'Unlocking global capability toggles (HKLM + HKCU)'
foreach ($cap in $Capabilities) {
  foreach ($hive in 'HKLM:', 'HKCU:') {
    $base = "$hive\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\$cap"
    Set-RegValue -Path $base -Name 'Value' -Value 'Allow'
    # "Let desktop apps access ..." lives under NonPackaged.
    Set-RegValue -Path "$base\NonPackaged" -Name 'Value' -Value 'Allow'
  }
}

# ── Per-exe explicit allow for Helen ─────────────────────────
# Windows keeps a per-executable history under NonPackaged\<path-with-#>.
# Seeding this entry with Value=Allow prevents the privacy UI from ever
# prompting or silently denying this process.
$ExePaths = @(
  "$env:LOCALAPPDATA\Programs\CommClient\Helen.exe",                                         # per-user install
  "$env:ProgramFiles\CommClient\Helen.exe",                                                   # per-machine install
  "C:\Users\youse\c\wifi\CommClient-Desktop\node_modules\electron\dist\electron.exe",         # dev electron
  "C:\Users\youse\c\wifi\CommClient-Server\dist\Helen-Server\Helen-Server.exe"                # server bundle
) + $ExtraExePaths

Write-Step "Granting explicit camera + mic allow for $($ExePaths.Count) exe path(s)"
foreach ($exe in $ExePaths) {
  $escaped = $exe -replace '\\', '#'
  foreach ($cap in 'webcam', 'microphone', 'graphicsCaptureProgrammatic', 'graphicsCaptureWithoutBorder') {
    $key = "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\$cap\NonPackaged\$escaped"
    Set-RegValue -Path $key -Name 'Value' -Value 'Allow'
    # Zero the last-used timestamps so Windows doesn't treat the entry as
    # a stale history row that it might scrub.
    Set-RegValue -Path $key -Name 'LastUsedTimeStart' -Value 0 -Type 'QWord'
    Set-RegValue -Path $key -Name 'LastUsedTimeStop' -Value 0 -Type 'QWord'
  }
}

# ── Firewall rules ───────────────────────────────────────────
Write-Step 'Opening LAN ports on Windows Firewall'
$rules = @(
  @{ Name = 'Helen LAN Discovery (UDP 41234)'; Port = 41234; Protocol = 'UDP' },
  @{ Name = 'Helen Server HTTP (TCP 3000)';    Port = 3000;  Protocol = 'TCP' },
  @{ Name = 'Helen Vite Dev (TCP 5173)';       Port = 5173;  Protocol = 'TCP' },
  @{ Name = 'Helen mediasoup RTC (UDP 40000-49999)'; Port = '40000-49999'; Protocol = 'UDP' }
)
foreach ($r in $rules) {
  foreach ($dir in 'Inbound', 'Outbound') {
    $display = "$($r.Name) $dir"
    Get-NetFirewallRule -DisplayName $display -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName $display -Direction $dir -Protocol $r.Protocol `
      -LocalPort $r.Port -Action Allow -Profile Any -ErrorAction SilentlyContinue | Out-Null
  }
}

# ── Windows Defender exclusions (prevents AV from quarantining dev files) ──
Write-Step 'Adding Defender exclusions for Helen source + build outputs'
$defenderPaths = @(
  'C:\Users\youse\c\wifi\CommClient-Desktop',
  'C:\Users\youse\c\wifi\CommClient-Server'
)
foreach ($p in $defenderPaths) {
  try { Add-MpPreference -ExclusionPath $p -ErrorAction Stop } catch { Write-Warning "Defender exclusion skipped for $p — $($_.Exception.Message)" }
}

Write-Host ''
Write-Host '[OK] Helen granted full device + network privileges.' -ForegroundColor Green
Write-Host '    Restart Electron / the server so new permissions take effect.'
