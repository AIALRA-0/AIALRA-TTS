param(
  [string]$TaskName = "AIALRA Localizer Worker Heartbeat",
  [string]$RemoteBaseUrl = $env:REMOTE_PUBLIC_BASE_URL,
  [string]$WorkerToken = $env:WORKER_SHARED_TOKEN,
  [int]$IntervalMinutes = 1
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $RemoteBaseUrl) { throw "RemoteBaseUrl is required." }
if (-not $WorkerToken) { throw "WorkerToken is required." }

$script = Join-Path $ProjectRoot "06_worker_heartbeat.ps1"
$encodedArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -RemoteBaseUrl `"$RemoteBaseUrl`" -WorkerToken `"$WorkerToken`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $encodedArgs -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes ([Math]::Max(1, $IntervalMinutes)))
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Installed scheduled task: $TaskName"
