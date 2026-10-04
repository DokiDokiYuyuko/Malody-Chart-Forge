---
name: malody-4k
description: 将用户提供的音乐用本机 MuG Diffusion 或 Mapperatorinator V32 生成旧版 Malody 手机四指 4K 多难度谱面，输出包含音乐的 MCZ 曲包。
---

使用 `the project root` 中已部署的服务，全部数据保存在该目录。六档为 easy、medium、hard、expert、master、lunatic，活动片段密度目标约 2.5 / 5 / 8.5 / 13 / 18.5 / 26 NPS。默认选前三档，长条目标 15%。旧参数 normal 映射 medium，勿同时传入两者。

服务地址以当前启动配置为准，本机工作台通常为 `http://127.0.0.1:8766`，旧启动默认也可能为 8765。先检查 `/api/health` 和当前队列，需要启动时运行项目 `start.ps1`。不要另外启动模型服务占用显存。

网页默认 V32，API 与 CLI 为兼容旧调用默认 MuG；明确传 `engine=v32` 或 `--engine v32` 可选 V32。先检查 health.engines 中对应引擎是否就绪。普通台使用母谱分层；高级台默认逐档独立生成，不能把普通台的分层结果描述成六份独立推理。新网页任务默认段落适配；旧参数快照缺少 dynamic_enabled 时仍走旧策略。权重位于 `models`，登记见 `models/README.md`。

高级项目保留原曲采样时钟、片段、不可变版本与手动选版。可启用人声/伴奏联合制谱：分离缓存、两份声部原谱及融合均保存在本机，融合只消费一次共享预算，导出使用原曲。不要把模型产生的 BPM 当作已验证音频 BPM。具体操作见 `docs/advanced-authoring-guide.md`、`docs/adaptive-sections.md`、`docs/stem-generation.md`。高级父任务独占生成资源，不额外排子任务导致阻塞。

用户要求从网络选曲时，可通过 `/api/music/search?q=<曲名或YouTube单曲链接>` 搜索，核对音乐人、曲名和版本，再 POST `/api/music/<id>/import`。GET 同路径读取下载状态，直到 ready 后 POST `/api/music/<id>/generate`，表单参数与上传接口一致。GET `/api/music` 可复用已下载的本地音乐。来源需要登录、限制访问或下载失败时显示原因，不使用账号 Cookie 或绕过限制。仅有相似曲名不足以认定是目标版本。

使用项目环境执行 `tools/generate.py --audio <本地音频> --title <曲名> --artist <音乐人>`。难度参数为 `--difficulties easy medium hard expert master lunatic`，MuG 精度为 `--steps 20|50|100`。仅在用户要求参考曲时使用 `--reference`。音频缺失时先查 uploads，根据用户授权寻找其他来源，不将相似歌曲替代目标曲。

在线音乐使用所选 YouTube 视频缩略图，MC 的 meta.background 引用包内 background.jpg。本地上传若有确认的对应视频，可传表单 artwork_url 或 CLI `--artwork-url <YouTube单曲链接>`；不要猜测对应视频。图片下载失败仍可生成音乐曲包，报告会说明图片缺失。记录使用 `/api/history?page=1&page_size=6&q=<曲名>` 分页查询；旧 `/api/jobs` 仅返回最近 20 条。

等待任务完成，查看生成报告中的轨道校验、音符数量、长条比例、密度与警告。任务失败时读取 `logs/<job_id>.log`，诊断后再决定重试，避免重复排队同一任务。返回脚本输出的 `.mcz` 和报告路径。

MCZ 已包含 OGG Vorbis 音频和 UTF-8 MC 谱面。保持模型时间戳和 BGM 引用一致，不擅自截去前奏、静音或将音符强制吸附到自动 BPM。BPM 估计可能半速或倍速；可用已知 BPM 重生成，其变化不应改变音符在音乐中的绝对时刻。

难度名称是生成预设，模型强度不是官方 Malody Lv。文件校验不代表真人手感已通过；明确说明手机实际导入和试玩尚未完成的部分。保留实际生成引擎的 AI 来源标记。更详细的实现与来源见项目 `docs/model-selection.md`；使用与格式约定见 `README.md`。

六档参数、UI 与图片验证见 `docs/studio-v2.md`。最高档目标 26 NPS，可用起音不足时不凭空补键，报告实测值。V32 极少量非法轨道音符可舍弃且计入报告，较多非法输出仍拒绝打包。
