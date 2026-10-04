param(
    [string]$HfEndpoint = 'https://huggingface.co',
    [string]$WeightsPath
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$bootstrapPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $bootstrapPython)) { throw '请先运行一键配置环境.bat，完成制谱台基础安装。' }
Push-Location -LiteralPath $projectRoot
try {
    $uv = Join-Path $projectRoot 'cache\separation-bootstrap\uv\uv.exe'
    if (-not (Test-Path -LiteralPath $uv)) {
        & $bootstrapPython (Join-Path $PSScriptRoot 'setup_separation.py') --bootstrap
        if ($LASTEXITCODE -ne 0) { throw '项目内安装工具下载失败。' }
    }
    $basePython = Join-Path $projectRoot 'runtime\python\cpython-3.10.19-windows-x86_64-none\python.exe'
    if (-not (Test-Path -LiteralPath $basePython)) {
        $previousInstall = $env:UV_PYTHON_INSTALL_DIR
        try {
            $env:UV_PYTHON_INSTALL_DIR = Join-Path $projectRoot 'runtime\python'
            & $uv python install --no-bin --no-registry 3.10.19
            if ($LASTEXITCODE -ne 0) { throw '项目内 Python 3.10 安装失败。' }
        } finally { $env:UV_PYTHON_INSTALL_DIR = $previousInstall }
    }
    $arguments = @((Join-Path $PSScriptRoot 'setup_roformer.py'), '--python', $basePython, '--hf-endpoint', $HfEndpoint)
    if ($WeightsPath) { $arguments += @('--weights-path', (Resolve-Path -LiteralPath $WeightsPath).Path) }
    & $bootstrapPython @arguments
    if ($LASTEXITCODE -ne 0) { throw 'RoFormer 独立环境或固定权重验证失败，请查看上方具体原因。' }
    Write-Host '质量档已安装。请在高级制谱 → 分离中先试一小段。'
} finally { Pop-Location }
