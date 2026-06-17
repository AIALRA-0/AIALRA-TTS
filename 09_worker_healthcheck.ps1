param(
  [string]$RemoteBaseUrl = $env:REMOTE_PUBLIC_BASE_URL,
  [string]$WorkerToken = $env:WORKER_SHARED_TOKEN,
  [string]$WorkerId = "local-windows-worker",
  [switch]$SkipRemote,
  [switch]$Json
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Py)) { & (Join-Path $ProjectRoot "setup.ps1") }

$argsList = @(
  "-m", "ecse_localizer",
  "--config", (Join-Path $ProjectRoot "config.yaml"),
  "worker-health",
  "--worker-id", $WorkerId
)

if ($SkipRemote) {
  $argsList += "--skip-remote"
} elseif ($RemoteBaseUrl -and $WorkerToken) {
  $env:REMOTE_PUBLIC_BASE_URL = $RemoteBaseUrl
  $env:WORKER_SHARED_TOKEN = $WorkerToken
} else {
  Write-Warning "Remote heartbeat check skipped. Set REMOTE_PUBLIC_BASE_URL and WORKER_SHARED_TOKEN, or pass -SkipRemote explicitly."
  $argsList += "--skip-remote"
}

if ($Json) { $argsList += "--json" }

& $Py @argsList
exit $LASTEXITCODE
