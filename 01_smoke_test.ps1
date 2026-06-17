param(
  [int]$Seconds = 90,
  [string]$InputDir = (Split-Path -Parent $PSScriptRoot)
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Py)) { & (Join-Path $ProjectRoot "setup.ps1") }
& $Py -m ecse_localizer smoke --input $InputDir --seconds $Seconds
