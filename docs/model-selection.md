# 模型选择与格式依据

调研与部署时间：2026-10-01。目标是公开权重、能在现有 Windows NVIDIA GPU 上推理、控制 4K 难度与长条比例、可改造成上传音乐直接出曲包的模型。这里的“最佳”是当前任务的落地选择，不是所有论文模型的统一质量排名。

2026-10-02 补充调研：Mapperatorinator V32 和 Mini 均已核实存在公开 mania 专用权重，V32 应作为下一轮实测的主要候选。目前没有同条件手机 4K 质量评测支持直接替换。详见 [其他模型调研](alternative-models-2026-10-02.md)。

选择 [Keytoyze/Mug-Diffusion](https://github.com/Keytoyze/Mug-Diffusion)。它提供已训练的 4K 音频条件扩散模型、难度与键型控制，作者明确提及 Malody 风格。源码固定在 `c2903d4e3d7216d94ef512a9872bc77423c6e0ac`，没有改动上游模型代码，本项目通过独立推理适配器调用。其他研究路线常需要重新训练或缺少同等完整的现成推理交付，不能仅凭更新日期判定更适合用户的任务。

公开镜像：[ayousanz/Mug-Diffusion-model](https://huggingface.co/ayousanz/Mug-Diffusion-model)。权重 1,839,231,053 字节，SHA-256 `af6ab91337d0ef6b518367082ac3f849448c6daaa01fd987678fb25ea44ca184`，下载完成已校验。配置来自同一镜像。使用上游特征变换：22050 Hz、512 FFT、128 hop、128 mel、log1p，以及对应潜变量填充长度。保留模型原始绝对时间，随后施加手机四指的碰撞和密度限制。

上游默认导出 osu!mania，本项目转换成 classic MC。参考旧版转换器 [Jakads/malody2osu](https://github.com/Jakads/malody2osu/blob/master/convert.py) 验证拍坐标、轨道、长条和 BGM offset 的符号。其音符时间转换为 `beat*60000/BPM - BGM.offset`，因此本项目拍零为音频零，BGM offset 为零；独立测试覆盖非零 offset 与跨 BPM 长条。

MCZ 使用标准 ZIP，内部 `0/` 包含音轨与多个 MC 文件，组织约定参考 [Jubeat2Malody-GUI 的打包实现](https://github.com/Swan416ya/Jubeat2Malody-GUI/blob/master/docs/PARSING_LOGIC.md)。复读每份 MC 校验合法拍分数、0–3 轨、时间顺序、时长、同轨重复与长条覆盖；音频实际解码校验为 OGG Vorbis，并检查与分析音频时长一致。尚未在手机游戏中做真实导入，不能声称已经实机验证。

参考音乐来自 [ヰ世界情緒官方 MV：シリウスの心臓](https://www.youtube.com/watch?v=UKZt1vq8bKI)，只下载其公开音轨供用户要求的本地制谱，原始信息保存于 `uploads/reference-sirius.info.json`。未使用登录绕过、DRM 或付费访问绕过。参考 MV 长约 5:20，结尾含实际音轨的静音尾段，完整保留。

代码保留上游 MIT LICENSE。英文 README 对模型权重和生成谱面附加非商业声明，AI 来源应保留；本项目在 MC creator、曲包 generation.txt 与报告中明确标注 MuG Diffusion。音乐的权利不因谱面生成而改变，本地参考曲包不自动发布。

## 可改进之处

模型强度是条件输入，未经 MinaCalc 或 Malody 官方评级。生成后显示真实物量、峰值与长条比例，遇到难度密度非递增会提示。手机约束不是完整的人体工学判定；还可以通过用户试玩反馈改进双手分配、夹押与长条覆盖时的手负担。当前 BPM 仅供编辑器拍坐标，无需将生成的绝对时间误吸附到错误节拍。后续可接入可靠的分段节拍/变速识别并做听感评价。
