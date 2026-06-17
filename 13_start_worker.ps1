param(
  [string]$RemoteBaseUrl = $env:REMOTE_PUBLIC_BASE_URL,
  [string]$WorkerToken = $env:WORKER_SHARED_TOKEN,
  [string]$WorkerId = "local-windows-worker",
  [string]$Config = "",
  [int]$IntervalSeconds = 15,
  [int]$HeartbeatIntervalSeconds = 60,
  [int]$MaxConcurrentJobs = 1,
  [switch]$Once,
  [switch]$DryRun,
  [switch]$NoHeartbeat,
  [switch]$HeartbeatOnly,
  [switch]$LocalCheck
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Py)) { & (Join-Path $ProjectRoot "setup.ps1") }
if (-not $Config) { $Config = Join-Path $ProjectRoot "config.yaml" }
if (-not (Test-Path -LiteralPath $Config)) {
  throw "Config file not found: $Config. Create local config.yaml from config.example.yaml first."
}

$argsList = @(
  "-m", "ecse_localizer",
  "--config", $Config,
  "worker",
  "--worker-id", $WorkerId,
  "--interval-seconds", [string]$IntervalSeconds,
  "--heartbeat-interval-seconds", [string]$HeartbeatIntervalSeconds,
  "--max-concurrent-jobs", [string]$MaxConcurrentJobs
)

if ($LocalCheck) {
  $argsList += "--local-check"
} else {
  if (-not $RemoteBaseUrl) {
    throw "RemoteBaseUrl is required. Set REMOTE_PUBLIC_BASE_URL or pass -RemoteBaseUrl."
  }
  if (-not $WorkerToken) {
    throw "WorkerToken is required. Set WORKER_SHARED_TOKEN or pass -WorkerToken."
  }
  $env:REMOTE_PUBLIC_BASE_URL = $RemoteBaseUrl
  $env:WORKER_SHARED_TOKEN = $WorkerToken
}

if ($Once) { $argsList += "--once" }
if ($DryRun) { $argsList += "--dry-run" }
if ($NoHeartbeat) { $argsList += "--no-heartbeat" }
if ($HeartbeatOnly) { $argsList += "--heartbeat-only" }

& $Py @argsList
exit $LASTEXITCODE
