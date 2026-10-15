# Release notes · 更新记录

## Codex playlist tasks and production delivery — 2026-10-15

- Recommend using Codex with the published `malody-4k` Skill to arrange playlist tasks; keep the web studios focused on manual authoring, listening and candidate review.
- Publish a reusable Advanced batch submitter with frozen manifests, durable intent/receipts, serial polling and recovery without automatic resubmission of uncertain or failed requests.
- Add a portable classic-4K delivery importer: explicit package selection, source-hash checks, artwork/artist validation, independent file readback, configurable game directory and preserved import history.
- Publish conservative micro-chord alignment, single-lane comfort checks and local density-reuse guards. Import-time correction is opt-in and preserves the original MCZ; it is not a claim that all web generation runs these rules.
- Default production to compact artifact retention, including shared-input lifecycle, transient preview cleanup and per-song debug retries that preserve frozen input identity and adopted versions.
- Keep observed downbeats when a provisional local tempo grid is rejected, instead of retaining its projected anchors as timing evidence.
- Add bilingual playlist, storage and quality guides, an API reference, and tests for batch recovery and delivery import. Keep personal task controllers, music, packages and one-off storage maintenance local.
- Document the local 120-song production case separately from listening and human-playtest claims.

推荐由 Codex 结合 Skill 根据歌单提交自动任务，减少逐首在前端操作。公开可恢复的批量提交、可配置的成品导入和可选质量规则；补齐默认中间文件清理及单曲调试说明。网页继续用于试听、试玩、编辑和 A/B 复核；不新增歌单按钮或预先启用定时任务。

## Current desktop workflow — 2026-10-08

- Separate music downloads from generation; add Bilibili resolution and dedicated YouTube login.
- Replace new V32 density routing with independent per-difficulty NPS ranges, frozen BPM buckets and versioned NPS-to-SR calibration.
- Update Advanced processing, region planning, stem generation, immutable candidates, local redo and A/B adoption.
- Improve synchronized waveform/spectrum/gameplay transport, chart-author metadata and export handling.
- Unify Simple/Advanced delivery browsing, pagination, multi-delete and completed-task history.
- Preserve historical retry policies and original audio/chart versions; distinguish density, musical review and actual game evidence.
- Refresh bilingual README, workflow diagram and desktop screenshots using synthetic demo data.
- Remove internal development reports, old screenshots, local readiness records and personal defaults from the current public tree.
- Preserve the previous GitHub main commit as the `v1` branch.

新版更新包括独立音乐下载、V32 各档 NPS 范围与 BPM 条件、高级台区域工作流、分轨与 A/B、同步试听试玩、署名和导出、统一曲包库与历史。
旧版源码保存在 `v1`。当前说明只记录公开产品变更；本地实验记录不作为发布文档。
