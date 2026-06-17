param(
  [ValidateSet("Status", "Start", "Stop", "Restart", "Uninstall")]
  [string]$Action = "Status",
  [string]$TaskName = "AIALRA Localizer Unified Worker",
  [switch]$Json
)

$ErrorActionPreference = "Stop"

function Get-TaskSnapshot {
  param([Parameter(Mandatory=$true)][string]$Name)

  try {
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
  } catch {
    return [ordered]@{
      installed = $false
      task_name = $Name
      state = "missing"
      last_run_time = $null
      next_run_time = $null
      last_task_result = $null
      message = "Scheduled task is not installed. Run install_worker_task.ps1 first."
    }
  }

  $info = $null
  try {
    $info = Get-ScheduledTaskInfo -TaskName $Name -ErrorAction Stop
  } catch {
    $info = $null
  }

  return [ordered]@{
    installed = $true
    task_name = $Name
    state = [string]$task.State
    last_run_time = if ($info) { $info.LastRunTime } else { $null }
    next_run_time = if ($info) { $info.NextRunTime } else { $null }
    last_task_result = if ($info) { $info.LastTaskResult } else { $null }
    actions_redacted = $true
    message = "Task command arguments are intentionally omitted to avoid leaking local paths or environment-derived secrets."
  }
}

function Write-TaskResult {
  param(
    [Parameter(Mandatory=$true)][hashtable]$Result,
    [switch]$AsJson
  )

  if ($AsJson) {
    $Result | ConvertTo-Json -Depth 6
    return
  }

  Write-Host "ok: $($Result.ok)"
  Write-Host "action: $($Result.action)"
  Write-Host "task: $($Result.task.task_name)"
  Write-Host "installed: $($Result.task.installed)"
  Write-Host "state: $($Result.task.state)"
  if ($Result.task.last_task_result -ne $null) {
    Write-Host "last_task_result: $($Result.task.last_task_result)"
  }
  if ($Result.message) {
    Write-Host "message: $($Result.message)"
  }
}

function Assert-TaskInstalled {
  param([Parameter(Mandatory=$true)][string]$Name)

  $snapshot = Get-TaskSnapshot -Name $Name
  if (-not $snapshot.installed) {
    throw "Scheduled task is not installed: $Name. Run install_worker_task.ps1 first."
  }
}

$result = [ordered]@{
  ok = $true
  action = $Action
  task = $null
  message = ""
}

switch ($Action) {
  "Status" {
    $result.task = Get-TaskSnapshot -Name $TaskName
    $result.ok = [bool]$result.task.installed
  }
  "Start" {
    Assert-TaskInstalled -Name $TaskName
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Milliseconds 300
    $result.task = Get-TaskSnapshot -Name $TaskName
    $result.message = "Start requested."
  }
  "Stop" {
    Assert-TaskInstalled -Name $TaskName
    Stop-ScheduledTask -TaskName $TaskName
    Start-Sleep -Milliseconds 300
    $result.task = Get-TaskSnapshot -Name $TaskName
    $result.message = "Stop requested."
  }
  "Restart" {
    Assert-TaskInstalled -Name $TaskName
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Milliseconds 300
    $result.task = Get-TaskSnapshot -Name $TaskName
    $result.message = "Restart requested."
  }
  "Uninstall" {
    Assert-TaskInstalled -Name $TaskName
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    $result.task = Get-TaskSnapshot -Name $TaskName
    $result.message = "Task uninstalled."
  }
}

Write-TaskResult -Result $result -AsJson:$Json
if (-not $result.ok) { exit 1 }
