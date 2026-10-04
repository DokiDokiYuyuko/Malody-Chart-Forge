# 同步频谱与四轨预览

频谱只提供听音证据，不改音符、难度、播放速度或判定。频谱横轴是频率，四轨是排键，轨道与音高没有映射关系。

## 时间与绘制契约

同一次绘制从音乐 owner 读取一次 snapshot，并复用四轨渲染器的 geometry/travel：

```javascript
const pane = SpectrumPane.create(canvas, {onChange: redraw});
pane.setSource({
  initialManifest, manifestUrl,
  tileUrl: (analysisId, level, index) => `/api/.../${analysisId}/tiles/${level}/${index}`,
  sourceTimeOffsetMs: 0,
  label: '原曲频谱', isAssembly: false
});
pane.setAnchors(anchors); // 已选节拍参考及可信状态、起音原始 time_ms
pane.setHighlights(selectedNotes); // time_ms/start_ms 必须已经在分析音频域；可传color
pane.render({displayTimeMs: now, sourcePosition}, {
  ...PlayfieldRenderer.geometry(chartWidth, sharedHeight), travelMs: 5000 / speed
});
```

也支持 `projection.geometry` 形式。提供 `projection.timeToY(t)` 时直接调用共享函数，不重新计算投影。流速 1–16×仅调整 travel；频谱绝不修改 HTMLAudio 或键盘判定。

原曲试听的 displayTimeMs 是原曲毫秒，offset=0；片段局部试玩的 displayTimeMs 是局部毫秒，offset=片段原曲起点毫秒；成品使用实际 assembled.wav、displayTimeMs 是成品输出毫秒，offset=0。origin_sample 是分析来源元信息，不能在 Pane 中再次自动加上。音乐事件 offset 已由 chart adapter 规范化，不再加入频谱偏移。

成品原曲定位由 `playbackToSource(outputSample, mapping)` 提供，区间半开；准备静音区返回 null。映射跨接缝不是固定 origin 平移。两张 A/B 必须来自同一源、同一范围，并已经转换到相同 display 域。切换 A/B 不换频谱、不重启音乐。

## 后端

```python
service = SpectrumService(project_directory)  # 按项目复用此实例
manifest = service.request(source_id, wav_path, start_ms, end_ms, origin_sample=0)
manifest = service.manifest(analysis_id, start_ms, end_ms)
tile_path = service.tile(analysis_id, level, index)  # None => HTTP 202, Retry-After: 1
matrix = service.alignment_matrix(analysis_id, start_ms, end_ms)  # None => 仍在准备
```

WAV 必须位于当前项目内，44100 Hz；source_id 区分 original、人声、伴奏、assembly 身份。stem 路径由分离 manifest 的服务端 resolver提供，不能接收浏览器任意文件路径。成品传 `assemblies/{id}/assembled.wav`，不能用原曲频谱拼贴冒充实际接缝声音。

推荐路由：POST `/projects/{pid}/analysis` 完成源解析并返回 manifest，GET `/projects/{pid}/analysis/{aid}?start_ms=&end_ms=`优先准备当前窗口，GET `/projects/{pid}/analysis/{aid}/tiles/{level}/{index}`返回 application/octet-stream。浏览器 manifestUrl 指向 GET；可提供 `loadManifest(start,end,signal)` 代替 URL。源解析/哈希/文件操作放在线程池，不能阻塞异步服务事件循环。

manifest schema_version=2，包含音频哈希、参数、帧原点、采样数、dt_ms、levels、bins、ready_tiles、tile_hashes和errors。HTTP返回隐藏 audio_path/signature。参数和音频内容哈希验证缓存身份；每个tile另有完整SHA256，截断或同长度损坏会重建。源被修改后旧 analysis_id 拒绝读取，新 prepare得到新身份。

参数：22050Hz、1024点周期Hann、hop110、center=True、90dB固定标度、192个频率显示箱；1024基础frame一tile，level0–4 stride1/2/4/8/16。二进制顺序 `[plane, frame, frequency]`，uint8，plane0均值、plane1峰值。每level的frame数是 ceil(基础frame数/stride)，tile index始终按基础frame计。uint8表示 -90 + value/255*90 dB。参考是固定窗归一振幅1，禁止每窗口归一化；超过参考饱和用于显示，不改分析音频。

采样中心 k*110/22050秒，对应源 k*220 sample，无额外半窗延迟。STFT窗长46.44ms，帧间隔4.989ms；更密采样不代表真实瞬态分辨率5ms。滤波器为显式41点kaiser5 polyphase FIR；分块在偶数源sample起读，带40源sample上下文，STFT再带左右512分析sample。只在真实文件首尾补零，奇数总sample也保持全局网格。通道分别计算功率再平均，避免反相立体声平均波形造成的抵消。LOD在功率域池化均值与最大值，显示量化不用于重新推导精确起音。

全局一个后台CPU worker：当前窗口优先于相邻预取，同任务去重、提升优先级，文件原子提交。只计算当前窗口及相邻tile，不等待全曲STFT。一个tile运行中不会被取消；切视图的旧任务最多完成当前块，音乐继续播放。

静态 Agent PNG消费 alignment_matrix 的 `times_ms` 和 `db[frames,192]`，按真实帧中心定位，不将列无条件拉伸成另一时间范围。起音和节拍由现有features及已选timing参考提供；tempo变化仅更新覆盖层，谱面revision、主题、流速不使声学缓存失效。

## 前端资源与布局

SpectrumPane 是独立 Canvas2D 实例。Worker 将下载的tile转换ImageBitmap，逐帧仅裁切拼贴，不执行STFT。旧浏览器在tile到达时一次转换降级。默认缓存32MB，最多两请求，generation/AbortController阻止旧来源覆盖新来源，dispose释放Worker/bitmap/定时器；暂停时仅待处理tile刷新，不推进时间。离开高级视图调用 suspend() 停止后台重绘/重试，返回调用 resume() 后用新的共同snapshot绘制。

布局使用 `.spectrum-stage > .spectrum-column(canvas) + .spectrum-chart-host(.adv-canvases, arcade)`；试玩只隐藏 chart-host 下的预览，频谱保持可见。窄stage设置 data-narrow=true 并沿用 data-ab=before/after，只显示完整A或B与单频谱，不能纵向堆叠三张。外层固定视口高度及侧栏抽屉由高级工作区布局负责。

缺tile显示准备状态，不阻止播放。起音实线、节拍虚线，不把声学峰自动当正确音符。五主题仍沿用界面变量与认可的四轨青蓝金粉；频谱使用独立低亮度功率色表。

## 验证

新增 test_spectrum.py 验证分块与整段重采样一致、STFT跨tile等价、中心脉冲无半窗偏移、反相通道、LOD瞬态保留、缓存身份/损坏/边界、实际成品准备静音与fade。spectrum_ui.test.cjs验证1/8/16×多DPR同投影≤1设备像素，成品源定位到1sample、准备区空映射、迟到tile不重绘新来源及暂停重试清理。

真实曲包保留28–44秒核心、26–46秒上下文。四档基线音符数40/80/135/202，27.318秒150→240节拍参考；频谱功能不修改基线。还应实际检查原曲试听、片段局部、成品1.5秒准备区、连续/非连续接缝、A/B、Autoplay和DFJK，以及五主题/低高度桌面无纵向滚动。自动化不代替听辨或手机游戏验收。
