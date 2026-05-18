# ===== Claude Auto-Runner =====
$ErrorActionPreference = 'SilentlyContinue'
$ConfirmPreference     = 'None'
$ProgressPreference    = 'SilentlyContinue'
Set-ExecutionPolicy -Scope Process Bypass -Force

Set-Location C:\Users\youse\c
$env:ANTHROPIC_MODEL = 'claude-opus-4-7'

$logDir = 'C:\Users\youse\c\logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
$log = Join-Path $logDir ('claude-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')

Write-Host '====================================================' -ForegroundColor Cyan
Write-Host ' Claude Auto-Runner | Admin | xhigh | 1000h'          -ForegroundColor Cyan
Write-Host (' Dir:   ' + (Get-Location))                          -ForegroundColor Cyan
Write-Host (' Log:   ' + $log)                                    -ForegroundColor Cyan
Write-Host '====================================================' -ForegroundColor Cyan

$end = (Get-Date).AddHours(1000)
$i   = 0
$b   = 5

while ((Get-Date) -lt $end -and $i -lt 50000) {
    $i++
    $hdr = "`n===== RUN $i @ $(Get-Date -Format 'HH:mm:ss') ====="
    Write-Host $hdr -ForegroundColor Cyan
    Add-Content $log $hdr

    $t = Get-Date
    $flags = @('--model','claude-opus-4-7','--effort','xhigh','--dangerously-skip-permissions')
    if ($i -gt 1) { $flags += '--continue' }

    & claude @flags 2>&1 | Tee-Object -FilePath $log -Append
    $code = $LASTEXITCODE
    $dur  = ((Get-Date) - $t).TotalSeconds

    if ($code -eq 0) {
        Write-Host 'COMPLETED' -ForegroundColor Green
        break
    }

    if ($dur -lt 5) { $b = [Math]::Min($b * 2, 300) } else { $b = 5 }
    Write-Host ("Exit=$code Dur=$([int]$dur)s. Retry in $b s") -ForegroundColor Yellow
    Start-Sleep $b
}

Write-Host "`n=== Session done. Runs=$i ===" -ForegroundColor Cyan
