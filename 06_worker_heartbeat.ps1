param(
  [string]$RemoteBaseUrl = $env:REMOTE_PUBLIC_BASE_URL,
  [string]$WorkerToken = $env:WORKER_SHARED_TOKEN,
  [string]$WorkerId = "local-windows-worker",
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

$argsList = @(
  "-m", "ecse_localizer",
  "--config", (Join-Path $ProjectRoot "config.yaml"),
  "worker",
  "--remote-base-url", $RemoteBaseUrl,
  "--worker-token", $WorkerToken,
  "--worker-id", $WorkerId,
  "--heartbeat-interval-seconds", [string]$IntervalSeconds,
  "--heartbeat-only"
)
if (-not $Loop) { $argsList += "--once" }

& $Py @argsList
exit $LASTEXITCODE
