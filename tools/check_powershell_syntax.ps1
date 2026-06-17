param(
  [string]$Root = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
)

$ErrorActionPreference = "Stop"
$RootPath = (Resolve-Path -LiteralPath $Root).Path
$SkipDirs = @(
  ".git",
  ".venv",
  ".conda",
  ".pytest_cache",
  "logs",
  "runs",
  "models",
  "third_party",
  "_localizer_output"
)

$scripts = Get-ChildItem -LiteralPath $RootPath -Recurse -Filter "*.ps1" -File |
  Where-Object {
    $relative = [System.IO.Path]::GetRelativePath($RootPath, $_.FullName)
    $parts = $relative -split '[\\/]'
    -not ($parts | Where-Object { $SkipDirs -contains $_ })
  } |
  Sort-Object FullName

$failed = $false
foreach ($script in $scripts) {
  $tokens = $null
  $errors = $null
  [System.Management.Automation.Language.Parser]::ParseFile($script.FullName, [ref]$tokens, [ref]$errors) | Out-Null
  if ($errors -and $errors.Count) {
    $failed = $true
    foreach ($errorItem in $errors) {
      $line = $errorItem.Extent.StartLineNumber
      $column = $errorItem.Extent.StartColumnNumber
      Write-Error "$($script.FullName):$($line):$($column) $($errorItem.Message)"
    }
  }
}

if ($failed) {
  exit 1
}

Write-Host "PowerShell syntax PASS: $($scripts.Count) script(s)"
