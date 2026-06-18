param(
  [Parameter(Mandatory=$true)][string]$Video
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Py)) { & (Join-Path $ProjectRoot "setup.ps1") }
& $Py -m ecse_localizer process-one --video $Video
exit $LASTEXITCODE
