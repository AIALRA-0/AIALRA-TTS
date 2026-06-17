param(
  [string]$Config = (Join-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "deploy") "config.remote.example.yaml"),
  [switch]$Json
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Py)) { & (Join-Path $ProjectRoot "setup.ps1") }

$argsList = @(
  "-m", "ecse_localizer",
  "--config", $Config,
  "deploy-check"
)
if ($Json) { $argsList += "--json" }

& $Py @argsList
exit $LASTEXITCODE
