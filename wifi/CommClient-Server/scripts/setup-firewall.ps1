#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Configure Windows Firewall for CommClient-Server

.DESCRIPTION
    Sets up firewall inbound rules to allow CommClient-Server operation:
    - TCP 3000: HTTP + Socket.IO (main server)
    - UDP 41234: Discovery broadcast
    - UDP 40000-49999: mediasoup RTP streams
    - UDP 5353: mDNS (Multicast DNS)
    - All rules prefixed with "CommClient" for easy management

.PARAMETER ProfileType
    Firewall profile(s) to configure (default: All)
    Valid values: Public, Private, Domain, All

.PARAMETER Enable
    Enable all CommClient firewall rules (default action)

.PARAMETER Disable
    Disable all CommClient firewall rules

.PARAMETER Remove
    Remove all CommClient firewall rules

.PARAMETER ListRules
    List all CommClient-related firewall rules

.EXAMPLE
    PS> .\setup-firewall.ps1
    # Enables all CommClient firewall rules for all profiles

.EXAMPLE
    PS> .\setup-firewall.ps1 -ProfileType "Private, Domain"
    # Enables rules only for Private and Domain profiles

.EXAMPLE
    PS> .\setup-firewall.ps1 -Remove
    # Removes all CommClient firewall rules
#>

param(
    [ValidateSet("Public", "Private", "Domain", "All")]
    [string]$ProfileType = "All",

    [switch]$Enable,
    [switch]$Disable,
    [switch]$Remove,
    [switch]$ListRules
)

# ============================================================================
# Configuration
# ============================================================================
$ErrorActionPreference = "Stop"
$VerbosePreference = "Continue"

$RulePrefix = "CommClient"

# Define all firewall rules
$FirewallRules = @(
    @{
        Name        = "$RulePrefix-HTTP-SocketIO"
        DisplayName = "CommClient Server - HTTP and Socket.IO (TCP 3000)"
        Direction   = "Inbound"
        Action      = "Allow"
        Protocol    = "TCP"
        LocalPort   = 3000
        Program     = ""
        Description = "HTTP and WebSocket (Socket.IO) server endpoint"
    },
    @{
        Name        = "$RulePrefix-Discovery"
        DisplayName = "CommClient Server - Discovery Broadcast (UDP 41234)"
        Direction   = "Inbound"
        Action      = "Allow"
        Protocol    = "UDP"
        LocalPort   = 41234
        Program     = ""
        Description = "LAN device discovery broadcast"
    },
    @{
        Name        = "$RulePrefix-Mediasoup-RTP"
        DisplayName = "CommClient Server - Mediasoup RTP (UDP 40000-49999)"
        Direction   = "Inbound"
        Action      = "Allow"
        Protocol    = "UDP"
        LocalPort   = "40000-49999"
        Program     = ""
        Description = "RTP media streams for calls and screen sharing"
    },
    @{
        Name        = "$RulePrefix-mDNS"
        DisplayName = "CommClient Server - mDNS (UDP 5353)"
        Direction   = "Inbound"
        Action      = "Allow"
        Protocol    = "UDP"
        LocalPort   = 5353
        Program     = ""
        Description = "Multicast DNS (Bonjour/Avahi) discovery"
    }
)

# ============================================================================
# Helper Functions
# ============================================================================

function Write-Status {
    param([string]$Message, [string]$Type = "Info")
    $timestamp = Get-Date -Format "HH:mm:ss"
    $prefix = switch ($Type) {
        "Success" { "[✓]" }
        "Error" { "[✗]" }
        "Warning" { "[!]" }
        "Info" { "[*]" }
        default { "[*]" }
    }
    $color = switch ($Type) {
        "Success" { "Green" }
        "Error" { "Red" }
        "Warning" { "Yellow" }
        default { "Cyan" }
    }
    Write-Host "$prefix [$timestamp] $Message" -ForegroundColor $color
}

function Test-AdminPrivileges {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal $identity
    return $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
}

function Get-ProfileList {
    param([string]$Profiles)

    if ($Profiles -eq "All") {
        return @("Public", "Private", "Domain")
    }
    return @($Profiles.Split(",") | ForEach-Object { $_.Trim() })
}

function Create-FirewallRule {
    param(
        [hashtable]$RuleConfig,
        [string]$Profile
    )

    $ruleName = $RuleConfig.Name
    $displayName = "$($RuleConfig.DisplayName) [$Profile]"

    # Check if rule exists
    $existingRule = Get-NetFirewallRule -DisplayName $displayName -ErrorAction SilentlyContinue
    if ($existingRule) {
        Write-Status "Rule already exists: $displayName" "Info"
        return $existingRule
    }

    Write-Status "Creating firewall rule: $displayName" "Info"

    try {
        $params = @{
            Name            = "$ruleName-$Profile"
            DisplayName     = $displayName
            Direction       = $RuleConfig.Direction
            Action          = $RuleConfig.Action
            Protocol        = $RuleConfig.Protocol
            LocalPort       = $RuleConfig.LocalPort
            Enabled         = $true
            Profile         = $Profile
            Description     = $RuleConfig.Description
            ErrorAction     = "Stop"
        }

        $rule = New-NetFirewallRule @params
        Write-Status "  ✓ Rule created" "Success"
        return $rule
    }
    catch {
        Write-Status "  ✗ Failed to create rule: $_" "Error"
        return $null
    }
}

function Enable-FirewallRules {
    Write-Status "Enabling CommClient firewall rules..." "Info"

    $profiles = Get-ProfileList $ProfileType
    $enabledCount = 0

    foreach ($profile in $profiles) {
        Write-Status "Profile: $profile" "Info"

        foreach ($ruleConfig in $FirewallRules) {
            try {
                $ruleName = $ruleConfig.Name
                $displayName = "$($ruleConfig.DisplayName) [$profile]"

                # Try to find and enable existing rule
                $rule = Get-NetFirewallRule -DisplayName $displayName -ErrorAction SilentlyContinue
                if ($rule) {
                    if ($rule.Enabled -eq $false) {
                        Set-NetFirewallRule -InputObject $rule -Enabled $true
                        Write-Status "  ✓ Enabled: $($ruleConfig.DisplayName)" "Success"
                        $enabledCount++
                    }
                    else {
                        Write-Status "  ✓ Already enabled: $($ruleConfig.DisplayName)" "Info"
                    }
                }
                else {
                    # Create new rule
                    $newRule = Create-FirewallRule $ruleConfig $profile
                    if ($newRule) {
                        $enabledCount++
                    }
                }
            }
            catch {
                Write-Status "  ✗ Error with rule $($ruleConfig.Name): $_" "Warning"
            }
        }
    }

    Write-Status "Enabled/created $enabledCount rule(s)" "Success"
}

function Disable-FirewallRules {
    Write-Status "Disabling CommClient firewall rules..." "Warning"

    $disabledCount = 0

    $rules = Get-NetFirewallRule -DisplayName "*$RulePrefix*" -ErrorAction SilentlyContinue
    foreach ($rule in $rules) {
        try {
            Set-NetFirewallRule -InputObject $rule -Enabled $false
            Write-Status "  ✓ Disabled: $($rule.DisplayName)" "Success"
            $disabledCount++
        }
        catch {
            Write-Status "  ✗ Error disabling $($rule.DisplayName): $_" "Warning"
        }
    }

    Write-Status "Disabled $disabledCount rule(s)" "Success"
}

function Remove-FirewallRules {
    Write-Status "Removing CommClient firewall rules..." "Warning"

    $removedCount = 0

    $rules = Get-NetFirewallRule -DisplayName "*$RulePrefix*" -ErrorAction SilentlyContinue
    foreach ($rule in $rules) {
        try {
            Remove-NetFirewallRule -InputObject $rule -Confirm:$false
            Write-Status "  ✓ Removed: $($rule.DisplayName)" "Success"
            $removedCount++
        }
        catch {
            Write-Status "  ✗ Error removing $($rule.DisplayName): $_" "Warning"
        }
    }

    Write-Status "Removed $removedCount rule(s)" "Success"
}

function Show-FirewallRules {
    Write-Status "CommClient Firewall Rules:" "Info"

    $rules = Get-NetFirewallRule -DisplayName "*$RulePrefix*" -ErrorAction SilentlyContinue
    if (-not $rules) {
        Write-Status "No CommClient firewall rules found" "Warning"
        return
    }

    Write-Host ""
    Write-Host "Rule Name                                       Enabled  Direction  Action  Profile" -ForegroundColor Cyan
    Write-Host "─────────────────────────────────────────────────────────────────────────────────" -ForegroundColor Cyan

    foreach ($rule in $rules | Sort-Object DisplayName) {
        $enabled = if ($rule.Enabled) { "Yes    " } else { "No     " }
        $direction = $rule.Direction.ToString().PadRight(10)
        $action = $rule.PrimaryStatus.ToString().PadRight(7)
        $profile = $rule.Profile -join ","

        Write-Host "$($rule.DisplayName.PadRight(45)) $enabled $direction $action  $profile" -ForegroundColor Green
    }

    Write-Host ""

    # Summary
    $enabledRules = $rules | Where-Object { $rule.Enabled } | Measure-Object
    Write-Status "Total rules: $($rules.Count), Enabled: $($enabledRules.Count)" "Info"
}

function Verify-Connectivity {
    Write-Status "Verifying firewall configuration..." "Info"

    Write-Host ""
    Write-Host "Port Status:" -ForegroundColor Cyan

    # Check if ports are listening (when server is running)
    $portTests = @(
        @{ Port = 3000; Name = "HTTP/Socket.IO"; Protocol = "TCP" }
        @{ Port = 41234; Name = "Discovery"; Protocol = "UDP" }
        @{ Port = 5353; Name = "mDNS"; Protocol = "UDP" }
    )

    foreach ($test in $portTests) {
        $listening = Get-NetTCPConnection -LocalPort $test.Port -State Listen -ErrorAction SilentlyContinue
        if ($listening) {
            Write-Status "  ✓ Port $($test.Port) ($($test.Name)) - Listening" "Success"
        }
        else {
            Write-Status "  · Port $($test.Port) ($($test.Name)) - Not listening (server may not be running)" "Info"
        }
    }

    Write-Host ""
}

# ============================================================================
# Main Execution
# ============================================================================

function Main {
    Write-Host ""
    Write-Host "╔════════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host "║  CommClient-Server Windows Firewall Configuration             ║" -ForegroundColor Cyan
    Write-Host "╚════════════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
    Write-Host ""

    # Check admin privileges
    if (-not (Test-AdminPrivileges)) {
        Write-Status "This script requires administrator privileges!" "Error"
        Write-Status "Please run PowerShell as Administrator and try again." "Error"
        exit 1
    }

    # Determine action
    $action = if ($Remove) { "Remove" } elseif ($Disable) { "Disable" } elseif ($ListRules) { "List" } else { "Enable" }

    Write-Status "Action: $action" "Info"
    Write-Status "Profile(s): $ProfileType" "Info"
    Write-Host ""

    # Execute action
    switch ($action) {
        "Enable" {
            Enable-FirewallRules
            Show-FirewallRules
            Verify-Connectivity
        }
        "Disable" {
            Disable-FirewallRules
            Show-FirewallRules
        }
        "Remove" {
            Remove-FirewallRules
            Write-Status "All CommClient firewall rules removed" "Warning"
        }
        "List" {
            Show-FirewallRules
            Verify-Connectivity
        }
    }

    Write-Host ""
    Write-Host "╔════════════════════════════════════════════════════════════════╗" -ForegroundColor Green
    Write-Host "║  Configuration Complete!                                       ║" -ForegroundColor Green
    Write-Host "╚════════════════════════════════════════════════════════════════╝" -ForegroundColor Green
    Write-Host ""

    Write-Host "Port Reference:" -ForegroundColor Cyan
    Write-Host "  3000      - HTTP and Socket.IO server (main endpoint)"
    Write-Host "  41234     - Discovery broadcast (device discovery)"
    Write-Host "  40000-49999 - Mediasoup RTP (media streams)"
    Write-Host "  5353      - mDNS (network discovery)"
    Write-Host ""

    if ($Remove) {
        Write-Status "Firewall rules have been removed. Client connectivity may be affected." "Warning"
    }
    elseif ($Disable) {
        Write-Status "Firewall rules have been disabled. Client connectivity may be affected." "Warning"
    }
    else {
        Write-Status "Firewall rules are ready for CommClient-Server operation." "Success"
    }

    Write-Host ""
}

Main
