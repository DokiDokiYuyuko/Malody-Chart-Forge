$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location -LiteralPath $root
$env:TEMP = Join-Path $root 'cache'
$env:TMP = $env:TEMP
$env:PYTHONUTF8 = '1'
New-Item -ItemType Directory -Force -Path (Join-Path $root 'logs') | Out-Null
$url = 'http://127.0.0.1:8765'
try {
    $health = Invoke-RestMethod "$url/api/health" -TimeoutSec 3
    if ($health.engine -eq 'MuG Diffusion v1.0.0') { Start-Process $url; exit 0 }
} catch {}
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Project Python environment is missing.' }
$service = Start-Process -FilePath $python -ArgumentList '-m','uvicorn','malody_studio.server:app','--host','127.0.0.1','--port','8765' -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $root 'logs\server.stdout.log') -RedirectStandardError (Join-Path $root 'logs\server.stderr.log') -PassThru
$service.Id | Set-Content -LiteralPath (Join-Path $root 'logs\server.pid')
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Seconds 1
    if ($service.HasExited) { throw 'Server failed to start. See logs/server.stderr.log.' }
    try { $health = Invoke-RestMethod "$url/api/health" -TimeoutSec 2; Start-Process $url; exit 0 } catch {}
}
throw 'Server startup timed out. See logs/server.stderr.log.'
