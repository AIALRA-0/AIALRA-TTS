param(
  [string]$TaskName = "AIALRA Localizer Unified Worker",
  [string]$RemoteBaseUrl = $env:REMOTE_PUBLIC_BASE_URL,
  [string]$WorkerToken = $env:WORKER_SHARED_TOKEN,
  [string]$WorkerId = "local-windows-worker",
  [string]$Config = "",
  [int]$IntervalSeconds = 15,
  [int]$HeartbeatIntervalSeconds = 60,
  [int]$MaxConcurrentJobs = 1,
  [switch]$StoreUserEnvironment
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Config) { $Config = Join-Path $ProjectRoot "config.yaml" }
if (-not (Test-Path -LiteralPath $Config)) {
  throw "Config file not found: $Config. Create local config.yaml from config.example.yaml first."
}
if (-not $RemoteBaseUrl) {
  throw "RemoteBaseUrl is required. Set REMOTE_PUBLIC_BASE_URL or pass -RemoteBaseUrl."
}
if (-not $WorkerToken) {
  throw "WorkerToken is required. Set WORKER_SHARED_TOKEN or pass -WorkerToken."
}

function Test-PersistentEnvValue {
  param(
    [Parameter(Mandatory=$true)][string]$Name,
    [Parameter(Mandatory=$true)][string]$Expected
  )
  $userValue = [Environment]::GetEnvironmentVariable($Name, "User")
  $machineValue = [Environment]::GetEnvironmentVariable($Name, "Machine")
  return (($userValue -and $userValue -eq $Expected) -or ($machineValue -and $machineValue -eq $Expected))
}

if ($StoreUserEnvironment) {
  [Environment]::SetEnvironmentVariable("REMOTE_PUBLIC_BASE_URL", $RemoteBaseUrl, "User")
  [Environment]::SetEnvironmentVariable("WORKER_SHARED_TOKEN", $WorkerToken, "User")
  Write-Host "Stored REMOTE_PUBLIC_BASE_URL and WORKER_SHARED_TOKEN in the current user's environment."
} else {
  if (-not (Test-PersistentEnvValue -Name "REMOTE_PUBLIC_BASE_URL" -Expected $RemoteBaseUrl)) {
    throw "REMOTE_PUBLIC_BASE_URL is not set in the persistent User/Machine environment. Rerun with -StoreUserEnvironment or set it before installing the scheduled task."
  }
  if (-not (Test-PersistentEnvValue -Name "WORKER_SHARED_TOKEN" -Expected $WorkerToken)) {
    throw "WORKER_SHARED_TOKEN is not set in the persistent User/Machine environment. Rerun with -StoreUserEnvironment or set it before installing the scheduled task."
  }
}

$script = Join-Path $ProjectRoot "13_start_worker.ps1"
$encodedArgs = @(
  "-NoProfile",
  "-ExecutionPolicy", "Bypass",
  "-File", "`"$script`"",
  "-Config", "`"$Config`"",
  "-WorkerId", "`"$WorkerId`"",
  "-IntervalSeconds", $IntervalSeconds,
  "-HeartbeatIntervalSeconds", $HeartbeatIntervalSeconds,
  "-MaxConcurrentJobs", $MaxConcurrentJobs
) -join " "

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $encodedArgs -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName"
Write-Host "The task reads REMOTE_PUBLIC_BASE_URL and WORKER_SHARED_TOKEN from the persistent User/Machine environment at runtime."
Write-Host "The worker token is not embedded in the scheduled task command line."
