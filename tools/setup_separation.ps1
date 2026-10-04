param([switch]$FineTuned)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$bootstrapPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
& $bootstrapPython (Join-Path $PSScriptRoot 'setup_separation.py') --bootstrap
if ($LASTEXITCODE -ne 0) { throw '分离环境工具下载失败' }
$uv = Join-Path $projectRoot 'cache\separation-bootstrap\uv\uv.exe'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $projectRoot 'runtime\python'
$env:UV_CACHE_DIR = Join-Path $projectRoot 'cache\uv'
$env:UV_CREDENTIALS_DIR = Join-Path $projectRoot 'cache\uv-credentials'
$env:TEMP = Join-Path $projectRoot 'cache'
$env:TMP = $env:TEMP
& $uv python install --no-bin --no-registry 3.10.19
if ($LASTEXITCODE -ne 0) { throw '项目内 Python 安装失败' }
$separationPython = Join-Path $projectRoot 'runtime\separation-venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $separationPython)) {
    & $uv venv --python 3.10.19 (Join-Path $projectRoot 'runtime\separation-venv')
    if ($LASTEXITCODE -ne 0) { throw '独立分离环境创建失败' }
}
$lock = Join-Path $projectRoot 'runtime\separation-requirements-lock.txt'
if (-not (Test-Path -LiteralPath $lock)) { $lock = Join-Path $projectRoot 'runtime\separation-requirements.txt' }
& $uv pip install --python $separationPython -r $lock
if ($LASTEXITCODE -ne 0) { throw '独立分离依赖安装失败' }
$arguments = @((Join-Path $PSScriptRoot 'setup_separation.py'))
if ($FineTuned) { $arguments += '--ft' }
& $bootstrapPython @arguments
if ($LASTEXITCODE -ne 0) { throw '分离权重登记或环境校验失败' }
