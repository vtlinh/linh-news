# Local smart-cron entry point -- runs the generation pipeline on THIS machine
# (not on the Fly server) so we don't have to worry about the small Fly VM
# OOMing during WeasyPrint. Designed to be fired every 6 hours by Windows
# Task Scheduler. There is no server-side cron endpoint -- generation only
# runs here.
#
# The Python CLI (`app.generate --smart`) inspects each enabled user's state
# for today and decides per-user whether to skip, run a full pipeline, or
# retry the post-LLM steps with the cached LLM output.
#
# Usage:
#   powershell.exe -File scripts\trigger_cron.ps1
#   powershell.exe -File scripts\trigger_cron.ps1 -Slot evening

param(
    [ValidateSet('morning', 'evening')]
    [string]$Slot = 'morning',
    [string]$RepoRoot = 'C:\Users\Linh\Workspace\linh-news',
    [string]$DbAppName = 'linh-news-db',
    [int]$ProxyLocalPort = 15432
)

$ErrorActionPreference = 'Stop'

function Test-PortListening {
    param([int]$Port)
    $listeners = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    return [bool]$listeners
}

# 1. Ensure Fly Postgres proxy is up on localhost:$ProxyLocalPort.
if (-not (Test-PortListening -Port $ProxyLocalPort)) {
    Write-Host "fly proxy not listening on $ProxyLocalPort -- starting"
    Start-Process -FilePath 'fly' `
        -ArgumentList "proxy", "$ProxyLocalPort`:5432", "-a", $DbAppName `
        -WindowStyle Hidden
    # Wait for the proxy to bind, up to ~15s
    $deadline = (Get-Date).AddSeconds(15)
    while (-not (Test-PortListening -Port $ProxyLocalPort) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
    }
    if (-not (Test-PortListening -Port $ProxyLocalPort)) {
        Write-Error "fly proxy did not come up on $ProxyLocalPort within 15s"
        exit 1
    }
}

# 2. Run the smart cron -- Python CLI handles per-user gating + logging.
# WeasyPrint loads its GTK/Pango/Cairo DLLs from the MSYS2 UCRT64 install;
# `app.pdf` registers the directory via `os.add_dll_directory` at import time
# (override with WEASYPRINT_DLL_DIR if the libs live elsewhere).
Set-Location $RepoRoot
& uv run python -m app.generate $Slot --smart
$code = $LASTEXITCODE
Write-Host "app.generate exited with $code"
exit $code
