param(
  [string]$RemoteBaseUrl = $env:REMOTE_PUBLIC_BASE_URL,
  [string]$WorkerToken = $env:WORKER_SHARED_TOKEN,
  [int]$IntervalSeconds = 30,
  [switch]$Loop
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Py)) { & (Join-Path $ProjectRoot "setup.ps1") }

if (-not $RemoteBaseUrl) {
  throw "RemoteBaseUrl is required. Set REMOTE_PUBLIC_BASE_URL or pass -RemoteBaseUrl."
}
if (-not $WorkerToken) {
  throw "WorkerToken is required. Set WORKER_SHARED_TOKEN or pass -WorkerToken."
}

function Send-Heartbeat {
  $statusJson = & $Py -m ecse_localizer --config (Join-Path $ProjectRoot "config.yaml") worker-status
  if ($LASTEXITCODE -ne 0) { throw "worker-status failed with exit code $LASTEXITCODE" }
  $uri = ($RemoteBaseUrl.TrimEnd('/')) + "/api/worker/heartbeat"
  Invoke-RestMethod -Method Post -Uri $uri -Headers @{ "X-Worker-Token" = $WorkerToken } -Body $statusJson -ContentType "application/json" | Out-Null
  Write-Host ("Heartbeat sent to {0} at {1}" -f $uri, (Get-Date).ToString("s"))
}

if ($Loop) {
  while ($true) {
    try {
      Send-Heartbeat
    } catch {
      Write-Warning $_
    }
    Start-Sleep -Seconds ([Math]::Max(5, $IntervalSeconds))
  }
} else {
  Send-Heartbeat
}
