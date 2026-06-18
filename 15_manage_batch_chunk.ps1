param(
  [ValidateSet("Start", "Status", "Stop")]
  [string]$Action = "Status",
  [string]$InputDir = (Split-Path -Parent $PSScriptRoot),
  [int]$Limit = 1,
  [switch]$ShortestFirst,
  [switch]$Force,
  [switch]$AllowParallel,
  [int]$TailLines = 80,
  [switch]$Json
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunsDir = Join-Path $ProjectRoot "runs\batch_background"
$LogsDir = Join-Path $ProjectRoot "logs"
$ProcessAll = Join-Path $ProjectRoot "03_process_all.ps1"
$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

New-Item -ItemType Directory -Force -Path $RunsDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

function Escape-PowerShellSingleQuoted([string]$Value) {
  return $Value.Replace("'", "''")
}

function Read-JsonFile([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) { return $null }
  return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Write-JsonFile($Object, [string]$Path) {
  $Object | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Get-StateFiles {
  Get-ChildItem -LiteralPath $RunsDir -Filter "batch_chunk_*.json" -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notmatch "_done\.json$" -and $_.Name -notmatch "_stop\.json$" } |
    Sort-Object LastWriteTime -Descending
}

function Get-LatestState {
  $stateFile = Get-StateFiles | Select-Object -First 1
  if (-not $stateFile) { return $null }
  $state = Read-JsonFile $stateFile.FullName
  if (-not $state) { return $null }
  $state | Add-Member -NotePropertyName state_path -NotePropertyValue $stateFile.FullName -Force
  return $state
}

function Test-StateRunning($State) {
  if (-not $State) { return $false }
  if ($State.done_marker -and (Test-Path -LiteralPath $State.done_marker)) { return $false }
  if (-not $State.pid) { return $false }
  $proc = Get-Process -Id ([int]$State.pid) -ErrorAction SilentlyContinue
  return [bool]$proc
}

function Get-LogTail([string]$Path, [int]$Lines) {
  if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return @() }
  return @(Get-Content -LiteralPath $Path -Tail $Lines -Encoding UTF8 -ErrorAction SilentlyContinue)
}

function Get-ChecklistSummary {
  if (-not (Test-Path -LiteralPath $Py)) { return $null }
  $raw = & $Py -m ecse_localizer progress-checklist --json 2>$null
  if ($LASTEXITCODE -ne 0 -or -not $raw) { return $null }
  try {
    return ($raw | Out-String | ConvertFrom-Json)
  } catch {
    return $null
  }
}

function Build-StatusPayload {
  $state = Get-LatestState
  $done = $null
  $stop = $null
  $running = Test-StateRunning $state
  if ($state -and $state.done_marker) { $done = Read-JsonFile $state.done_marker }
  if ($state -and $state.stop_marker) { $stop = Read-JsonFile $state.stop_marker }
  $checklist = Get-ChecklistSummary
  $status = "not_started"
  if ($state) {
    if ($done) {
      if ([int]$done.exit_code -eq 0) { $status = "completed" } else { $status = "failed" }
    } elseif ($running) {
      $status = "running"
    } elseif ($stop) {
      $status = "stop_requested"
    } else {
      $status = "not_running_no_done_marker"
    }
  }

  $payload = [ordered]@{
    status = $status
    running = $running
    run_id = if ($state) { $state.run_id } else { "" }
    pid = if ($state) { $state.pid } else { $null }
    started_at = if ($state) { $state.started_at } else { "" }
    completed_at = if ($done) { $done.completed_at } else { "" }
    exit_code = if ($done) { $done.exit_code } else { $null }
    limit = if ($state) { $state.limit } else { 0 }
    shortest_first = if ($state) { [bool]$state.shortest_first } else { $false }
    state_path = if ($state) { $state.state_path } else { "" }
    stdout_log = if ($state) { $state.stdout_log } else { "" }
    stderr_log = if ($state) { $state.stderr_log } else { "" }
    stdout_tail = @(if ($state) { Get-LogTail $state.stdout_log $TailLines })
    stderr_tail = @(if ($state) { Get-LogTail $state.stderr_log $TailLines })
    checklist = if ($checklist) {
      [ordered]@{
        summary = $checklist.summary
        batch_readiness = $checklist.batch_readiness
        latest_batch_process = $checklist.latest_batch_process
        latest_batch_background = $checklist.latest_batch_background
      }
    } else { $null }
  }
  return $payload
}

if ($Action -eq "Start") {
  $latest = Get-LatestState
  if ((Test-StateRunning $latest) -and -not $AllowParallel) {
    $payload = Build-StatusPayload
    if ($Json) {
      $payload | ConvertTo-Json -Depth 8
    } else {
      Write-Host "A batch chunk is already running: $($payload.run_id) pid=$($payload.pid)"
      Write-Host "Use -Action Status to inspect it, or -AllowParallel if you intentionally want another chunk."
    }
    exit 3
  }

  if (-not (Test-Path -LiteralPath $ProcessAll)) {
    throw "Missing process-all script: $ProcessAll"
  }
  if (-not (Test-Path -LiteralPath $Py)) {
    & (Join-Path $ProjectRoot "setup.ps1")
  }

  $RunId = "batch_chunk_{0}" -f (Get-Date -Format "yyyyMMdd_HHmmss")
  $StatePath = Join-Path $RunsDir "$RunId.json"
  $RunnerPath = Join-Path $RunsDir "$RunId.runner.ps1"
  $DonePath = Join-Path $RunsDir "$RunId`_done.json"
  $StopPath = Join-Path $RunsDir "$RunId`_stop.json"
  $StdoutLog = Join-Path $LogsDir "$RunId.out.log"
  $StderrLog = Join-Path $LogsDir "$RunId.err.log"

  $argLines = @(
    "-InputDir '$((Escape-PowerShellSingleQuoted $InputDir))'"
  )
  if ($Limit -gt 0) { $argLines += "-Limit $Limit" }
  if ($ShortestFirst) { $argLines += "-ShortestFirst" }
  if ($Force) { $argLines += "-Force" }
  $processArgs = $argLines -join " "

  $runner = @"
`$ErrorActionPreference = 'Stop'
try {
  Set-Location -LiteralPath '$((Escape-PowerShellSingleQuoted $ProjectRoot))'
  & '$((Escape-PowerShellSingleQuoted $ProcessAll))' $processArgs
  `$exitCode = `$LASTEXITCODE
} catch {
  Write-Error `$_.Exception.Message
  `$exitCode = 1
}
`$done = [ordered]@{
  run_id = '$RunId'
  exit_code = `$exitCode
  completed_at = (Get-Date).ToString('s')
}
`$done | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath '$((Escape-PowerShellSingleQuoted $DonePath))' -Encoding UTF8
exit `$exitCode
"@
  Set-Content -LiteralPath $RunnerPath -Value $runner -Encoding UTF8

  $proc = Start-Process -FilePath "powershell.exe" `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $RunnerPath) `
    -WindowStyle Hidden `
    -RedirectStandardOutput $StdoutLog `
    -RedirectStandardError $StderrLog `
    -PassThru

  $state = [ordered]@{
    kind = "batch_chunk"
    run_id = $RunId
    status = "started"
    pid = $proc.Id
    started_at = (Get-Date).ToString("s")
    input_dir = $InputDir
    limit = $Limit
    shortest_first = [bool]$ShortestFirst
    force = [bool]$Force
    process_all = $ProcessAll
    runner = $RunnerPath
    stdout_log = $StdoutLog
    stderr_log = $StderrLog
    done_marker = $DonePath
    stop_marker = $StopPath
  }
  Write-JsonFile $state $StatePath
  Start-Sleep -Milliseconds 500
  $payload = Build-StatusPayload
  if ($Json) {
    $payload | ConvertTo-Json -Depth 8
  } else {
    Write-Host "Started batch chunk: $RunId pid=$($proc.Id)"
    Write-Host "State: $StatePath"
    Write-Host "Logs:  $StdoutLog"
    Write-Host "       $StderrLog"
    Write-Host "Check: .\15_manage_batch_chunk.ps1 -Action Status"
  }
  exit 0
}

if ($Action -eq "Stop") {
  $state = Get-LatestState
  if (-not $state) {
    if ($Json) { @{ status = "not_started" } | ConvertTo-Json -Depth 4 } else { Write-Host "No batch chunk state found." }
    exit 0
  }
  $running = Test-StateRunning $state
  $stopRecord = [ordered]@{
    run_id = $state.run_id
    stop_requested_at = (Get-Date).ToString("s")
    pid = $state.pid
  }
  Write-JsonFile $stopRecord $state.stop_marker
  if ($running) {
    Stop-Process -Id ([int]$state.pid) -Force
    Start-Sleep -Milliseconds 500
  }
  $payload = Build-StatusPayload
  if ($Json) {
    $payload | ConvertTo-Json -Depth 8
  } else {
    Write-Host "Stop requested for $($state.run_id)."
    Write-Host "Status: $($payload.status)"
  }
  exit 0
}

$payload = Build-StatusPayload
if ($Json) {
  $payload | ConvertTo-Json -Depth 8
} else {
  Write-Host "Batch chunk status: $($payload.status)"
  if ($payload.run_id) { Write-Host "Run: $($payload.run_id) pid=$($payload.pid) limit=$($payload.limit) shortest_first=$($payload.shortest_first)" }
  if ($payload.state_path) { Write-Host "State: $($payload.state_path)" }
  if ($payload.stdout_log) { Write-Host "Stdout log: $($payload.stdout_log)" }
  if ($payload.stderr_log) { Write-Host "Stderr log: $($payload.stderr_log)" }
  if ($payload.checklist) {
    $readiness = $payload.checklist.batch_readiness
    Write-Host "Batch readiness: $($readiness.completed_count)/$($readiness.video_count) complete; pending $($readiness.pending_count)"
  }
  if ($payload.stdout_tail.Count -gt 0) {
    Write-Host ""
    Write-Host "--- stdout tail ---"
    $payload.stdout_tail | ForEach-Object { Write-Host $_ }
  }
  if ($payload.stderr_tail.Count -gt 0) {
    Write-Host ""
    Write-Host "--- stderr tail ---"
    $payload.stderr_tail | ForEach-Object { Write-Host $_ }
  }
}
exit 0
