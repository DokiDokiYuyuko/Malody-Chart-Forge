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
