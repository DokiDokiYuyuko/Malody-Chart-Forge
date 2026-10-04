# 人声与伴奏分离：先试听，再采用

在高级制谱台打开 **分离**。原曲、人声、伴奏共用原曲时钟；试听选择与制谱输入分别保存。

1. 选择模型。**Demucs 标准**是默认快速档；**Kim MelBand RoFormer**是高质量档；**Demucs FT**用于对照。
2. 填写对比开始、结束时间，或使用当前选区。点击 **在此范围试分离**，后台保留前后各 8 秒上下文，只返回所选区间。
3. 在 A / B 选择器中选择版本和人声 / 伴奏。切换保留原曲位置；开启循环可反复比较同一段。音量匹配只降低较响一路，不改变文件或生成输入。
4. 满意后点击 **分离完整原曲**。局部结果是试听预览，不能冒充整曲声部。
5. 展开完整版本的 **版本与生成输入**，明确选择人声、伴奏或两路原谱，再前往生成窗口提交。试听不会自动采用谱面，也不会替换原音频。

## 安装 Kim 质量档

在项目根目录运行：

```powershell
.\tools\setup_roformer.ps1
```

安装器使用独立的 `runtime/roformer-venv`，保留现有制谱与 Demucs 环境。检查固定代码、配置和权重 SHA256 后才注册模型；下载成功与推理验证分别显示。必须有可用的 NVIDIA CUDA 环境，推理失败不会静默切换到 CPU。

大陆网络下可使用可信的 Hugging Face 镜像：

```powershell
.\tools\setup_roformer.ps1 -HfEndpoint https://hf-mirror.com
```

或从官方模型页另行下载文件后，指定 `-WeightsPath "本地的 MelBandRoformer.ckpt 路径"`。镜像和离线安装仍核对同一 SHA256，不接受其他权重冒充。脚本只安装项目内 Python 和 RoFormer 环境，不要求先安装 Demucs。

## 参数与数据

| 模型 | 窗口 | 重叠 | 默认 |
|---|---|---|---|
| Demucs 标准 / FT | 1–7.8 秒 | 比例 10%–75% | 7.8 秒、25%、一次随机平移 |
| Kim MelBand RoFormer | 固定 352800 采样 / 8 秒 | 覆盖 2 / 4 / 8 次 | 4 次、单窗口批量、CUDA AMP、关闭 TTA |

Kim 人声保留原始输出尺度，伴奏以 **原曲 − 人声**计算。返回 44.1 kHz 双声道，帧数与输入完全相同，不裁静音、不拉伸、不独立归一化。整曲叠加缓冲放在内存，模型计算在显卡；这不表示模型退回 CPU。

旧项目原音频与版本保持不变。新建 YouTube 项目优先使用保留的下载原文件；旧缓存没有原文件时明确记录转换音源回退。试听文件仅作防削波等比例降音量。

没有真实独立声部作为参考时，能量、相关性、重组误差都不能当作串音率或 SDR。分离听感与制谱采音需要分别评价；不要以“音符更多”代替采音合理。

## 固定来源

- [MIT MSST 推理代码与配置](https://github.com/ZFTurbo/Music-Source-Separation-Training/tree/e247dfe4abc1f17c69dff719207fe045dc04413a)
- [Kim 模型说明](https://github.com/KimberleyJensen/Mel-Band-Roformer-Vocal-Model)
- [固定权重 revision](https://huggingface.co/KimberleyJSN/melbandroformer/tree/ac9b0614ab3cd7f77219e18ba494dfd93956c348)
- [Mel-Band RoFormer 论文](https://arxiv.org/abs/2310.01809)

选择模型前可先比较本机试听与制谱结果。[完整本机验收](separation-upgrade-validation.md)包含四次全曲分离、六个重点窗口和十组同种子 V32 对照。对比音频在本地 `outputs/separation-evaluation/20261004/`；打开其中的 `listen.html` 即可同位置 A/B 试听。
