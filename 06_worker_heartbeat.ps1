param(
  [string]$RemoteBaseUrl = $env:REMOTE_PUBLIC_BASE_URL,
  [string]$WorkerToken = $env:WORKER_SHARED_TOKEN,
  [int]$IntervalSeconds = 30,
  [switch]$Loop
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Py)) { & (Join-Path $ProjectRoot "setup.ps1") }

if (-not $RemoteBaseUrl) {
  throw "RemoteBaseUrl is required. Set REMOTE_PUBLIC_BASE_URL or pass -RemoteBaseUrl."
}
if (-not $WorkerToken) {
  throw "WorkerToken is required. Set WORKER_SHARED_TOKEN or pass -WorkerToken."
}

function ConvertTo-LowerHex {
  param([byte[]]$Bytes)
  return ([System.BitConverter]::ToString($Bytes)).Replace("-", "").ToLowerInvariant()
}

function New-WorkerSignedHeaders {
  param(
    [string]$Token,
    [string]$Method,
    [string]$Path,
    [string]$Body
  )
  $timestamp = [string][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
  $nonce = [guid]::NewGuid().ToString("N")
  $encoding = [System.Text.Encoding]::UTF8
  $bodyBytes = $encoding.GetBytes($Body)
  $sha = [System.Security.Cryptography.SHA256]::Create()
  $bodyHash = ConvertTo-LowerHex -Bytes ($sha.ComputeHash($bodyBytes))
  $message = "$timestamp`n$($Method.ToUpperInvariant())`n$Path`n$nonce`n$bodyHash"
  $hmac = [System.Security.Cryptography.HMACSHA256]::new($encoding.GetBytes($Token))
  $signature = ConvertTo-LowerHex -Bytes ($hmac.ComputeHash($encoding.GetBytes($message)))
  return @{
    "X-Worker-Auth" = "hmac-sha256"
    "X-Worker-Timestamp" = $timestamp
    "X-Worker-Nonce" = $nonce
    "X-Worker-Signature" = $signature
  }
}

function Send-Heartbeat {
  $statusJson = & $Py -m ecse_localizer --config (Join-Path $ProjectRoot "config.yaml") worker-status
  if ($LASTEXITCODE -ne 0) { throw "worker-status failed with exit code $LASTEXITCODE" }
  $path = "/api/worker/heartbeat"
  $uri = ($RemoteBaseUrl.TrimEnd('/')) + $path
  $headers = New-WorkerSignedHeaders -Token $WorkerToken -Method "POST" -Path $path -Body $statusJson
  Invoke-RestMethod -Method Post -Uri $uri -Headers $headers -Body $statusJson -ContentType "application/json" | Out-Null
  Write-Host ("Heartbeat sent to {0} at {1}" -f $uri, (Get-Date).ToString("s"))
}

if ($Loop) {
  while ($true) {
    try {
      Send-Heartbeat
    } catch {
      Write-Warning $_
    }
    Start-Sleep -Seconds ([Math]::Max(5, $IntervalSeconds))
  }
} else {
  Send-Heartbeat
}
