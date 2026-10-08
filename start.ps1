$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location -LiteralPath $root
$env:TEMP = Join-Path $root 'cache'
$env:TMP = $env:TEMP
$env:PYTHONUTF8 = '1'
New-Item -ItemType Directory -Force -Path (Join-Path $root 'logs') | Out-Null
# Reuse a server with the current workflow, rather than opening an older
# process that happens to answer the original health endpoint.
function Test-CurrentServer([string]$candidateUrl) {
    try {
        $health = Invoke-RestMethod "$candidateUrl/api/health" -TimeoutSec 3
        if ($health.api_version -lt 2 -or $health.advanced_workflow_version -lt 12 -or $health.workflow_repair_version -lt 2 -or $health.music_workflow_version -lt 2 -or $health.gpu_resident_version -lt 1 -or $health.chart_quality_version -lt 4 -or $health.density_validation_version -lt 1 -or $health.music_assets_version -lt 1 -or $health.library_version -lt 3) { return $false }
        if ($health.nps_star_direct_version -lt 1) { return $false }
        $schema = Invoke-RestMethod "$candidateUrl/openapi.json" -TimeoutSec 3
        $paths = $schema.paths.PSObject.Properties.Name
        return ($paths -contains '/api/advanced/projects/{pid}/separation-trials' -and
                $paths -contains '/api/advanced/separation/models' -and
                $paths -contains '/api/advanced/projects/{pid}/audition-metadata' -and
                $paths -contains '/api/advanced/projects/{pid}/separations' -and
                $paths -contains '/api/advanced/projects/{pid}/segmentation-apply' -and
                $paths -contains '/api/advanced/projects/{pid}/generation-batches' -and
                $paths -contains '/api/advanced/projects/{pid}/music-analysis' -and
                $paths -contains '/api/advanced/projects/{pid}/confirm-and-generate' -and
                $paths -contains '/api/advanced/projects/{pid}/quality-candidates' -and
                $paths -contains '/api/advanced/projects/{pid}/arrangement-candidates' -and
                $paths -contains '/api/advanced/projects/{pid}/workflow-candidates' -and
                $paths -contains '/api/gpu/resident/release')
    } catch { return $false }
}
$port = $null
$oldServiceUrl = $null
foreach ($candidatePort in 8765..8774) {
    $candidateUrl = "http://127.0.0.1:$candidatePort"
    if (Test-CurrentServer $candidateUrl) { Start-Process $candidateUrl; exit 0 }
    try {
        $existingHealth = Invoke-RestMethod "$candidateUrl/api/health" -TimeoutSec 2
        if ($existingHealth.api_version -ge 2 -and $existingHealth.engines) { $oldServiceUrl = $candidateUrl }
    } catch { }
    if ($null -eq $port -and -not (Get-NetTCPConnection -LocalPort $candidatePort -State Listen -ErrorAction SilentlyContinue)) {
        $port = $candidatePort
    }
}
if ($null -ne $oldServiceUrl) { throw "Existing studio at $oldServiceUrl needs a service update. Close that service, then start the studio again." }
if ($null -eq $port) { throw 'No free local port (8765-8774). Close an unused server and try again.' }
$url = "http://127.0.0.1:$port"
$portArgument = $port.ToString([Globalization.CultureInfo]::InvariantCulture)
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Environment is not configured. Run 一键配置环境.bat first.' }
$logStem = if ($port -eq 8765) { 'server' } else { "server-$port" }
$service = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','malody_studio.server:app','--host','127.0.0.1','--port',$portArgument,'--timeout-graceful-shutdown','15') -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $root "logs\$logStem.stdout.log") -RedirectStandardError (Join-Path $root "logs\$logStem.stderr.log") -PassThru
$service.Id | Set-Content -LiteralPath (Join-Path $root "logs\$logStem.pid")
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Seconds 1
    if ($service.HasExited) { throw "Server failed to start. See logs/$logStem.stderr.log." }
    try { $health = Invoke-RestMethod "$url/api/health" -TimeoutSec 2; if ($health.api_version -ge 2) { Start-Process $url; exit 0 } } catch {}
}
throw "Server startup timed out. See logs/$logStem.stderr.log."
