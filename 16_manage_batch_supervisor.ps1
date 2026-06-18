param(
  [ValidateSet("Start", "Status", "Stop")]
  [string]$Action = "Status",
  [string]$InputDir = (Split-Path -Parent $PSScriptRoot),
  [int]$Limit = 1,
  [switch]$ShortestFirst,
  [int]$MaxChunks = 0,
  [int]$PollSeconds = 60,
  [int]$TailLines = 80,
  [switch]$AllowParallel,
  [switch]$StopActiveChunk,
  [switch]$Json
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunsDir = Join-Path $ProjectRoot "runs\batch_supervisor"
$LogsDir = Join-Path $ProjectRoot "logs"
$ChunkManager = Join-Path $ProjectRoot "15_manage_batch_chunk.ps1"
$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

New-Item -ItemType Directory -Force -Path $RunsDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

function Escape-Single([string]$Value) {
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
  Get-ChildItem -LiteralPath $RunsDir -Filter "batch_supervisor_*.json" -File -ErrorAction SilentlyContinue |
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
  return [bool](Get-Process -Id ([int]$State.pid) -ErrorAction SilentlyContinue)
}

function Get-LogTail([string]$Path, [int]$Lines) {
  if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return @() }
  return @(Get-Content -LiteralPath $Path -Tail $Lines -Encoding UTF8 -ErrorAction SilentlyContinue)
}

function Build-StatusPayload {
  $state = Get-LatestState
  $done = $null
  $stop = $null
  if ($state -and $state.done_marker) { $done = Read-JsonFile $state.done_marker }
  if ($state -and $state.stop_marker) { $stop = Read-JsonFile $state.stop_marker }
  $running = Test-StateRunning $state
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
  return [ordered]@{
    status = $status
    running = $running
    run_id = if ($state) { $state.run_id } else { "" }
    pid = if ($state) { $state.pid } else { $null }
    started_at = if ($state) { $state.started_at } else { "" }
    completed_at = if ($done) { $done.completed_at } else { "" }
    exit_code = if ($done) { $done.exit_code } else { $null }
    chunks_started = if ($done) { $done.chunks_started } elseif ($state) { $state.chunks_started } else { 0 }
    max_chunks = if ($state) { $state.max_chunks } else { 0 }
    limit = if ($state) { $state.limit } else { 0 }
    shortest_first = if ($state) { [bool]$state.shortest_first } else { $false }
    state_path = if ($state) { $state.state_path } else { "" }
    stdout_log = if ($state) { $state.stdout_log } else { "" }
    stderr_log = if ($state) { $state.stderr_log } else { "" }
    stdout_tail = @(if ($state) { Get-LogTail $state.stdout_log $TailLines })
    stderr_tail = @(if ($state) { Get-LogTail $state.stderr_log $TailLines })
  }
}

function Convert-ToSupervisorJson($Payload) {
  $safe = [ordered]@{
    status = [string]$Payload.status
    running = [bool]$Payload.running
    run_id = [string]$Payload.run_id
    pid = $Payload.pid
    started_at = [string]$Payload.started_at
    completed_at = [string]$Payload.completed_at
    exit_code = $Payload.exit_code
    chunks_started = [int]$Payload.chunks_started
    max_chunks = [int]$Payload.max_chunks
    limit = [int]$Payload.limit
    shortest_first = [bool]$Payload.shortest_first
    state_path = [string]$Payload.state_path
    stdout_log = [string]$Payload.stdout_log
    stderr_log = [string]$Payload.stderr_log
  }
  return ($safe | ConvertTo-Json -Depth 4)
}

if ($Action -eq "Start") {
  $latest = Get-LatestState
  if ((Test-StateRunning $latest) -and -not $AllowParallel) {
    $payload = Build-StatusPayload
    if ($Json) { Write-Output (Convert-ToSupervisorJson $payload) } else { Write-Host "Batch supervisor is already running: $($payload.run_id) pid=$($payload.pid)" }
    exit 3
  }
  if (-not (Test-Path -LiteralPath $ChunkManager)) {
    throw "Missing batch chunk manager: $ChunkManager"
  }
  if (-not (Test-Path -LiteralPath $Py)) {
    & (Join-Path $ProjectRoot "setup.ps1")
  }

  $RunId = "batch_supervisor_{0}" -f (Get-Date -Format "yyyyMMdd_HHmmss")
  $StatePath = Join-Path $RunsDir "$RunId.json"
  $RunnerPath = Join-Path $RunsDir "$RunId.runner.ps1"
  $DonePath = Join-Path $RunsDir "$RunId`_done.json"
  $StopPath = Join-Path $RunsDir "$RunId`_stop.json"
  $StdoutLog = Join-Path $LogsDir "$RunId.out.log"
  $StderrLog = Join-Path $LogsDir "$RunId.err.log"
  $shortestArg = if ($ShortestFirst) { "-ShortestFirst" } else { "" }

  $runner = @"
`$ErrorActionPreference = 'Stop'
`$chunksStarted = 0
function Log([string]`$Message) {
  Write-Output ("{0} {1}" -f (Get-Date).ToString('s'), `$Message)
}
function ReadJson([string]`$Path) {
  if (-not (Test-Path -LiteralPath `$Path)) { return `$null }
  return Get-Content -LiteralPath `$Path -Raw -Encoding UTF8 | ConvertFrom-Json
}
function RunPythonJson([string[]]`$CliArgs) {
  `$raw = & '$((Escape-Single $Py))' @CliArgs
  if (`$LASTEXITCODE -ne 0 -or -not `$raw) { return `$null }
  return (`$raw | Out-String | ConvertFrom-Json)
}
try {
  Set-Location -LiteralPath '$((Escape-Single $ProjectRoot))'
  while (`$true) {
    if (Test-Path -LiteralPath '$((Escape-Single $StopPath))') {
      Log 'Stop marker detected; supervisor will not start more chunks.'
      break
    }
    if ($MaxChunks -gt 0 -and `$chunksStarted -ge $MaxChunks) {
      Log "MaxChunks reached: `$chunksStarted / $MaxChunks."
      break
    }
    `$checklist = RunPythonJson @('-m','ecse_localizer','progress-checklist','--json')
    `$pending = if (`$checklist -and `$checklist.batch_readiness) { [int]`$checklist.batch_readiness.pending_count } else { -1 }
    Log "Batch readiness pending=`$pending chunks_started=`$chunksStarted."
    if (`$pending -eq 0) {
      Log 'No pending videos remain.'
      break
    }

    `$statusRaw = & '$((Escape-Single $ChunkManager))' -Action Status -NoChecklist -Json
    `$status = `$statusRaw | Out-String | ConvertFrom-Json
    if (`$status.running) {
      Log "Existing chunk running: `$(`$status.run_id) progress=`$(`$status.progress.percent)% phase=`$(`$status.progress.phase)."
      Start-Sleep -Seconds $PollSeconds
      continue
    }
    if (`$status.status -eq 'failed') {
      Log "Latest chunk failed: `$(`$status.run_id). Supervisor stopping."
      throw "Latest chunk failed: `$(`$status.run_id)"
    }
    Log 'Starting next chunk.'
    `$startRaw = & '$((Escape-Single $ChunkManager))' -Action Start -InputDir '$((Escape-Single $InputDir))' -Limit $Limit $shortestArg -NoChecklist -Json
    `$started = `$startRaw | Out-String | ConvertFrom-Json
    if (-not `$started.running) {
      Log "Chunk did not enter running state: `$(`$started.status)."
      throw "Chunk did not start"
    }
    `$chunksStarted += 1
    Log "Started chunk `$(`$started.run_id) pid=`$(`$started.pid)."
    Start-Sleep -Seconds $PollSeconds
  }
  `$exitCode = 0
} catch {
  Write-Error `$_.Exception.Message
  `$exitCode = 1
}
`$done = [ordered]@{
  run_id = '$RunId'
  exit_code = `$exitCode
  chunks_started = `$chunksStarted
  completed_at = (Get-Date).ToString('s')
}
`$done | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath '$((Escape-Single $DonePath))' -Encoding UTF8
exit `$exitCode
"@
  Set-Content -LiteralPath $RunnerPath -Value $runner -Encoding UTF8
  $runnerArg = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $RunnerPath.Replace('"', '\"')
  $proc = Start-Process -FilePath "powershell.exe" `
    -ArgumentList $runnerArg `
    -WindowStyle Hidden `
    -RedirectStandardOutput $StdoutLog `
    -RedirectStandardError $StderrLog `
    -PassThru
  $state = [ordered]@{
    kind = "batch_supervisor"
    run_id = $RunId
    status = "started"
    pid = $proc.Id
    started_at = (Get-Date).ToString("s")
    input_dir = $InputDir
    limit = $Limit
    shortest_first = [bool]$ShortestFirst
    max_chunks = $MaxChunks
    poll_seconds = $PollSeconds
    chunks_started = 0
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
    Write-Output (Convert-ToSupervisorJson $payload)
  } else {
    Write-Host "Started batch supervisor: $RunId pid=$($proc.Id)"
    Write-Host "State: $StatePath"
    Write-Host "Logs:  $StdoutLog"
  }
  exit 0
}

if ($Action -eq "Stop") {
  $state = Get-LatestState
  if (-not $state) {
    if ($Json) { Write-Output (@{ status = "not_started" } | ConvertTo-Json -Depth 4) } else { Write-Host "No batch supervisor state found." }
    exit 0
  }
  $stopRecord = [ordered]@{
    run_id = $state.run_id
    stop_requested_at = (Get-Date).ToString("s")
    pid = $state.pid
    stop_active_chunk = [bool]$StopActiveChunk
  }
  Write-JsonFile $stopRecord $state.stop_marker
  if (Test-StateRunning $state) {
    Stop-Process -Id ([int]$state.pid) -Force -ErrorAction SilentlyContinue
  }
  if ($StopActiveChunk) {
    & $ChunkManager -Action Stop -NoChecklist | Out-Null
  }
  $payload = Build-StatusPayload
  if ($Json) {
    Write-Output (Convert-ToSupervisorJson $payload)
  } else {
    Write-Host "Stop requested for supervisor $($state.run_id)."
    if ($StopActiveChunk) { Write-Host "Active chunk stop was also requested." }
  }
  exit 0
}

$payload = Build-StatusPayload
if ($Json) {
  Write-Output (Convert-ToSupervisorJson $payload)
} else {
  Write-Host "Batch supervisor status: $($payload.status)"
  if ($payload.run_id) { Write-Host "Run: $($payload.run_id) pid=$($payload.pid) chunks_started=$($payload.chunks_started) max_chunks=$($payload.max_chunks)" }
  if ($payload.stdout_log) { Write-Host "Stdout log: $($payload.stdout_log)" }
  if ($payload.stderr_log) { Write-Host "Stderr log: $($payload.stderr_log)" }
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
