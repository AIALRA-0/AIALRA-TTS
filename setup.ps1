param(
  [switch]$SkipPiperDownload
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $ProjectRoot "logs"
$ModelDir = Join-Path $ProjectRoot "models"
$VenvDir = Join-Path $ProjectRoot ".venv"
New-Item -ItemType Directory -Force -Path $LogDir,$ModelDir | Out-Null
$LogFile = Join-Path $LogDir ("setup_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))

function Write-Log {
  param([string]$Message)
  $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
  Write-Host $line
  Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
}

function Invoke-WithRetry {
  param(
    [scriptblock]$Action,
    [string]$Name,
    [int]$Attempts = 3
  )
  for ($i = 1; $i -le $Attempts; $i++) {
    try {
      Write-Log "$Name attempt $i/$Attempts"
      & $Action
      return
    } catch {
      Write-Log "$Name failed: $($_.Exception.Message)"
      if ($i -eq $Attempts) { throw }
      Start-Sleep -Seconds ([Math]::Min(15, 3 * $i))
    }
  }
}

Write-Log "Project root: $ProjectRoot"
Write-Log "Checking Python"
python --version | Tee-Object -FilePath $LogFile -Append

if (-not (Test-Path -LiteralPath $VenvDir)) {
  Invoke-WithRetry -Name "Create venv" -Action { python -m venv $VenvDir }
}

$Py = Join-Path $VenvDir "Scripts\python.exe"
Invoke-WithRetry -Name "Upgrade pip" -Action { & $Py -m pip install --upgrade pip setuptools wheel }
Invoke-WithRetry -Name "Install Python requirements" -Action { & $Py -m pip install -r (Join-Path $ProjectRoot "requirements.txt") }
Invoke-WithRetry -Name "Install ecse-localizer editable package" -Action { & $Py -m pip install -e $ProjectRoot }

if (-not $SkipPiperDownload) {
  $PiperDir = Join-Path $ModelDir "piper"
  $PiperExe = Join-Path $PiperDir "piper.exe"
  $VoiceDir = Join-Path $PiperDir "voices"
  $VoiceOnnx = Join-Path $VoiceDir "zh_CN-huayan-medium.onnx"
  $VoiceJson = Join-Path $VoiceDir "zh_CN-huayan-medium.onnx.json"
  New-Item -ItemType Directory -Force -Path $PiperDir,$VoiceDir | Out-Null

  if (-not (Test-Path -LiteralPath $PiperExe)) {
    $ZipPath = Join-Path $ModelDir "piper_windows_amd64.zip"
    $Url = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip"
    Invoke-WithRetry -Name "Download Piper Windows binary (~21 MB)" -Action {
      Invoke-WebRequest -Uri $Url -OutFile $ZipPath -UseBasicParsing
    }
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $ModelDir -Force
    $Found = Get-ChildItem -LiteralPath $ModelDir -Recurse -Filter "piper.exe" | Select-Object -First 1
    if (-not $Found) { throw "piper.exe not found after extraction" }
    if ($Found.DirectoryName -ne $PiperDir) {
      Copy-Item -LiteralPath (Join-Path $Found.DirectoryName "*") -Destination $PiperDir -Recurse -Force
    }
  }

  if (-not (Test-Path -LiteralPath $VoiceOnnx)) {
    Invoke-WithRetry -Name "Download Piper Mandarin voice model (~63 MB)" -Action {
      Invoke-WebRequest -Uri "https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx" -OutFile $VoiceOnnx -UseBasicParsing
    }
  }
  if (-not (Test-Path -LiteralPath $VoiceJson)) {
    Invoke-WithRetry -Name "Download Piper Mandarin voice config" -Action {
      Invoke-WebRequest -Uri "https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx.json" -OutFile $VoiceJson -UseBasicParsing
    }
  }
  Write-Log "Piper ready: $PiperExe"
  Write-Log "Piper voice ready: $VoiceOnnx"
}

Write-Log "Setup complete. Use .\.venv\Scripts\python.exe -m ecse_localizer ..."
