# Release notes · 更新记录

## 歌单任务与可玩 Demo — 2026-10-15

- 推荐通过 Codex + `malody-4k` Skill 按歌单安排制谱任务，支持选曲、音源准备、批量生成和成品整理。
- 新增支持断点恢复的批量提交工具，以及可指定 Malody V 目录的曲包导入工具。
- 新增可选的多押微错位对齐、单轨负担检查和已有模型音符的密度检查。
- 自动清理用完的中间文件，支持为单曲保留调试文件。
- 修复局部节拍网格被拒绝时的强拍记录处理。
- 新增六首可玩 Demo，包含 Expert / Master 共十二张谱面。
- README 内嵌每首 30 秒、×3 流速、60 FPS 的视频预览，配有 Malody V `hit-4` 按键音。
- 更新中英文使用文档、安装说明与服务接口参考。

Codex playlist workflow, resumable batch submission, configurable Malody V import, optional chart checks and automatic temporary-file cleanup. Six playable demos include Expert / Master charts and embedded 30-second video previews.

## 桌面制谱流程更新 — 2026-10-08

- 独立音乐下载页面，支持 YouTube 搜索、B站链接解析和 YouTube 登录。
- V32 支持每档独立设置 NPS 范围与生成条件。
- 高级台支持区域规划、声部分离、局部重做和 A/B 候选比较。
- 改进波形、频谱与四轨的同步播放，完善谱师署名和曲包导出。
- 统一简易台与高级台的曲包库，支持分页、多选删除和任务历史。
- 新增主题与桌面界面截图。旧版源码见 [`v1`](https://github.com/DokiDokiYuyuko/Malody-Chart-Forge/tree/v1)。

Desktop workflow updates cover music downloads, independent V32 difficulty targets, Advanced region editing, stems, A/B review, synchronized playback, chart-author credits and the unified package library.
