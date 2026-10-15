# 文件保留与单曲排查 · Storage and diagnostics

普通台、高级台、CLI 和未指定该参数的 API 请求，均默认 `retain_debug_artifacts=false`。界面中的“保留调试文件”用于单曲排查。

## 保存与清理

- 原始音乐，以及高级项目用于采样时钟的规范原曲 PCM。
- 不可变谱面版本、采用映射、历史 MCZ、封面和游戏用 OGG。
- 实际解析的生成参数、种子、源身份、任务状态和简要质量／错误摘要。
- 后续规则迭代仍需要的谱面缓存。

生成结束或失败后，清理已经消费的模型输入、解码、试听、拼接等临时 WAV，以及详细声学诊断和 JSONL 调试轨迹。高级批次的共用声部必须等最后一个使用它的任务结束后才能清理。未消费的独立分离结果继续保留。

再次生成时可按保存的设置从原曲重建声部。分离后的试听音轨已清理时，需要重新分离。临时试听 WAV 在响应结束后删除。原曲、历史曲包、模型权重与运行环境继续保留。

清理摘要保存在任务 `artifact_retention` 和 `storage-retention.json`。被占用的文件通过 `storage_warning` 记录清理提示，制谱状态单独显示。

## 保留单曲调试文件

保留旧结果，创建独立重试：

```http
POST /api/jobs/<job_id>/regenerate
Content-Type: application/json

{"retain_debug_artifacts": true}
```

也可在新 CLI 请求中开启：

```powershell
.\.venv\Scripts\python.exe tools\generate.py --audio 'data\music\track.flac' --title 'Track' --engine v32 --retain-debug-artifacts
```

高级请求使用 `settings.retain_debug_artifacts=true`，普通上传使用同名表单字段。调试重试沿用原任务设置，保存新诊断并保留旧结果。排查后可关闭开关，后续任务恢复自动清理；已保留的调试文件可按需手动整理。

## English

Production defaults to `retain_debug_artifacts=false` across both studios, CLI and API. Original music, normalized source PCM, chart versions, adopted results, packages, parameters and summaries are saved. Consumed intermediate WAVs and detailed diagnostics are cleaned after success or failure. Shared stems stay until their final queued consumer finishes; later generation can reconstruct and verify them using the saved settings.

Enable debug retention for an independent single-song retry, using the API or CLI examples above. New diagnostics and existing results are retained. Turn the option off after investigation to resume automatic cleanup for later tasks. Previously retained diagnostics can be managed manually.
