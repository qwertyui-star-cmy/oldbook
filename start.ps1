$ErrorActionPreference = "Stop"

$app = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$port = 8787

if (!(Test-Path -LiteralPath $python)) {
  Write-Host "Python runtime not found: $python"
  Start-Sleep -Seconds 5
  exit 1
}

$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
  try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/api/status" -TimeoutSec 2
    if ($response.StatusCode -eq 200) {
      Start-Process "http://127.0.0.1:$port/index.html"
      exit 0
    }
  } catch {
    Write-Host "Port $port is occupied by another program."
    Start-Sleep -Seconds 5
    exit 1
  }
}

Start-Process -FilePath $python -ArgumentList @("server.py") -WorkingDirectory $app -WindowStyle Hidden

for ($i = 0; $i -lt 30; $i++) {
  try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/api/status" -TimeoutSec 1
    if ($response.StatusCode -eq 200) { break }
  } catch {
    Start-Sleep -Milliseconds 150
  }
}

Start-Process "http://127.0.0.1:$port/index.html"
