# 谱面检查与交付 · Chart quality and delivery

本页介绍最终 MCZ 的指标检查、可选后处理和 Malody V 导入工具。

## 可选后处理

| 模块 | 用途 | 使用范围 |
| --- | --- | --- |
| `micro_chord_alignment.py` | 保守处理多押音符头的微错位，保护错落节奏、同轨近邻和跨越中的长按 | 固定 120 BPM 导出时钟；按具体候选检查 |
| `lane_burden.py` | 检查短时单轨负担，优先安全换轨，必要时有限删 tap | 面向常规舒适度调整；Jack 等重复谱型可跳过单轨修正 |
| `density_lift_guard.py` | 当任务授权复用同录音的已有模型音符时，限制多个时间尺度的相对密度增幅 | 已有模型音符复用时的密度检查 |

导入工具默认校验并原样导入曲包。添加 `--align-micro-chords` 或 `--limit-lane-burden` 可在暂存副本中应用对应规则，源 MCZ 保留。修正后可查看长按、局部负担和密度的变化。

## 检查最终 MCZ

```powershell
.\.venv\Scripts\python.exe tools\import_chart_packs.py 'outputs\deliveries\song.mcz' --dry-run
```

工具独立读取经典 `0/` 包结构，核对四轨 MC、音轨引用、OGG Vorbis、封面解码、作者、排序、音频时间范围与轨道冲突，输出音符头／长按数、轨道分布、平均 NPS 和 1 秒峰值。平均 NPS 按首个音符头到最后一个音符头或长按尾的跨度计算，跨度最短按 1 秒。

对齐和单轨规则支持以固定 120 BPM 时钟导出的 MC；这里的 120 BPM 是文件时间编码。使用相同选项先做 dry-run，检查修正后的密度和档位差，再试听候选并导入。

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

`path` 相对清单所在目录。`approved=true` 表示该曲包已选入交付清单。为同一录音保留稳定 `id`，不同录音使用不同 `id`。工具只导入列出的包，不递归扫描工作目录。

```powershell
.\.venv\Scripts\python.exe tools\import_chart_packs.py 'work\batch\deliveries.json' --dry-run
.\.venv\Scripts\python.exe tools\import_chart_packs.py 'work\batch\deliveries.json' --chart-dir 'D:\SteamLibrary\steamapps\common\MalodyV\chart'
```

游戏目录必须替换为本机实际位置。导入器使用 `Startrail-<id hash>` 文件夹，重复导入会比较实际文件字节。替换前将旧文件夹保存到 `outputs/import-history/`，不会自动删除；可用 `--history-dir` 指定游戏扫描目录之外的位置。单独导入 MCZ 时，默认以包内音轨的哈希识别录音。可选 `--align-micro-chords`、`--limit-lane-burden` 只修改导入副本；其读回报告包含修正摘要。最终游戏文件可能因此与源 MCZ 的谱面字节不同。

导入后可在游戏选歌页按 F5 重新扫描，再打开曲目游玩。

## 生成条件

V32 为每档难度和条件范围独立生成。提交时记录 NPS 范围、BPM 桶、校准版本和输入；历史任务重试使用其保存的设置。

Beat This 提供音乐分析和 BPM 条件。自动 V32 timing 参考注入默认关闭；需要指定节拍参考时可使用手动 timing 设置。

NPS→SR 曲线由 `1 ≤ SR < 7` 的样本拟合，`SR ≥ 7` 使用趋势外推。目标 NPS 用于选择模型条件，实际密度在生成后按最终 MCZ 计算。

后处理使用模型生成的音符头。微错位对齐调整已有音符，保留有意错开的节奏；复用同一录音的其他模型版本时，用密度检查限制局部增幅。相关操作约束见 [制谱 Skill](../skills/malody-4k/SKILL.md)。

## English

Use `tools/import_chart_packs.py <final.mcz> --dry-run` to inspect chart structure, audio, artwork, artist credits and note metrics. Average NPS uses the span from the first note head to the last head or hold tail, with a one-second minimum.

For multiple songs, list selected packages in the manifest above with `approved=true` and their SHA-256 hashes. Specify your actual `--chart-dir` to install. Reimports compare file bytes and preserve replaced folders in `outputs/import-history/`. Press F5 in Malody V to rescan, then open a song to play.

The importer copies packages unchanged by default. Optional alignment and lane-burden flags operate on staging copies using a fixed 120-BPM MC export clock. Check the dry-run report and audition the revised chart before import. For intentional Jack patterns, leave lane-burden correction off.

V32 generates each difficulty and condition span independently using saved NPS ranges, BPM buckets and calibration versions. Beat This supplies analysis and BPM conditions; automatic timing-reference injection is disabled. The NPS→SR curve is fitted on `1 ≤ SR < 7` samples and extrapolated above that range. Measure the resulting density from the exported MCZ.
