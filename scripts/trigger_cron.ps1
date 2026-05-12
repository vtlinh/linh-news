# Trigger the Linh News daily edition from this machine.
#
# Usage:
#   $env:CRON_SECRET = "<secret>"
#   powershell.exe -File scripts\trigger_cron.ps1                 # morning (default)
#   powershell.exe -File scripts\trigger_cron.ps1 -Slot evening
#
# Schedule it via Windows Task Scheduler so it fires once a day
# (e.g. 6:00 AM America/New_York). The script POSTs to /cron/{slot}
# with the shared CRON_SECRET; the Fly server runs the generation.

param(
    [ValidateSet('morning', 'evening')]
    [string]$Slot = 'morning',
    [string]$BaseUrl = 'https://linh-news.fly.dev'
)

$ErrorActionPreference = 'Stop'

$secret = $env:CRON_SECRET
if (-not $secret) {
    Write-Error "CRON_SECRET env var is not set"
    exit 1
}

$url = "$BaseUrl/cron/$Slot"
Write-Host "POST $url"

try {
    $response = Invoke-WebRequest -Method Post -Uri $url `
        -Headers @{ 'X-Cron-Token' = $secret } `
        -UseBasicParsing
    Write-Host "HTTP $($response.StatusCode)"
    Write-Host $response.Content
    if ($response.StatusCode -ne 202) { exit 1 }
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
