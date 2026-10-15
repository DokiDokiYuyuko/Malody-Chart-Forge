# 谱面检查与交付 · Chart quality and delivery

目标 NPS、模型条件和难度标签是请求，最终 MCZ 的实际音符才是验收对象。分别记录结构检查、音乐试听、游戏目录导入回读和真人试玩。

## 现有规则

| 模块 | 用途 | 使用范围 |
| --- | --- | --- |
| `micro_chord_alignment.py` | 保守处理多押音符头的微错位，保护错落节奏、同轨近邻和跨越中的长按 | 固定 120 BPM 导出时钟；按具体候选检查 |
| `lane_burden.py` | 检查短时单轨负担，优先安全换轨，必要时有限删 tap | 可选的舒适度规则；不适用于有意要求的 Jack 等重复谱型 |
| `density_lift_guard.py` | 当任务授权复用同录音的已有模型音符时，限制多个时间尺度的相对密度增幅 | 校验辅助模块；不负责生成或自动补音符 |

这些模块不代表网页生成结果已自动执行全部后处理。公共导入工具默认原样校验和导入；显式选项才在独立暂存区执行多押对齐／单轨规则。原曲、源 MCZ 和历史采用版不改写。音符头只能来自模型；禁止声学补音、造和弦凑密度。每次修订仍须检查音符来源、长按、局部负担、最终密度及可回退性。

## 检查最终 MCZ

```powershell
.\.venv\Scripts\python.exe tools\import_chart_packs.py 'outputs\deliveries\song.mcz' --dry-run
```

工具独立读取经典 `0/` 包结构，核对四轨 MC、音轨引用、OGG Vorbis、封面解码、作者、排序、音频时间范围与轨道冲突，输出音符头／长按数、轨道分布、平均 NPS 和 1 秒峰值。平均 NPS 的分母为首个头到最后一个头／长按尾的跨度，最短按 1 秒；不是整首音频时长。该数值与物量约束一起查看，不能代替听音。

可选规则只支持固定 120 BPM 的导出时钟；这不是歌曲的真实 BPM。先用完全相同的选项做 dry-run，核对修正后的密度、档位差和局部节奏，再导入。导入工具不会自行判断某曲是否满足用户的物量目标，也不会自动选择历史尝试版。

## 明确选择成品再导入

单曲使用上面的 MCZ 路径。批量使用显式交付清单：

```json
{
  "schema": 1,
  "deliveries": [
    {"id": "recording-001", "path": "song.mcz", "sha256": "<actual MCZ SHA-256>", "approved": true}
  ]
}
```

`path` 相对清单所在目录。`approved` 记录用户或 Codex 已完成选包的决定，不能代替质量证据。为同一录音保留稳定 `id`，不同录音使用不同 `id`。工具只导入列出的包，不递归扫描工作目录。

```powershell
.\.venv\Scripts\python.exe tools\import_chart_packs.py 'work\batch\deliveries.json' --dry-run
.\.venv\Scripts\python.exe tools\import_chart_packs.py 'work\batch\deliveries.json' --chart-dir 'D:\SteamLibrary\steamapps\common\MalodyV\chart'
```

游戏目录必须替换为本机实际位置。导入器使用 `Startrail-<id hash>` 文件夹，重复导入会比较实际文件字节。替换前将旧文件夹保存到 `outputs/import-history/`，不会自动删除；可用 `--history-dir` 指定游戏扫描目录之外的位置。单独导入 MCZ 时，默认以包内音轨的哈希识别录音。可选 `--align-micro-chords`、`--limit-lane-burden` 只修改导入副本；其读回报告包含修正摘要。最终游戏文件可能因此与源 MCZ 的谱面字节不同。

导入后复核游戏文件；选歌页可按 F5 重新扫描。文件回读成功仍不等于已在游戏中打开或真人试玩通过。

## 批量案例

本地已有 75 首和新增 45 首，共 120 首成品包。新增批次的 45 首／90 张 Expert、Master 谱面在 2026-10-14 完成了游戏目录回读。该批次最高平均 NPS 为 Expert 11.949、Master 14.949，最小档位差 1.004；7 首在已有模型结果上作了有限规则修正。它说明批量流程已有实际运行记录，不是对任意歌单、音乐准确性或真人手感的保证。个人音频、封面、曲包和任务清单不随仓库发布。

## English

Evaluate the final MCZ, separating structural checks, listening, installed-file readback and human playtesting. The optional fixed-120-BPM alignment and lane-comfort rules operate on staging copies, preserve source packages and require review. They are not universally applied by the web generator; intentional Jack patterns need their own policy. Model-note reuse guards do not generate new heads.

Use `tools/import_chart_packs.py <final.mcz> --dry-run` to inspect a package. For multiple songs, explicitly list approved, SHA-256-matched deliveries using the manifest above. Specify the actual `--chart-dir` to install. Reimports compare real file bytes and preserve replaced folders in `outputs/import-history/`. NPS uses the playable-head-to-last-head/hold-tail span, with a one-second minimum. The tool does not enforce a user's density targets or prove musical quality.

The local production case contains 120 song packages. The latest 45-song/90-chart batch has installed-file readback records; it is not a human-playtest claim. Personal media and task records are excluded from the repository.
