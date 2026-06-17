param(
  [string]$Path = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path -LiteralPath $Path
$patterns = @(
  'password\s*[:=]\s*["'']?[^"''\s]+',
  'session_secret\s*[:=]\s*["'']?[^"''\s]+',
  'api[_-]?key\s*[:=]',
  'bearer\s+[a-z0-9._-]+',
  'token\s*[:=]\s*["'']?[^"''\s]+',
  'C:\\Users\\[^\\]+',
  '\b(?:\d{1,3}\.){3}\d{1,3}\b'
)

$ignoreDirs = @('.git', 'runs', 'logs', 'models', 'third_party', '.venv', '.conda', '__pycache__')
$ignoreFiles = @('config.yaml')
$files = Get-ChildItem -LiteralPath $root -Recurse -File -Force |
  Where-Object {
    $relativeParts = $_.FullName.Substring($root.Path.Length).TrimStart('\', '/') -split '[\\/]'
    (-not ($relativeParts | Where-Object { $ignoreDirs -contains $_ })) -and ($ignoreFiles -notcontains $_.Name)
  }

$hits = @()
foreach ($file in $files) {
  $relative = (Resolve-Path -LiteralPath $file.FullName -Relative) -replace '^[.][\\/]', ''
  foreach ($pattern in $patterns) {
    $matches = Select-String -LiteralPath $file.FullName -Pattern $pattern -CaseSensitive:$false -ErrorAction SilentlyContinue
    foreach ($match in $matches) {
      $line = $match.Line.Trim()
      if ($relative -eq '.env.example' -or $relative -eq 'tools\secret_scan.ps1') { continue }
      if ($line -match 'change-me|example\.invalid|127\.0\.0\.1|0\.0\.0\.0|localhost|password_hash|WORKER_SHARED_TOKEN|WEBUI_|token = request\.headers|expected = str|password\s*=\s*str\(|password:\s+str') { continue }
      if (($file.Extension -in @('.py', '.js', '.ts')) -and ($pattern -match 'password|token|session_secret')) { continue }
      $hits += [pscustomobject]@{
        File = $relative
        Line = $match.LineNumber
        Pattern = $pattern
        Text = $line
      }
    }
  }
}

if ($hits.Count -gt 0) {
  $hits | Format-Table -AutoSize
  throw "Potential secrets or machine-specific values found. Remove them before committing."
}

Write-Host "Secret scan passed for $root"
