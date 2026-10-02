"""Render the project model inventory from verified per-model manifests."""
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / 'models'

def main():
    lines = ['# 本地模型权重登记', '', '所有制谱权重统一保存在 `I:\\model\\malody\\models`。源码位于 `vendor`，运行环境位于 `runtime`，下载与安装缓存位于 `cache`。', '',
             '## 目录', '', '```text', 'models/', '  mug-diffusion/v1.0.0/      # 已部署的 MuG 基线',
             '  mapperatorinator/', '    v32-mania/             # 四键生成专用模型',
             '    v32-timing/            # 基础模型，用于自动节拍生成', '```', '',
             '每个目录的 `manifest.json` 记录下载来源、版本、文件字节数和 SHA-256。V32 只下载推理需要的权重和配置，未下载训练数据。', '',
             '## 权重与版本', '']
    for manifest_path in sorted(MODELS.glob('*/*/manifest.json')):
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        lines.extend(['### ' + manifest_path.parent.relative_to(MODELS).as_posix(), '',
                      '- 来源：' + manifest['source'],
                      '- 仓库版本：`' + manifest.get('revision', '镜像最初下载未固定提交；按下列哈希核实') + '`',
                      '- 权重子目录：`' + (manifest.get('subfolder') or '仓库根目录') + '`', '',
                      '| 文件 | 字节数 | SHA-256 |', '| --- | ---: | --- |'])
        for record in manifest['files']:
            lines.append(f'| {record["file"]} | {record["bytes"]} | `{record["sha256"]}` |')
        lines.append('')
    companion = MODELS / 'mapperatorinator' / 'backbone-configs.json'
    if companion.is_file():
        lines.extend(['## 架构初始化配置（没有下载 Whisper 权重）', '', '| 配置来源 | 版本 | SHA-256 |', '| --- | --- | --- |'])
        for record in json.loads(companion.read_text(encoding='utf-8')):
            lines.append(f'| {record["repo"]} / config.json | `{record["revision"]}` | `{record["sha256"]}` |')
        lines.extend(['', '配置备份位于 `mapperatorinator/backbone-configs`。每次启动从已核实备份恢复项目内缓存，清理 cache 后仍可离线初始化。', ''])
    revision = subprocess.check_output(['git', '-C', str(ROOT / 'vendor' / 'Mapperatorinator'), 'rev-parse', 'HEAD'], text=True).strip()
    lines.extend(['## 部署', '', '- V32 源码提交：`' + revision + '`。上游源码保持完整；本项目适配在 `tools/mapperatorinator_worker.py` 和 `malody_studio/mapperatorinator.py`。',
                  '- MuG：`.venv`，Python 3.12 / PyTorch 2.5.1 CUDA 12.4。',
                  '- V32：`runtime/mapperatorinator-venv`，Python 3.12 / PyTorch 2.10 CUDA 13.0；实际冻结版本见 `runtime/mapperatorinator-requirements-lock.txt`。Python 3.12 为本机部署选择，需以本机验证为准。',
                  '- V32 采用 BF16、SDPA、本地进程；无需 Flash Attention 或远程推理。切换到 V32 前释放 MuG 模型显存；单队列执行生成。',
                  '- V32 节拍与 mania 权重各加载一份；未部署 Mini，未下载 standard 坐标扩散模型。',
                  '- 验证记录见 `docs/v32-deployment.md`；只有完成真实推理后才写入 `mapperatorinator/deployment-ready.json`。', '',
                  '## 格式与来源说明', '',
                  '- V32 保留原始 `.osu` 于任务目录 `v32-original`，转换为 Key 4K 的 UTF-8 MC，跨 BPM 长条保留音频时间。生成的滚速效果不导出，采用固定滚速。',
                  '- 曲包统一含 OGG Vorbis / 44100 Hz / stereo，MC 标注实际 AI 引擎。手机导入与手感不由格式校验代替。',
                  '- Mapperatorinator 代码及 Hugging Face 模型卡标注 MIT；原始声明见 `vendor/Mapperatorinator/LICENSE` 和模型来源。MuG 的声明见 `docs/model-selection.md` 与上游 README。音乐另行保留其来源。', '',
                  '## 维护', '', '`tools/download_mapperatorinator.py` 下载 mania 权重，`--base` 下载节拍模型；下载可重试并校验公开 LFS 哈希。`tools/update_model_registry.py` 根据清单重建本文档。更换权重时使用新版本目录，固定版本与哈希并重新验证后再启用。', ''])
    (MODELS / 'README.md').write_text('\n'.join(lines), encoding='utf-8')

if __name__ == '__main__':
    main()
