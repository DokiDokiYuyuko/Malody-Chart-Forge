$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location -LiteralPath $root
$env:TEMP = Join-Path $root 'cache'
$env:TMP = $env:TEMP
$env:PYTHONUTF8 = '1'
New-Item -ItemType Directory -Force -Path (Join-Path $root 'logs') | Out-Null
$port = 8765
$url = "http://127.0.0.1:$port"
$existing = $null
try { $existing = Invoke-RestMethod "$url/api/health" -TimeoutSec 30 } catch {}
if ($existing -and $existing.api_version -ge 2) { Start-Process $url; exit 0 }
if ($existing -or (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
    $port = 8766
    $url = "http://127.0.0.1:$port"
    try {
        $existing = Invoke-RestMethod "$url/api/health" -TimeoutSec 30
        if ($existing.api_version -ge 2) { Start-Process $url; exit 0 }
    } catch {}
}
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Environment is not configured. Run 一键配置环境.bat first.' }
$logStem = if ($port -eq 8765) { 'server' } else { "server-$port" }
$service = Start-Process -FilePath $python -ArgumentList '-m','uvicorn','malody_studio.server:app','--host','127.0.0.1','--port',[string]$port -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $root "logs\$logStem.stdout.log") -RedirectStandardError (Join-Path $root "logs\$logStem.stderr.log") -PassThru
$service.Id | Set-Content -LiteralPath (Join-Path $root "logs\$logStem.pid")
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Seconds 1
    if ($service.HasExited) { throw "Server failed to start. See logs/$logStem.stderr.log." }
    try { $health = Invoke-RestMethod "$url/api/health" -TimeoutSec 2; if ($health.api_version -ge 2) { Start-Process $url; exit 0 } } catch {}
}
throw "Server startup timed out. See logs/$logStem.stderr.log."
