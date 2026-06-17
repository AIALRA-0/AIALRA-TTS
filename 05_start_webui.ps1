param(
  [string]$HostName,
  [int]$Port,
  [switch]$Background
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Py)) { & (Join-Path $ProjectRoot "setup.ps1") }

$PyArgs = @("-m", "ecse_localizer.webui", "--config", (Join-Path $ProjectRoot "config.yaml"))
if ($HostName) { $PyArgs += @("--host", $HostName) }
if ($Port) { $PyArgs += @("--port", [string]$Port) }

function Quote-Arg {
  param([string]$Value)
  if ($Value -match '\s') {
    return '"' + ($Value -replace '"', '\"') + '"'
  }
  return $Value
}

if ($Background) {
  $LogDir = Join-Path $ProjectRoot "logs"
  New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
  $OutLog = Join-Path $LogDir "webui.out.log"
  $ErrLog = Join-Path $LogDir "webui.err.log"
  $ArgLine = ($PyArgs | ForEach-Object { Quote-Arg $_ }) -join " "
  $proc = Start-Process -FilePath $Py -ArgumentList $ArgLine -WorkingDirectory $ProjectRoot -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog -WindowStyle Hidden -PassThru
  Write-Host ("WebUI started: PID {0}. Logs: {1}, {2}" -f $proc.Id, $OutLog, $ErrLog)
} else {
  & $Py @PyArgs
}
