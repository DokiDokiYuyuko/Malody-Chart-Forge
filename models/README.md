# 本地模型权重登记

所有制谱权重保存在项目根目录下的 `models/`；不应将权重文件提交到 Git。源码位于 `vendor/`，运行环境位于 `runtime/`，下载与安装缓存位于 `cache/`。

## 目录

```text
models/
  mug-diffusion/v1.0.0/      # 已部署的 MuG 基线
  mapperatorinator/
    v32-mania/             # 四键生成专用模型
    v32-timing/            # 基础模型，用于自动节拍生成
```

每个目录的 `manifest.json` 记录下载来源、版本、文件字节数和 SHA-256。V32 只下载推理需要的权重和配置，未下载训练数据。

## 权重与版本

### mapperatorinator/v32-mania

- 来源：https://huggingface.co/OliBomby/Mapperatorinator-v32
- 仓库版本：`74f22583400d259bf424819e11027c17933efe54`
- 权重子目录：`gamemode=3`

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| config.json | 2972 | `38486bbba93ba1f8b2701ade96d9b356d4e5d57e5b6fdd4ec118c0f03af198db` |
| generation_config.json | 259 | `10b901beeb3c982c16c53cd5b37a4f9b3d4674fc70cbc3e64c7b20fdb0433f51` |
| model.safetensors | 865900700 | `3d5d1c2a01ad462bcc99bafaaf44174bc47be099724574b17cd68016be5a863f` |
| tokenizer.json | 4976412 | `737148191316146ba76b6b7b0c59646baff6002ef834734dd113a8eb1e5036da` |

### mapperatorinator/v32-timing

- 来源：https://huggingface.co/OliBomby/Mapperatorinator-v32
- 仓库版本：`74f22583400d259bf424819e11027c17933efe54`
- 权重子目录：`仓库根目录`

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| README.md | 186 | `63ff96745dfd3d32d130def31e37d4a5c013412a673aacb7d102171e4a3aa1dc` |
| config.json | 2972 | `38486bbba93ba1f8b2701ade96d9b356d4e5d57e5b6fdd4ec118c0f03af198db` |
| generation_config.json | 259 | `10b901beeb3c982c16c53cd5b37a4f9b3d4674fc70cbc3e64c7b20fdb0433f51` |
| model.safetensors | 865900700 | `a79fd39a72f2ae814b397f8b4c991fac0b5faa9fbaca3f8f0df91289b23b9707` |
| tokenizer.json | 4976412 | `737148191316146ba76b6b7b0c59646baff6002ef834734dd113a8eb1e5036da` |

### mug-diffusion/v1.0.0

- 来源：https://huggingface.co/ayousanz/Mug-Diffusion-model
- 仓库版本：`镜像最初下载未固定提交；按下列哈希核实`
- 权重子目录：`仓库根目录`

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| model.ckpt | 1839231053 | `af6ab91337d0ef6b518367082ac3f849448c6daaa01fd987678fb25ea44ca184` |
| model.yaml | 3859 | `aa70281a92f57e3abfed75e78d2bd455ed206dccd660b0893c7318abc3b26901` |

## 架构初始化配置（没有下载 Whisper 权重）

| 配置来源 | 版本 | SHA-256 |
| --- | --- | --- |
| openai/whisper-small / config.json | `973afd24965f72e36ca33b3055d56a652f456b4d` | `e6a2b489da1b5aed65a8eb8d1e7466fa867ad5643a8bc138ba708bd56b2875c4` |
| openai/whisper-base / config.json | `e37978b90ca9030d5170a5c07aadb050351a65bb` | `a153c53883a6799b6f056b4a8d1a515c9926d03994682ba88a7616618d7da0c1` |

配置备份位于 `mapperatorinator/backbone-configs`。每次启动从已核实备份恢复项目内缓存，清理 cache 后仍可离线初始化。

## 部署

- V32 源码提交：`0e2b0e387aab4b35c64b0b11b12d47578dea7587`。上游源码保持完整；本项目适配在 `tools/mapperatorinator_worker.py` 和 `malody_studio/mapperatorinator.py`。
- MuG：`.venv`，Python 3.12 / PyTorch 2.5.1 CUDA 12.4。
- V32：`runtime/mapperatorinator-venv`，Python 3.12 / PyTorch 2.10 CUDA 13.0；实际冻结版本见 `runtime/mapperatorinator-requirements-lock.txt`。Python 3.12 为本机部署选择，需以本机验证为准。
- V32 采用 BF16、SDPA、本地进程；无需 Flash Attention 或远程推理。切换到 V32 前释放 MuG 模型显存；单队列执行生成。
- V32 节拍与 mania 权重各加载一份；未部署 Mini，未下载 standard 坐标扩散模型。
- 验证记录见 `docs/v32-deployment.md`；只有完成真实推理后才写入 `mapperatorinator/deployment-ready.json`。

## 格式与来源说明

- V32 保留原始 `.osu` 于任务目录 `v32-original`，转换为 Key 4K 的 UTF-8 MC，跨 BPM 长条保留音频时间。生成的滚速效果不导出，采用固定滚速。
- 曲包统一含 OGG Vorbis / 44100 Hz / stereo，MC 标注实际 AI 引擎。手机导入与手感不由格式校验代替。
- Mapperatorinator 代码及 Hugging Face 模型卡标注 MIT；原始声明见 `vendor/Mapperatorinator/LICENSE` 和模型来源。MuG 的声明见 `docs/model-selection.md` 与上游 README。音乐另行保留其来源。

## 维护

`tools/download_mapperatorinator.py` 下载 mania 权重，`--base` 下载节拍模型；下载可重试并校验公开 LFS 哈希。`tools/update_model_registry.py` 根据清单重建本文档。更换权重时使用新版本目录，固定版本与哈希并重新验证后再启用。

## 人声分离（独立环境）

`separation/demucs-4.0.1/` 保存官方 `htdemucs` 模型、模型bag配置、MIT许可证和完整SHA-256登记。默认权重为 `955717e8-8726e21a.th`；来源 `https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/`，代码固定Demucs4.0.1。运行环境为项目内 `runtime/separation-venv`：Python3.10.19、Torch2.0.1+cu118、Torchaudio2.0.2+cu118、NumPy1.26.4；实际完整锁位于 `runtime/separation-requirements-lock.txt`，不改现有MuG/V32环境。首次真实推理发现新版einops与旧Torch初始化不兼容，现固定einops0.7.0、setuptools70.3.0并已通过真实分离。

安装工具 `tools/setup_separation.ps1`；加 `-FineTuned` 可部署可选 `htdemucs_ft` 四模型bag，首轮默认未安装该慢档。权重和配置的校验与真实推理验证分别登记；默认实际验证记录见 `cache/separation-acceptance.json`。

2026-10-03在本机RTX4080 SUPER上，290.112秒原曲已完成默认GPU分离：12,793,940源帧，两stem精确同帧数、双声道FLOAT44100、origin0；总耗时57.17秒（worker52.985秒），Torch allocator峰值allocated573,086,208字节/reserved725,614,592字节。这是单次记录，不是性能保证，也不代表机器总显存使用。28–44秒两stem之和相对原混音的局部相关峰lag为0samples，相关系数0.99943；原曲WAV哈希未变。此检查不代表每个声部的起音/人声识别均正确，尚需听辨及制谱验收。

分离只用于采音/分析/试听；伴奏浮点峰值可超过1，保留原模型幅度而不单stem归一化。成品依旧从原曲PCM组装，详细版本/融合规则见 `docs/stem-generation.md`。

### Kim Mel-Band RoFormer（试验质量档）

独立运行在 `runtime/roformer-venv`（Python 3.10、Torch 2.5.1+cu124），不替换 Demucs、MuG 或 V32 的依赖。MSST 推理代码固定于 `e247dfe4abc1f17c69dff719207fe045dc04413a`，权重固定于 KimberleyJSN/melbandroformer revision `ac9b0614ab3cd7f77219e18ba494dfd93956c348`，文件 `MelBandRoformer.ckpt` 为 913106900 字节，SHA256 为 `87201f4d31afb5bc79993230fc49446918425574db48c01c405e44f365c7559e`。代码和权重来源声明 MIT，原许可证随配置保留。

窗口固定为 352800 采样（8 秒），覆盖 2/4/8 次；默认 4 次、batch 1、CUDA AMP、关闭 TTA。伴奏在任何试听增益之前以原曲减人声计算，保留完整立体声 PCM 帧数。CUDA 不可用或显存不足会明确失败。

2026-10-04 本机 4080 SUPER 全曲测试：D/N/A 129.59 秒，2/4 次覆盖含队列与启动耗时 22.58/28.61 秒；《初音未来的消失》290.11 秒，31.59/43.89 秒。四次运行整块 GPU 峰值占用不超过 5908 MiB，最少剩余 10468 MiB（外部每 3 秒采样；不是绝对瞬时峰值）。worker 自身峰值 reserved 2.04 GiB。安装、局部试听与采用步骤见 [分离实验室指南](../docs/separation-upgrade-guide.md)。质量档仍需逐片段试听判断，不能以重组误差接近零替代分离质量评价。
