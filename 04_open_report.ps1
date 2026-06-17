param(
  [string]$OutputDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "_localizer_output")
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Py)) { & (Join-Path $ProjectRoot "setup.ps1") }
& $Py -m ecse_localizer report --output $OutputDir
$Report = Join-Path $OutputDir "audit_report.md"
if (Test-Path -LiteralPath $Report) { Invoke-Item -LiteralPath $Report }
