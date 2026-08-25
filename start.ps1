$ErrorActionPreference = "Stop"

$app = Split-Path -Parent $MyInvocation.MyCommand.Path
$port = 8787
$statusUrl = "http://127.0.0.1:$port/api/status"
$cacheDir = Join-Path $app ".cache"
$stdoutLog = Join-Path $cacheDir "server.stdout.log"
$stderrLog = Join-Path $cacheDir "server.stderr.log"

$pythonCandidates = @()
if ($env:TEXT_LAYER_PYTHON) {
  $pythonCandidates += $env:TEXT_LAYER_PYTHON
}
$pythonCandidates += (Join-Path $app ".venv\Scripts\python.exe")
$pythonCandidates += (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe")
$pythonCandidates += (Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
$pythonCandidates = $pythonCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -Unique
$python = $null
$dependencyErrors = @()
foreach ($candidate in $pythonCandidates) {
  Push-Location $app
  try {
    $checkOutput = & $candidate -c "import server" 2>&1
    $checkExitCode = $LASTEXITCODE
  } finally {
    Pop-Location
  }
  if ($checkExitCode -eq 0) {
    $python = $candidate
    break
  }
  $dependencyErrors += "$candidate`: $($checkOutput -join ' ')"
}

if (!$python) {
  $setup = "py -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
  throw "No compatible Python environment was found. Run: $setup`n$($dependencyErrors -join [Environment]::NewLine)"
}

$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
  try {
    $status = Invoke-RestMethod -Uri $statusUrl -TimeoutSec 2
    if ($status.ok -and $status.root -eq $app) {
      Start-Process "http://127.0.0.1:$port/index.html"
      exit 0
    }
  } catch {
  }
  throw "Port $port is already used by another application."
}

New-Item -ItemType Directory -Path $cacheDir -Force | Out-Null
$process = Start-Process -FilePath $python -ArgumentList @("server.py") -WorkingDirectory $app -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru

for ($i = 0; $i -lt 80; $i++) {
  try {
    $status = Invoke-RestMethod -Uri $statusUrl -TimeoutSec 1
    if ($status.ok -and $status.root -eq $app) {
      Start-Process "http://127.0.0.1:$port/index.html"
      exit 0
    }
  } catch {
  }
  if ($process.HasExited) { break }
  Start-Sleep -Milliseconds 250
}

$detail = ""
if (Test-Path -LiteralPath $stderrLog) {
  $detail = (Get-Content -LiteralPath $stderrLog -Tail 8) -join [Environment]::NewLine
}
throw "The server failed to start. Error log: $stderrLog`n$detail"
