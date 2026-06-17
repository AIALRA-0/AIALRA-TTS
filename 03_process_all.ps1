param(
  [string]$InputDir = (Split-Path -Parent $PSScriptRoot),
  [switch]$Force
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Py)) { & (Join-Path $ProjectRoot "setup.ps1") }
$Args = @("-m", "ecse_localizer", "process-all", "--input", $InputDir)
if ($Force) { $Args += "--force" }
& $Py @Args
