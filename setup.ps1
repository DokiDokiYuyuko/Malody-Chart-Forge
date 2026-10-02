[CmdletBinding()]
param(
    [ValidateSet('mug', 'v32', 'all')]
    [string]$Engine = '',
    [string]$PipIndexUrl = '',
    [string]$HFEndpoint = '',
    [string]$Proxy = '',
    [switch]$SkipWeights,
    [switch]$SkipYouTube,
    [switch]$StartApp
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location -LiteralPath $root
$logPath = Join-Path $root 'logs\setup.log'
New-Item -ItemType Directory -Force -Path (Join-Path $root 'logs'), (Join-Path $root 'cache'), (Join-Path $root 'models') | Out-Null

function Protect-SetupText([string]$Message) {
    $safe = [regex]::Replace($Message, '(?i)(https?://)[^/@:\s]+:[^/@\s]+@', '$1***@')
    return [regex]::Replace($safe, '(?i)([?&](?:token|access_token|password|passwd|api_key|key)=)[^&\s]+', '$1***')
}

function Write-SetupLog([string]$Message) {
    $safe = Protect-SetupText $Message
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $safe
    Write-Host $safe
    Add-Content -LiteralPath $logPath -Value $line -Encoding utf8
}

function Invoke-SetupWebRequest([string]$Uri, [string]$OutFile) {
    $request = @{ Uri = $Uri; OutFile = $OutFile; UseBasicParsing = $true }
    if ($Proxy) { $request.Proxy = $Proxy }
    Invoke-WebRequest @request
}

function Invoke-Native([string]$Program, [string[]]$NativeArgs, [string]$Label) {
    Write-SetupLog $Label
    $output = & $Program @NativeArgs 2>&1
    $exitCode = $LASTEXITCODE
    foreach ($line in $output) {
        $safe = Protect-SetupText ([string]$line)
        Write-Host $safe
        Add-Content -LiteralPath $logPath -Value $safe -Encoding utf8
    }
    if ($exitCode -ne 0) { throw "$Label 失败，退出码 $exitCode。请查看 logs/setup.log。" }
}

try {
    if (-not $Engine) {
        Write-Host '选择要配置的生成引擎：'
        Write-Host '  1. MuG（基础安装，权重约 1.84 GB）'
        Write-Host '  2. V32（独立环境，需要 NVIDIA CUDA，权重约 1.73 GB）'
        Write-Host '  3. 两者都安装'
        $choice = Read-Host '输入 1、2 或 3'
        $Engine = switch ($choice) { '1' {'mug'} '2' {'v32'} '3' {'all'} default { throw '请输入 1、2 或 3。' } }
    }
    if (-not $PipIndexUrl) { $PipIndexUrl = Read-Host 'pip 镜像地址（留空使用官方源或现有配置）' }
    if (-not $HFEndpoint) { $HFEndpoint = Read-Host 'Hugging Face 兼容端点（留空使用官方源）' }
    if (-not $Proxy) { $Proxy = Read-Host 'HTTP/HTTPS 代理地址（不需要则留空）' }
    if ($PipIndexUrl) { $env:PIP_INDEX_URL = $PipIndexUrl }
    if ($HFEndpoint) {
        if ($HFEndpoint -notmatch '^https?://') { throw 'Hugging Face 端点必须以 http:// 或 https:// 开头。' }
        $env:HF_ENDPOINT = $HFEndpoint.TrimEnd('/')
    }
    if ($Proxy) { $env:HTTP_PROXY = $Proxy; $env:HTTPS_PROXY = $Proxy }
    $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
    $env:PIP_NO_INPUT = '1'

    if ($PSVersionTable.PSVersion.Major -lt 5) { throw '请使用 Windows PowerShell 5.1 或更新版本。' }
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw '没有找到 Python Launcher。请安装 Python 3.12 x64 并勾选 Python Launcher。' }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw '没有找到 Git for Windows。请先安装 Git 并重新运行。' }
    $pythonVersion = & py -3.12 -c "import platform,struct; print(platform.python_version()); print(struct.calcsize('P')*8)"
    if ($LASTEXITCODE -ne 0 -or $pythonVersion.Count -lt 2 -or $pythonVersion[1] -ne '64') {
        throw '未检测到 Python 3.12 x64。请安装 Python 3.12 x64 后重试。'
    }
    Write-SetupLog "预检通过：Python $($pythonVersion[0]) x64。"
    $drive = [System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($root))
    $needed = if ($Engine -eq 'all') { 18GB } else { 10GB }
    if ($drive.AvailableFreeSpace -lt $needed) {
        Write-SetupLog "磁盘可用空间约 $([math]::Round($drive.AvailableFreeSpace/1GB,1)) GB；建议至少保留 $([math]::Round($needed/1GB)) GB。"
        $continue = Read-Host '仍要继续吗？输入 Y 继续'
        if ($continue -notmatch '^(?i)y$') { throw '用户取消：磁盘空间不足。' }
    }

    if (Test-Path -LiteralPath (Join-Path $root '.gitmodules')) {
        if (-not (Test-Path -LiteralPath (Join-Path $root '.git'))) {
            throw '当前目录不是 Git clone。请运行 README 中带 --recurse-submodules 的 clone 命令，再启动配置脚本。'
        }
        Invoke-Native 'git' @('submodule','update','--init','--recursive') '准备模型上游源码'
    }
    if ($Engine -in @('mug','all') -and -not (Test-Path -LiteralPath (Join-Path $root 'vendor\Mug-Diffusion\mug'))) {
        throw 'MuG 上游源码缺失，请检查 Git 子模块是否下载完整。'
    }
    if ($Engine -in @('v32','all') -and -not (Test-Path -LiteralPath (Join-Path $root 'vendor\Mapperatorinator'))) {
        throw 'V32 上游源码缺失，请检查 Git 子模块是否下载完整。'
    }

    $mainPython = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $mainPython)) { Invoke-Native 'py' @('-3.12','-m','venv','.venv') '创建主 Python 环境' }
    $pipArgs = @('-m','pip','install','--disable-pip-version-check')
    if ($PipIndexUrl) { $pipArgs += @('--index-url',$PipIndexUrl) }
    Invoke-Native $mainPython ($pipArgs + @('--upgrade','pip')) '安装主环境安装工具'
    Invoke-Native $mainPython ($pipArgs + @('-r','requirements-lock.txt')) '安装主环境与 MuG 依赖'
    $gpuJson = & $mainPython -c "import json,torch; print(json.dumps({'torch':torch.__version__,'cuda':torch.cuda.is_available(),'gpu':torch.cuda.get_device_name(0) if torch.cuda.is_available() else None}))"
    if ($LASTEXITCODE -ne 0) { throw 'PyTorch 检查失败，请查看 logs/setup.log。' }
    $gpu = $gpuJson | ConvertFrom-Json
    Write-SetupLog "主环境已安装：PyTorch $($gpu.torch)，CUDA $($gpu.cuda)，设备 $($gpu.gpu)。"

    if ($Engine -in @('mug','all') -and -not $SkipWeights) {
        $mugWeight = Join-Path $root 'models\mug-diffusion\v1.0.0\model.ckpt'
        $mugHash = if (Test-Path -LiteralPath $mugWeight) { (Get-FileHash -LiteralPath $mugWeight -Algorithm SHA256).Hash.ToLowerInvariant() } else { '' }
        if ($mugHash -ne 'af6ab91337d0ef6b518367082ac3f849448c6daaa01fd987678fb25ea44ca184') {
            Write-Host 'MuG 权重及其生成内容受上游非商业使用限制。来源和条款见 README 中的模型链接。'
            $accept = Read-Host '确认仅按上游条款使用 MuG 权重吗？输入 Y 下载'
            if ($accept -notmatch '^(?i)y$') { throw 'MuG 条款未确认，已停止下载 MuG 权重。' }
            $base = if ($HFEndpoint) { $HFEndpoint.TrimEnd('/') } elseif ($env:HF_ENDPOINT) { $env:HF_ENDPOINT.TrimEnd('/') } else { 'https://huggingface.co' }
            $url = "$base/ayousanz/Mug-Diffusion-model/resolve/main/model.ckpt"
            Invoke-Native $mainPython @('tools\download_file.py',$url,'models\mug-diffusion\v1.0.0\model.ckpt','--workers','4','--sha256','af6ab91337d0ef6b518367082ac3f849448c6daaa01fd987678fb25ea44ca184') '下载并校验 MuG 权重'
        } else { Write-SetupLog 'MuG 权重 SHA-256 已匹配，跳过下载。' }
        Invoke-Native $mainPython @('tools\validate_mug_install.py') '加载 MuG 并检查模型结构'
    }

    if ($Engine -in @('v32','all')) {
        $v32Python = Join-Path $root 'runtime\mapperatorinator-venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $v32Python)) { Invoke-Native 'py' @('-3.12','-m','venv','runtime\mapperatorinator-venv') '创建 V32 独立环境' }
        $v32PipArgs = @('-m','pip','install','--disable-pip-version-check')
        if ($PipIndexUrl) { $v32PipArgs += @('--index-url',$PipIndexUrl) }
        Invoke-Native $v32Python ($v32PipArgs + @('--upgrade','pip')) '安装 V32 环境安装工具'
        Invoke-Native $v32Python ($v32PipArgs + @('-r','runtime\mapperatorinator-requirements-lock.txt')) '安装 V32 依赖'
        $v32Gpu = & $v32Python -c "import torch; print(torch.cuda.is_available())"
        if ($LASTEXITCODE -ne 0 -or $v32Gpu -notmatch '^True') { throw 'V32 环境无法使用 CUDA。请检查 NVIDIA 驱动和 CUDA 依赖锁文件后重试。' }
        if (-not $SkipWeights) {
            Invoke-Native $mainPython @('tools\download_mapperatorinator.py') '下载并校验 V32 四键权重'
            Invoke-Native $mainPython @('tools\download_mapperatorinator.py','--base') '下载并校验 V32 节拍权重'
            Invoke-Native $v32Python @('tools\validate_v32_install.py') '加载 V32 并执行本地冒烟验证'
        } else { Write-SetupLog '按要求跳过 V32 权重和推理校验。' }
    }

    if (-not $SkipYouTube -and -not (Test-Path -LiteralPath (Join-Path $root 'runtime\bin\yt-dlp.exe'))) {
        New-Item -ItemType Directory -Force -Path (Join-Path $root 'runtime\bin') | Out-Null
        $ytBase = 'https://github.com/yt-dlp/yt-dlp/releases/latest/download'
        $ytPath = Join-Path $root 'runtime\bin\yt-dlp.exe'
        $sumPath = Join-Path $root 'cache\yt-dlp-SHA2-256SUMS'
        try {
            Invoke-SetupWebRequest "$ytBase/SHA2-256SUMS" $sumPath
            Invoke-SetupWebRequest "$ytBase/yt-dlp.exe" $ytPath
            $sumLine = Select-String -LiteralPath $sumPath -Pattern '([a-fA-F0-9]{64})\s+\*?yt-dlp\.exe$' | Select-Object -First 1
            if (-not $sumLine -or (Get-FileHash -LiteralPath $ytPath -Algorithm SHA256).Hash -ne $sumLine.Matches[0].Groups[1].Value) {
                Remove-Item -LiteralPath $ytPath -Force -ErrorAction SilentlyContinue
                throw 'yt-dlp SHA-256 校验失败。'
            }
            Write-SetupLog 'yt-dlp 已从官方发布页下载并通过 SHA-256 校验。'
        } catch {
            Remove-Item -LiteralPath $ytPath -Force -ErrorAction SilentlyContinue
            Write-SetupLog 'yt-dlp 下载或校验失败；本地上传不受影响，可稍后从 GitHub 官方发布页重新安装。'
        }
    }

    Write-SetupLog '配置完成。请双击“启动制谱台.bat”打开网页。'
    Write-SetupLog "安装日志：$logPath"
    if ($StartApp) { & (Join-Path $root 'start.ps1') }
} catch {
    Write-SetupLog ("配置未完成：" + $_.Exception.Message)
    exit 1
}
