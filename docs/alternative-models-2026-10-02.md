# 其他 4K 制谱模型调研

调研日期：2026-10-02。目标：音乐输入 → 多难度 → 旧版 Malody 4K，手机四指。

## 结论

最值得加入实测的是 **Mapperatorinator V32 的 mania 专用权重**；Mini 版是第二个候选。MuG Diffusion 保留为已经跑通的基线。此次未找到针对这些模型、面向 Malody 手机四指的同条件公开质量评测，所以不能把部署优先级写成质量排名。

之前选择 MuG 的理由是先完成可用部署。补充调研后，Mapperatorinator 应进入正式对比名单。输出 osu!mania 并不妨碍最终生成 Malody，但格式转换、手机手感和模型质量必须分别验证。

## 候选与可用程度

| 方案 | 已核实的情况 | 本项目判断 |
| --- | --- | --- |
| MuG Diffusion | 当前项目已完成实际音乐、多难度推理和 MCZ 格式检查 | 基线；尚未完成手机试玩，不能称为质量冠军 |
| Mapperatorinator V32 | 音频条件生成，公开 mania 权重；可设四键、难度、长条比例 | 第一实测候选，需要接入导出与手机约束 |
| Mapperatorinator V32 Mini | 公开仓库存在 mania 专用权重，文件更小 | 对比质量、耗时、显存；更小不等于质量更好 |
| MugGen | 原生 Malody 4K；运行说明包含自行训练流程，难度和键型控制列为后续计划 | 暂不替换；此次未核实即用的公开预训练权重 |
| BeatAnything | osu!mania 的 VAE / Rectified Flow Demo；推理需参考谱提供节拍和设置，说明包含自行训练两阶段 | 研究候选；此次未核实可直接使用的公开权重 |
| rhythm-chart-diffusion | 面向 4K 的研究路线；当前说明中基线在进行，扩散阶段尚未开始 | 持续关注，目前不作为即用引擎 |

来源：[MuG](https://github.com/Keytoyze/Mug-Diffusion)、[Mapperatorinator](https://github.com/OliBomby/Mapperatorinator)、[V32 权重](https://huggingface.co/OliBomby/Mapperatorinator-v32)、[Mini 权重](https://huggingface.co/OliBomby/Mapperatorinator-v32-mini)、[MugGen](https://github.com/coralr-1/MugGen-An-Rcnn-embedding-model-for-automated-beatmap-generation-in-rhythm-games)、[BeatAnything](https://github.com/cjnsasdf/BeatAnything)、[rhythm-chart-diffusion](https://github.com/qxxiit/rhythm-chart-diffusion)。以上状态描述来自公开说明，不代表已在本机跑过所有候选。

## Mapperatorinator 的接入依据

官方推理配置包含 `gamemode`、`keycount`、`difficulty`、`hold_note_ratio`，并可自动选择对应模式模型。拟使用 `gamemode=3`、`keycount=4`。这些是生成条件，不保证生成后的物量或实际难度严格符合目标。来源：[默认推理配置](https://raw.githubusercontent.com/OliBomby/Mapperatorinator/main/configs/inference/default.yaml)。

V32 配置输出 TIMING、MAP、SV，并关闭位置生成。这里不能把 osu!standard 的坐标扩散模型能力直接当作 mania 质量证据。来源：[V32 配置](https://raw.githubusercontent.com/OliBomby/Mapperatorinator/main/configs/inference/v32.yaml)。

通过 Hugging Face 文件列表 API 只读核实：

| 权重 | mania 子目录 | model.safetensors 字节数 |
| --- | --- | ---: |
| V32 | gamemode=3 | 865900700 |
| V32 Mini | gamemode=3 | 223128924 |

API：[V32 mania 文件列表](https://huggingface.co/api/models/OliBomby/Mapperatorinator-v32/tree/main/gamemode%3D3)、[Mini mania 文件列表](https://huggingface.co/api/models/OliBomby/Mapperatorinator-v32-mini/tree/main/gamemode%3D3)。文件大小不等于运行显存需求，本轮没有下载权重或执行推理。

官方目前推荐 Python 3.10、PyTorch 2.10 和 CUDA 13.0；本项目现有环境是 Python 3.12、PyTorch 2.5.1 / CUDA 12.4。后续应建独立环境和独立进程，通过任务文件调用。4080 SUPER 16GB 是否能在目标配置下满足显存与速度要求，须实测；不能仅凭参数量保证。来源：[安装说明](https://github.com/OliBomby/Mapperatorinator#installation)、[依赖文件](https://raw.githubusercontent.com/OliBomby/Mapperatorinator/main/requirements.txt)。

拟接入顺序：独立环境 → 固定源码及权重版本 → 最小 4K 推理 → 解析 osu!mania → 沿用 MC / OGG / MCZ 导出 → 网站增加引擎选择。需要重点检查多 BPM、offset、长条跨节拍段、SV 的旧版兼容性。不得无条件吸附音符或丢弃时间信息；无法表达的效果要明确记录。

## 不宜直接当作替代模型的方案

- **Pulsefield R1 restored**：有公开 4K 权重，但需要事件时间表和种子谱，不从音频推断时间。因此更接近键型续写研究，不能独立替代“上传歌曲直接出谱”。来源：[模型卡](https://huggingface.co/sed-i/pulsefield-r1-restored)。
- **BeatLearning**：说明中实际流程仍面向 standard，mania 在待办中；不能因为架构支持四轨就说已有即用 4K 制谱。来源：[作者说明](https://github.com/sedthh/BeatLearning)。
- **osu-diffusion**：主要研究已给定时间等条件下的 standard 坐标生成。来源：[作者项目](https://github.com/OliBomby/osu-diffusion)。
- **Demucs + 规则的 4K 工具**：声部分离模型和规则排键可以成为辅助路线，但不是已训练的音频到谱面生成模型。来源：[opus-4.6-mania4kmapper](https://github.com/Ad4mu/opus-4.6-mania4kmapper)。

MIREX 2026 的制谱任务面向 osu!taiko，不能作为本项目 4K 模型优劣的证明。其结合机械检查和人工评价的思路可以借鉴。来源：[赛事任务说明](https://music-ir.org/mirex/wiki/2026:Rhythm_Game_Chart_Generation)。

## 建议的实测方案（尚未执行）

选 5–8 首不同类型的音乐，包含《シリウスの心臓》、节奏明显的电子乐、慢速人声和存在节拍变化的曲目。每个引擎生成易、中、难三个等级，各保留至少两个随机种子。

1. 对齐输入：使用同一音频、同一起止时间；难度参数不能跨模型直接视为等价，应按生成结果校准物量、峰值、长条比例。
2. 检查音乐表现：听音符起点、长条起止是否跟随可听事件；检查主副歌变化、重复句型和段落衔接。音符越多不能直接算更好。
3. 检查手机四指：连续同轨负担、夹押、长条占指时的连打、左右手负担，分别记录原始谱和统一手机过滤后的谱。
4. 检查工程质量：全曲是否完成，时间与格式是否合法，MCZ 是否实际导入旧版；同时记录失败率、生成时间、峰值显存。
5. 进行隐藏模型名称的手机试玩，比较跟音乐的感觉、可读性、疲劳与难度。实际玩家评价是最终选择的重要依据。

本轮只进行了资料、配置和权重文件列表核实。没有新模型的质量分数、耗时或显存实测结果；现有网页仍使用 MuG。
