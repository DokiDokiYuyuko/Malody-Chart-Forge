# 本地服务接口参考

本文件用于 Codex 按歌单准备与提交高级任务。先读取项目 README 和 `GET /api/health`；以部署代码和实际响应为准。默认根地址 `http://127.0.0.1:8765`，`GET /api/queue` 查看现有任务。所有调用复用同一个服务。

## 1. 冻结录音身份，准备项目

在 `work/<batch>/` 保存曲目 ID、歌名、音乐人、录音版本、歌单元数据来源、完整音源来源、时长、音频哈希、完整解码检查、封面来源及图片哈希。网易云曲目身份不等于已取得完整音频；不能用相似版本或片段顶替。音源获取使用用户已有文件或可核实来源，不依赖本仓库附带的私人脚本。

新高级项目可以上传本地音频：

```http
POST /api/advanced/projects/upload
Content-Type: multipart/form-data

file=<audio bytes>, title=<verified title>, artist=<verified artist>, creator=<chart author>
```

也可从已登记的音乐库音源创建：

```http
POST /api/advanced/projects/from-source
Content-Type: application/json

{"type":"asset","id":"<verified music asset id>","creator":"Startrail"}
```

创建不是幂等接口。先保存创建意图；响应不明时查询 `GET /api/advanced/projects`，核对本机项目原始文件哈希、元数据和创建时间，再记录实际项目 ID，不能不断重复上传。返回项目可能继承已登记音源的元数据，必须再次核对。

上传本地音频不一定附带封面。优先复用经过核验、含封面的登记音源。确需补封面时，使用可验证的图片在独立交付 MCZ 中补入包内图片，并同时更新每份 MC 的 `meta.background`；保留导出原版，不直接编辑运行中项目或已采用版本。

## 2. 保存生成设置与片段

用 `GET /api/advanced/projects/<pid>` 读取当前项目。PATCH 中的 `settings` 会验证并补全默认值；准备时应以当前 `settings` 为基础合并任务覆盖，不丢失已有设置。电脑端使用 `profile=keyboard`。

```http
PATCH /api/advanced/projects/<pid>
Content-Type: application/json

{
  "expected_revision": 0,
  "profile": "keyboard",
  "patterns": ["balanced"],
  "difficulties": ["expert", "master"],
  "settings": {
    "engine": "v32",
    "strategy": "independent",
    "source_mode": "mix",
    "dynamic_enabled": true,
    "retain_debug_artifacts": false,
    "nps_ranges": {"expert":{"min":9,"max":12},"master":{"min":11,"max":15}}
  }
}
```

修订号、难度、目标和参数均替换为任务实际值。V32 新请求独立生成各档，提交时冻结 NPS→SR 和 BPM 桶；检测的 BPM 不默认作强制 timing。SR≥7 仅来自低星样本的外推，不保证目标密度。

项目为空时可用 `POST /api/advanced/projects/<pid>/segments` 建立整曲片段：

```json
{"expected_revision":1,"name":"Full song","start_sample":0,"end_sample":12345678}
```

`end_sample` 使用项目实际 `samples`，采样时钟为 44100 Hz。已有片段不能盲目重建。也可使用已有的区域规划工作流。`POST /api/advanced/projects/<pid>/section-plans`，body `{"settings":<merged settings>}`，可预检并保存音乐分析方案；这一步可能需要 Beat This。获取新的项目修订号后冻结生成请求。

原曲示例用 `source_id=original`。需要自动准备人声／伴奏时，生成请求可用 `auto_prepare_stems=true`，并选择实际可用的分离设置；已存在分离版本可用 `source_id=vocals_accompaniment` 加 `stem_set_id`。不同难度的不同音源策略应拆成独立请求，不能把 mix 和 dual 结果混称。

## 3. 正式提交与恢复

生成接口为 `POST /api/advanced/projects/<pid>/generation-batches`。`request_id` 使用新生成且提前保存的 `uuid.uuid4().hex`。请求必须包含实际 `segment_ids`；`variants` 为项目返回的组合键，例如 `balanced--expert`。先读回项目并冻结 `expected_revision`，再写入完整请求。

交给项目 `tools/batch_generate.py` 执行已冻结请求；清单示例及命令见 [docs/codex-workflow.md](../../../docs/codex-workflow.md)。控制器只提交、跟踪和保存回执，不选择候选，不自动重试，不负责整个歌单的尝试预算。两次请求使用同一项目时，前一个任务会增加项目修订号；分阶段准备下一清单，不能提前写死过期修订号。

批次回执包含 `id` 和 job ID。`GET /api/advanced/projects/<pid>/generation-batches/<bid>` 返回 `status`、`jobs` 和 `results`。任务的已解析参数与冻结输入保存在对应队列任务记录中。`GET /api/advanced/projects/<pid>/tasks` 可辅助核对任务，`GET /api/jobs/<job_id>` 查看具体任务。

失败、partial、暂停或提交结果不明时，先核对状态和持久化回执，不重复提交持久化请求 ID。服务重启后原排队任务可能暂停；明确继续原队列，而不是复制新任务。新的质量候选使用新 ID，保留原候选并计算累计尝试次数。

## 4. 选版、独立导出与回读

完成后从批次 `results` 中选择匹配片段和组合、可用且经过核验的 `revision_id`，读取 `GET /api/advanced/projects/<pid>/revisions/<rid>`。仅 `status=completed` 不能确定结果音乐质量。不要修改原始模型版本。

采用候选可用 `POST /api/advanced/projects/<pid>/segments/<sid>/select`，body 包含实际 `variant`、`revision_id`、`expected_revision`。需保留旧采用映射时，优先独立候选导出：

```http
POST /api/advanced/projects/<pid>/assemblies
Content-Type: application/json

{
  "expected_revision": 7,
  "preroll": 1.5,
  "require_complete": true,
  "revision_overrides": {"<segment id>":{"balanced--expert":"<revision id>","balanced--master":"<revision id>"}}
}
```

`revision_overrides` 仅改变本次导出选择，不覆盖项目已采用映射。响应 `id` 是 assembly ID；`GET /api/advanced/projects/<pid>/assemblies/<aid>` 返回报告和预览，`GET /api/advanced/projects/<pid>/assemblies/<aid>/download` 返回 MCZ。保存后独立回读包内 MC、音频、封面与作者，按用户目标重算指标，再形成显式成品清单。

质量与导入命令见 [docs/chart-quality.md](../../../docs/chart-quality.md)。自动检查、实际听音、游戏目录回读和真人试玩分别记录；不要将任何一种证据冒充另一种。
