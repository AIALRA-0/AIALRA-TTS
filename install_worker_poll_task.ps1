param(
  [string]$TaskName = "AIALRA Localizer Worker Poll",
  [string]$RemoteBaseUrl = $env:REMOTE_PUBLIC_BASE_URL,
  [string]$WorkerToken = $env:WORKER_SHARED_TOKEN,
  [string]$WorkerId = "local-windows-worker",
  [int]$IntervalSeconds = 15,
  [int]$MaxConcurrentJobs = 1
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $RemoteBaseUrl) { throw "RemoteBaseUrl is required." }
if (-not $WorkerToken) { throw "WorkerToken is required." }

$script = Join-Path $ProjectRoot "07_worker_poll.ps1"
$encodedArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -RemoteBaseUrl `"$RemoteBaseUrl`" -WorkerToken `"$WorkerToken`" -WorkerId `"$WorkerId`" -IntervalSeconds $IntervalSeconds -MaxConcurrentJobs $MaxConcurrentJobs"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $encodedArgs -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Installed scheduled task: $TaskName"
