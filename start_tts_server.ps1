$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Piper = Join-Path $ProjectRoot "models\piper\piper.exe"
if (Test-Path -LiteralPath $Piper) {
  Write-Host "Piper is a CLI backend; no persistent server is required."
  & $Piper --help | Select-Object -First 20
} else {
  Write-Host "Piper is not installed. Run .\setup.ps1 first."
  exit 1
}
