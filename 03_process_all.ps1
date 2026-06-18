param(
  [string]$InputDir = (Split-Path -Parent $PSScriptRoot),
  [switch]$Force,
  [int]$Limit = 0,
  [switch]$ShortestFirst
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Py)) { & (Join-Path $ProjectRoot "setup.ps1") }
$Args = @("-m", "ecse_localizer", "process-all", "--input", $InputDir)
if ($Force) { $Args += "--force" }
if ($Limit -gt 0) { $Args += @("--limit", [string]$Limit) }
if ($ShortestFirst) { $Args += "--shortest-first" }
& $Py @Args
exit $LASTEXITCODE
