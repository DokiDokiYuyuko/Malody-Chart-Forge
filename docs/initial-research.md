# Malody 旧版手机四指 4K 自动制谱：首轮调研

调研日期：2026-10-01。目标已由用户明确：Malody 旧版、手机四指、输入音乐后生成多个难度的 4K 谱面。本轮只做资料与源码调研；未编写或安装 skill，未安装模型，未生成谱面，未进行游戏内导入测试。

## 1. 当前判断

技术上有明确的实现路线。建议把未来的 skill 做成“音频分析与制谱程序的调度入口”，配合可复用脚本、谱面格式参考和制谱规则。

第一优先候选是 **MuG Diffusion + 独立节拍校准 + 手机四指规则检查 + Malody 原生导出**；同时保留 **音频特征 + 规则编排** 的轻量路线用于对照与回退。选型尚未定案，需要实际歌曲和手机试玩验证。

必须区分四种成功：文件格式合法、游戏能导入、音符与音乐对齐、不同难度有合理手感。前两项不能证明后两项。

## 2. Malody 与输出格式：已核实到什么程度

旧版开发者的应用介绍明确列出 `.mc`、osu、sm、bms 等格式支持，并提供内置编辑器。这说明可以从文件生成与导入入手，不必把自动点击手机编辑器作为核心实现。[旧版开发者应用介绍](https://apps.apple.com/us/app/malody/id989630809)

已检查两个 Key 模式转换器：它们均读取 `.mc` 的 JSON，使用 `meta.mode = 0` 判断 Key 模式，读取 `meta.mode_ext.column` 作为轨道数，使用 `column` 定位音符，以 `endbeat` 区分长条。[Jakads 转换器源码](https://github.com/Jakads/malody2osu/blob/master/convert.py)、[另一转换器源码](https://github.com/Siflorite/osu-mania-editor/blob/main/malodyFunc.py)

对本项目的格式候选约定：

| 内容 | 当前源码证据 | 后续确认方式 |
| --- | --- | --- |
| 模式与键数 | `meta.mode = 0`、`meta.mode_ext.column = 4` | 旧版编辑器导出一个 4K 样本 |
| 四条轨道 | 音符 `column` 使用从 0 开始的编号 | 验证 0、1、2、3 的实际显示 |
| 节拍坐标 | `beat = [整数拍, 分子, 分母]`，数值为整数拍 + 分子/分母 | 短样本逐拍核对 |
| BPM | `time` 数组中的 `beat` 与 `bpm` | 验证多段 BPM 与边界 |
| 长条 | 起点 `beat`，终点 `endbeat` | 验证尾部与下一音符衔接 |
| 背景音乐 | `note` 内带 `sound`、`type: 1`、`offset` 的事件 | 验证音频引用、起始偏移 |
| 曲包 | `.mcz` 作为 ZIP 容器保存谱面与音频 | 对照旧版原生导出的目录结构 |

注意：**拍坐标的整数部分不能直接当作小节数**。转换器计算的是 `beat[0] + beat[1]/beat[2]`。例如 `[4, 1, 2]` 为 4.5 拍；在 BPM 120 下，相对拍零经过 2250 毫秒。小节与拍的关系需要结合拍号处理。[节拍转换实现](https://github.com/Jakads/malody2osu/blob/master/convert.py)、[分数拍序列化实现](https://github.com/Stepland/jubeatools/blob/master/jubeatools/formats/malody/dump.py)

背景音乐事件与可击打音符混在 `note` 数组里，解析时必须按字段识别，不能把每条记录都计入物量。长条也不能误写成“`type: 1` 就是长条”：在已读旧版格式实现中，`type: 1` 用于背景音乐事件，Key 长条由 `endbeat` 表示。[事件 schema](https://github.com/Stepland/jubeatools/blob/master/jubeatools/formats/malody/schema.py)、[Key 转换实现](https://github.com/Jakads/malody2osu/blob/master/convert.py)

偏移符号也需要专门核对：Jakads 的实现把音频事件 `offset` 取负作为拍零时间；jubeatools 的导出实现也对拍零偏移取负。初步推导：当背景音乐事件位于 `[0,0,1]`、首个 BPM 位于拍零时，固定 BPM 下 `音符时间毫秒 = 拍数 × 60000/BPM - offset`。此式应先用旧版原生样本验证，再写入导出器；更一般的音频事件位置需另行计算。[偏移序列化源码](https://github.com/Stepland/jubeatools/blob/master/jubeatools/formats/malody/dump.py)

`.mcz` 的 ZIP 容器属性可由转换器与 MugGen 作者说明交叉确认；具体顶层目录不能凭 Malody V 的教程套用到旧版，也不能把改扩展名本身当作格式转换。[转换器解包代码](https://github.com/Jakads/malody2osu/blob/master/convert.py)、[MugGen 作者说明](https://github.com/coralr-1/MugGen-An-Rcnn-embedding-model-for-automated-beatmap-generation-in-rhythm-games)

资料边界：旧版官方导入 Wiki 在本轮访问返回 403。上述字段依据公开实现交叉核对，属于格式实现证据，尚不是官方规范加旧版实机测试的完整确认。

## 3. 可选的制谱引擎

### A. MuG Diffusion：最贴近需求，优先做实测候选

作者提供的功能包括：从音频生成 4K 下落式谱面；控制 osu 星级或 Etterna MSD、风格、长条比例以及若干键型；风格选项包含 Malody stable。注意这里的风格标签不代表产物获得了 Malody Stable 认证。[项目说明](https://github.com/Keytoyze/Mug-Diffusion)

源码核对结果：

- WebUI 保存的是 `.osu`，打包得到 `.osz`；不能据“Malody 风格”推断它直接输出 `.mc/.mcz`。
- 支持 CUDA，并有 CPU 回退；用户机器的速度与可运行性仍未测试。
- 包含固定随机种子、移除部分过密同轨连击、谱面预览等可复用思路。
- 依赖表多为未锁定版本；实施前应建立隔离环境并验证兼容组合，不能假定安装所有最新版即可运行。

[输出与设备选择源码](https://github.com/Keytoyze/Mug-Diffusion/blob/master/webui.py)、[依赖列表](https://github.com/Keytoyze/Mug-Diffusion/blob/master/requirements.txt)

关键局限：其 `gridify` 从生成的音符时间估计一组 BPM/offset 并做吸附，这不等于独立确认了原音乐的节拍。项目中还有多 BPM 歌曲 timing 错误的用户报告，以及作者列出的钢琴音符对齐问题。它们是需要测试的线索，不是对所有歌曲的性能结论。[量化源码](https://github.com/Keytoyze/Mug-Diffusion/blob/master/mug/data/utils.py)、[多段 timing 报告](https://github.com/Keytoyze/Mug-Diffusion/issues/18)、[钢琴对齐问题](https://github.com/Keytoyze/Mug-Diffusion/issues/6)

因此建议：把模型当作候选谱面生成器，音频时间基准、难度复评和最终格式检查独立完成。初测时先比较原始输出与重新量化输出，避免强制吸附把正确的切分、摇摆或装饰音改错。

项目英文 README 与中文 README 的使用声明存在差异：英文版额外写有权重与生成谱面的非商业条件。若以后打包分发这个 skill，需要分别确认代码、权重、产物的条件；本轮不作法律结论。[英文 README](https://github.com/Keytoyze/Mug-Diffusion/blob/master/README.md)、[中文 README](https://github.com/Keytoyze/Mug-Diffusion/blob/master/README_CN.md)

### B. 音频特征 + 规则编排：解释性更强的对照路线

librosa 提供节拍跟踪等基础能力；Beat This 可输出节拍与小节起点，支持 CPU/GPU，适合作为另一种节拍候选。它们只解决音乐分析中的部分问题，均不等于完整制谱器。[librosa 节拍文档](https://librosa.org/doc/main/api/generated/librosa.beat.beat_track.html)、[Beat This 作者仓库](https://github.com/CPJKU/beat_this)

建议的规则路线：提取节拍、起音强度、频段变化、持续音与段落重复关系，构建带置信度的音乐事件，再按难度选择事件与分配轨道。优势是可追溯、能解释为什么在某处放置音符；风险是复杂旋律表达与键型变化需要长期打磨。以上是工程判断，尚未对本用户歌曲做效果比较。

需要注意节拍检测的半速/倍速歧义、弱起、静音前奏、无鼓点段落，以及检测误差造成的假变 BPM。不能把相邻检测节拍的所有微小波动直接写成 BPM 变化。

### C. MugGen：研究参考，暂不作为首选

作者公开了直接生成 Malody 4K 的方案，但 README 的运行流程包含准备谱面数据与训练模型，难度、键型控制列为未来方向；本轮未找到可直接替代 MuG Diffusion 的完整预训练交付流程。[MugGen 仓库](https://github.com/coralr-1/MugGen-An-Rcnn-embedding-model-for-automated-beatmap-generation-in-rhythm-games)

检查 `main.ipynb` 后，当前导出函数可作为旧版 JSON 字段的补充样例，但其音符导出只见 `beat/column`，未见长条 `endbeat`；节拍估计与序列化也需要重新验证。适合参考，不宜直接复制为可靠导出器。[生成 notebook](https://github.com/coralr-1/MugGen-An-Rcnn-embedding-model-for-automated-beatmap-generation-in-rhythm-games/blob/main/main.ipynb)

## 4. 旋律与长条分析：作为可选增强

Demucs 可分离鼓、贝斯、人声和其他伴奏，但原 facebookresearch 仓库已于 2025-01-01 归档。若采用，需要评估维护版本与运行环境。建议先作为复杂歌曲的可选增强，所有分离结果都与原音频时间轴核对。[Demucs 作者仓库](https://github.com/facebookresearch/demucs)

Basic Pitch 可把音频转成 MIDI/音符事件，作者明确说明它在单乐器输入下表现最好。它更适合用于较干净声部的起音与持续时间候选，不能假定整首混音能准确转录后直接映射四轨。[Basic Pitch 作者说明](https://github.com/spotify/basic-pitch)

本项目的设计判断：4K 谱面是对音乐的可玩编排。音高走势可以影响轨道走向，持续音可以支持长条，但不必逐音完整转录，也不应简单把 MIDI 音高对 4 取模作为最终键型。

## 5. 多难度如何生成

建议先保留完整的音乐事件层，再分别编排各难度，建立共同的节奏主题与高潮结构。降难度时按乐句保留主节奏、删去次要声部并重新分配轨道；随机删除高难谱音符不能保证节奏连贯。

初版可研究以下四档，但名称与参数尚未确定，更未对应 Malody 数字 Lv：

| 难度候选 | 音乐表达 | 手机四指编排重点 |
| --- | --- | --- |
| Easy | 强拍与清晰主节奏 | 单键为主，宽松同轨间隔，简单长条 |
| Normal | 主节奏加部分旋律 | 增加交互与双押，保留休息段 |
| Hard | 更多旋律细节与鼓组 | 控制连续流、叠键和长条占轨时的负荷 |
| Expert | 更完整的音乐层次 | 允许较复杂键型，但局部难点需符合目标强度 |

以上是拟定规则，不是 Malody 官方难度标准。可参考 osu!mania 的音乐对应与难度分层原则，但不能直接照搬其尾判规则、数值门槛或认证要求。[osu!mania 官方制谱标准](https://osu.ppy.sh/wiki/en/Ranking_criteria/osu!mania)

建议记录以下指标辅助复评：活动段平均与短窗口峰值物量、每轨击打间隔、双/三/四押比例、同轨连击长度、左右手负荷、长条占轨时间、连续高负荷段长度、休息比例。NPS 需要明确统计窗口与分母；平均物量相同的谱面可能在同轨连击或长条协调上差异很大。

osu 星级与 Etterna MSD 可作为辅助尺度，不能直接改名成 Malody Lv。MinaCalc 有可独立调用的实现，但原生编译与版本固定仍需验证；首次定级应结合参考谱与用户试玩，数字等级标为估计。[MinaCalc 作者仓库](https://github.com/kangalio/minacalc-standalone)

对手机四指，拟默认左手负责 0/1 轨、右手负责 2/3 轨，但必须允许按实际指法调整。四指可以处理四押，不应套用拇指玩法的“两键上限”；低难档仍应限制复杂同时按键和长条期间的协调负荷。第一版建议把特殊视觉变速设为可选风格，避免它干扰节拍与手感验证。

## 6. 未来 skill 的工作流程

拟定的数据流：

```text
用户音乐文件与难度要求
  → 音频检查与解码
  → 节拍 / 偏移 / 段落分析
  → 候选制谱：MuG Diffusion 或规则引擎
  → 多难度编排与手机四指修正
  → 格式、时间、轨道占用与难度检查
  → .mc 谱面 + 音频 → .mcz 曲包
  → 预览、校验报告与手机试玩
```

内部建议使用“音符开始毫秒、结束毫秒、轨道、声部/依据、置信度”的统一事件结构，另存节拍图。只有导出时才转换成 Malody 分数拍坐标。这能避免音频分析、模型输出与序列化各自采用不同 offset 约定。

音频分析副本可以重采样或转单声道，但输出游戏音频必须核对其时间轴。去静音、裁切、重新编码可能改变起始位置，所有音符和偏移须同步处理。首版优先研究完整本地音频，歌曲链接下载属于另一个输入适配问题。

skill 入口负责理解用户要求、选择后端、执行检查、说明输出与不确定段落；脚本负责稳定的数值运算和文件处理。拟定资源仅包括必要内容：

- `SKILL.md`：适用范围、旧版手机四指默认项、工作流程与验收要求。
- `scripts/`：音频分析、候选生成适配、谱面检查、Malody 导出与打包。
- `references/`：经实测确认的旧版格式、难度规则、模型依赖与限制。

这些只是结构建议，本轮尚未创建 skill 文件。

每次最终交付建议包含一个多难度 `.mcz`、各难度 `.mc`、谱面预览和简短分析报告。报告记录 BPM、拍零偏移、物量、难度估计、随机种子、生成后端和低置信度时间段。模型生成来源应保留在随包说明中，避免格式转换后丢失。

## 7. 首轮验证应怎样安排

以下是后续实验计划，本轮尚未执行：

1. **格式基准**：从用户实际旧版编辑器导出含四轨单键、双押、长条的短谱；确认音频事件、偏移、编辑器附加字段、包内目录。再做有意改变 BPM 与 offset 的样本，核对符号与单位。
2. **独立时间基准**：用已知点击时刻的短音频检查拍坐标转换；添加非零偏移、多段 BPM、三连音及跨 BPM 的长条。正确性应相对已知时刻验证，不能只做导出再解析的自洽检查。
3. **模型可运行性**：确认权重可取得、依赖能在目标电脑运行；测一首歌曲的耗时、内存与失败行为，不直接采用作者机器的速度估计。
4. **歌曲分组比较**：固定 BPM 电子乐、带静音前奏的流行歌、钢琴/持续音歌曲、真实变 BPM 歌曲。比较轻量规则与模型方案，重点听检开头、中间、末尾和变化边界。
5. **多难度验证**：先研究三档，核对总体难度是否递增、低难是否保留音乐主题、有无局部反而更难的键型。
6. **游戏内验收**：确认可导入、四轨正确、音频完整、开头与结尾不漂移、长条可正常游玩；只有完成手机试玩，才判断手感是否达标。

程序应检查：分母非零、BPM 为正、轨道仅 0–3、音符时间排序、同轨同刻重复、负长度长条、同轨长条重叠及内部 tap 冲突、音频引用可解析、音符超出有效音频范围。长条结束同刻接键的规则需要按旧版实测定义。音频事件单独校验，不计入可击打音符。

节拍不可靠时，应保留候选时间和置信度，输出可修正的草稿；不能静默用默认 BPM 冒充分析成功。重试应有上限，保留失败原因与中间结果。

## 8. 尚待明确的事项与下一阶段重点

已明确：旧版 Malody、手机四指、4K、多难度，当前阶段仅调研。

仍待实施前明确：Android 或 iOS、具体旧版版本、期望难度范围、长条偏好、能否提供参考谱，以及运行模型的电脑是否具备合适显卡。现在这些信息不妨碍继续格式与算法调研。

下一阶段优先顺序：**旧版原生样本与曲包格式 → MuG Diffusion 权重/环境验证 → 同曲多难度和手机手感对照 → 形成 skill**。主要研究风险是节拍/offset、复杂混音表达、难度校准和手机试玩反馈，文件写出本身不是最大的难点。

当前结论的强度：已确认文件生成路线与相关工具能力，有源码支持的候选方案；尚未证明在用户实际旧版与歌曲上能稳定产生好玩的谱面。
