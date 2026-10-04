# 人声、伴奏独立制谱与融合

分离用于采音、分析和试听；导出使用项目原曲，或用户选择片段组装后的原曲音频。不会用分离结果替换成品音乐。

高级台的「分离」窗口可以单独处理完整音乐并试听，不要求先划段或制谱。选择输入后到「生成」提交原曲、人声、伴奏或两路原谱；新工作流不默认融合。原谱完成后，到「比较与修订 → 融合已生成的两路原谱」手动创建联合候选并采用。[操作指南](advanced-authoring-guide.md)

## 部署

`tools/setup_separation.ps1` 在项目内建立第三独立环境。Python3.10.19、Demucs4.0.1、Torch2.0.1+cu118、Torchaudio2.0.2+cu118、NumPy1.26.4；实际依赖冻结在 `runtime/separation-requirements-lock.txt`。不修改 MuG 或 V32 环境。Python下载和 uv 工具也保存在项目 runtime/cache；不需要注册系统 Python。

默认 `htdemucs`，可用 `-FineTuned` 安装 `htdemucs_ft`。两者均从 Demucs 官方权重清单获取并核实文件名 SHA 前缀，再在项目 manifest 登记完整 SHA-256。模型文件与许可证在 `models/separation/demucs-4.0.1/`。依赖安装和权重校验不代表已经完成推理；`inference_verified` 只在实际分离通过后登记。

上游：[Demucs4.0.1](https://github.com/facebookresearch/demucs/tree/v4.0.1)、[MIT](https://github.com/facebookresearch/demucs/blob/v4.0.1/LICENSE)、[官方模型清单](https://github.com/facebookresearch/demucs/blob/v4.0.1/demucs/remote/files.txt)、[PyTorch Windows 版本组合](https://pytorch.org/get-started/previous-versions/)。默认模型的代码已归档，版本固定避免安装过程自动追踪主分支。现已追加独立的 Kim Mel-Band RoFormer 试验质量档，参见 [分离实验室指南](separation-upgrade-guide.md)。以下 Demucs 参数与算法说明仍适用于原有快速档。

## PCM 与缓存

唯一音频时钟为 stereo float32、44100 Hz、sample origin0、全曲帧数N。Demucs内部把原混音共同归一化，输出再恢复相同尺度；伴奏由 drums+bass+other 相加。独立stem不再峰值归一化、裁静音、伸缩或相位移动。

V32 输入适配器直接读取浮点 WAV，支持声部中超过 1.0 的合法瞬态，避免旧解码器不识别 FLOAT 格式。模型特征仍使用上游的输入归一化；它不会改写保存的声部或本地声学评分所用的共同幅度尺度。

`separation.ensure_stems(source,directory,settings,progress)` 由独立分离任务调用；兼容旧父生成任务中的同步阶段，没有子队列等待阻塞。缓存 key包括原PCM数据哈希、模型/环境/权重/配置和固定分离seed。模型结果帧数必须等于N；输出float WAV、完整哈希和manifest校验后才可复用。只有一个stem或 `.partial` 不能命中缓存。原PCM文件SHA沿用旧项目字段；新增PCM哈希是数据payload哈希，二者不混用。

`cache/separation/<recipe_hash>/` 保存可共享全曲分离结果。项目 `outputs/advanced/<pid>/stems/<id>/` 保存自己的文件链接/副本和manifest，所有试听路径留在项目内。`source_id=<recipe_hash>:vocals|accompaniment`，父源为原曲PCM哈希，origin仍为0。修改片段/BPM、重掷制谱seed不重新分离；分离seed独立固定。重复任务用原子目录lease避免同时写缓存；失败保留日志和可复用已完成阶段。

已完成的原谱阶段另在 `cache/stem-generation/<raw_recipe_hash>.json` 保存结果与内容哈希。服务提交前中断也能复用该阶段及其预分配revision ID。原谱缓存身份包含实际音源、种子、模型条件、相关SectionPlan条件、生成适配代码哈希和模型registry；更换模型或适配器不会假装旧输出来自新推理。

## 原谱、融合与版本

`stem_generation.run(source,directory,options,progress)` 返回advanced_result rows，由原父任务服务器统一提交。保持 `variant=pattern--difficulty`；人声/伴奏原谱用 `kind=stem_raw`、`provenance.source_role`区分，`activate_initial=False`，不抢占当前导出版本。

两stem读取不同PCM，基础seed按role和PCM哈希独立派生。保留用户的独立生成或共享母谱策略，`_stem_raw_only=True`告知生成器保留模型组件，禁止每stem消费完整可玩难度预算。未变的组件可通过原谱配方哈希复用；一个stem失败不以另一stem或原混音冒充补齐。空声部原谱合法。

融合需要同variant、同源片段范围、同stem_set的明确两个原谱revision。`fuse_revisions` 是纯函数，输出新谱、origins、决策和共享SectionPlan哈希；显式 `/segments/<sid>/fuse`创建未激活新候选。用户选择版本仍由既有select操作完成。重新融合不修改原谱。

## 规则

时间相近不等于同一声音。只有明确 shared acoustic event证据会去重；默认局部波形相似>0.995且弱声部幅度比<0.08才标记疑似泄漏。证据不足保留不同声部候选。独立同时起音可形成和弦，原始时点不因小于50ms被吞并或吸附。

候选按原曲局部能量支持排序，有界beam同时选择采音与四轨安排。原轨是软偏好；stream/speed/jack意图作为偏好保留，声部不固定左右。SectionPlan每段target_heads为软目标，额外头逐渐增加惩罚。peak_1s、chord、min_lane_gap、release_gap和LN上限作为全局约束；四轨占用跨段延续，rolling peak跨边界计数。手部交替为软惩罚，不宣称等同真人可玩性。

融合只消费一份每档总预算，不调用旧calibrate二次重排。候选不足不补网格音。省略、改轨、LN限制均有决策日志，输出note ID由配方和来源稳定派生。跨段长条头归属当前段、占轨延续；导出接缝仍执行既有连续/不连续原曲范围规则。

## 验证

自动用例 `tests/test_separation.py`与`tests/test_fusion.py`覆盖floatPCM帧数/完整性/原点、合法路径、独立seed、空谱、证据去重、近时不同声部、共享预算、跨段峰值/LN、不可变原谱和可复现融合。运行pytest时临时目录可放项目cache。

默认模型真实分离记录：2026-10-03 RTX4080 SUPER，290.112秒曲目全曲分离总57.17秒，12,793,940帧两stem均精确对齐；worker Torch allocator峰值allocated0.534GiB/reserved0.676GiB，并非系统总显存占用。28–44秒两stem之和对原曲的相关峰为0sample，相关系数0.99943；原曲文件哈希未变。第二次完整校验命中缓存0.578秒。完整记录在 `cache/separation-acceptance.json`。这些数据只代表这次本机默认模型分离，不保证ft性能或合成人声语义识别质量。

真实曲目28–44s高速合成人声段，固定同档条件和预算，对照原混音谱与两stem融合谱。分析上下文可用24–48s，核心仍为[1234800,1940400)源samples。比较误采/漏采/泄漏、局部起伏、占轨和手部负担；两版必须使用相同原曲试听。实际手机导入、真人四指手感和stem听辨需单独验收，自动结构验证不能替代。
