# Claude full-computer access launcher
$ErrorActionPreference = 'SilentlyContinue'
$ConfirmPreference     = 'None'
Set-ExecutionPolicy -Scope Process Bypass -Force

# Ensure claude is in PATH for this session
$localBin = 'C:\Users\youse\.local\bin'
if (Test-Path $localBin) {
    if ($env:Path -notlike "*$localBin*") { $env:Path = "$env:Path;$localBin" }
}

$env:ANTHROPIC_MODEL = 'claude-opus-4-7'

# Run from C:\ root with access to entire drive
Set-Location C:\

claude --model claude-opus-4-7 --effort xhigh --dangerously-skip-permissions --add-dir C:\
