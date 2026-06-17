param(
  [string]$Output,
  [string]$Config,
  [switch]$Json
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Py)) { & (Join-Path $ProjectRoot "setup.ps1") }

if (-not $Config) {
  $LocalConfig = Join-Path $ProjectRoot "config.yaml"
  $ExampleConfig = Join-Path $ProjectRoot "config.example.yaml"
  $Config = if (Test-Path -LiteralPath $LocalConfig) { $LocalConfig } else { $ExampleConfig }
}
if (-not $Output) {
  $Output = Join-Path $ProjectRoot "runs\platform_check"
}

$argsList = @(
  "-m", "ecse_localizer",
  "--config", $Config,
  "platform-check"
)
$argsList += @("--output", $Output)
if ($Json) { $argsList += "--json" }

& $Py @argsList
exit $LASTEXITCODE
