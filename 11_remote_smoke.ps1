param(
  [string]$Output,
  [switch]$Json
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Py)) { & (Join-Path $ProjectRoot "setup.ps1") }

$argsList = @(
  "-m", "ecse_localizer",
  "--config", (Join-Path $ProjectRoot "config.yaml"),
  "remote-smoke"
)
if ($Output) { $argsList += @("--output", $Output) }
if ($Json) { $argsList += "--json" }

& $Py @argsList
exit $LASTEXITCODE
