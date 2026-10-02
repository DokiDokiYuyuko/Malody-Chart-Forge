import json
from pathlib import Path
import time
from .paths import ROOT, PRESETS
from .audio import convert, analyze
from .charts import serialize, validate_chart, chart_stats, package
from .engine import Engine

engine = Engine()

def run(source, directory, options, progress):
    started = time.time()
    directory = Path(directory)
    progress('转换并检查音频', 3)
    y, sr, duration, audio = convert(source, directory)
    progress('分析节拍与音乐波形', 8)
    analysis = analyze(y, sr, options.get('bpm'))
    use_v32 = options.get('engine', 'mug') == 'v32'
    raw_charts, engine_metadata = {}, {}
    if use_v32:
        from .mapperatorinator import generate
        import soundfile as sf
        engine.unload()
        input_wave = directory / 'v32-input.wav'
        sf.write(input_wave, y, sr, subtype='PCM_16')
        raw_charts, engine_metadata = generate(input_wave, directory, options, progress)
        from .difficulty import attacks
        progress('分析音乐起音并划分六档难度', 93)
        candidates = attacks(y, sr, next(iter(raw_charts.values()))[0])
        wave = None
    else:
        wave = engine.prepare(y, sr, progress)
        master = engine.generate(wave, options,
                                 lambda f: progress('生成 MuG 共享母谱', 22 + f * 68))
        from .difficulty import attacks
        candidates = attacks(y, sr, master)
        engine_metadata['difficulty_policy'] = '共享母谱，音频起音辅助六档分层'
    charts, results = {}, []
    selected = [key for key in PRESETS if key in options['difficulties']]
    previews = {}
    for index, key in enumerate(selected):
        preset = PRESETS[key]
        label = preset['label']
        progress(f'校验{label}谱面', 93 + index * 3 / len(selected))
        if use_v32:
            notes, timing, inherited = raw_charts[key]
        else:
            notes = master
        raw_count = len(notes)
        from .difficulty import calibrate
        notes, adjustment = calibrate(candidates, duration * 1000, key, options['ln_ratio'],
                                      options['seed'], options.get('difficulty_rules', {}).get(key))
        if use_v32:
            from .mapperatorinator import serialize_with_timing
            chart = serialize_with_timing(notes, options['title'], options['artist'], f'4K {label} / V32', timing)
        else:
            chart = serialize(notes, options['title'], options['artist'], f'4K {label}', analysis['bpm'])
        validation = validate_chart(chart, duration * 1000)
        stats = chart_stats(notes, duration)
        charts[key] = chart
        results.append({'key': key, 'label': label,
                        'model_strength': options.get('v32_difficulty', 8) if use_v32 else options.get('mug_difficulty', 4),
                        'removed_notes': adjustment['phone_filtered_notes'], 'model_raw_notes': raw_count,
                        'difficulty_adjustment': adjustment, 'validation': validation, **stats})
        if use_v32:
            results[-1].update(timing_points=len(chart['time']), inherited_points_not_exported=inherited,
                               model_raw_notes=raw_count)
        previews[key] = [[round(n.start, 2), n.lane, round(n.end, 2) if n.end else None] for n in notes]
    del wave
    warnings = analysis['warnings'][:]
    if use_v32:
        if engine_metadata.get('discarded_invalid_lane_notes'):
            warnings.append(f"V32 原始输出中 {engine_metadata['discarded_invalid_lane_notes']} 个非法轨道音符已舍弃；原始文件保留供检查。")
        warnings = [warning for warning in warnings if '可填写已知 BPM' not in warning]
    warnings.append('六档难度由共享母谱与音乐起音辅助分层，名称不是官方 Lv。高档增加连打及同时按键，实际手感需试玩。')
    for result in results:
        if abs(result['ln_ratio'] - options['ln_ratio']) > .15:
            warnings.append(f"{result['label']}实际长条比例 {result['ln_ratio']:.1%}，与目标 {options['ln_ratio']:.1%} 偏差较大；生成条件不保证实际比例。")
    # Conditions are estimates. Report unexpected density order, never relabel it as a success.
    if any(results[i]['average_nps'] > results[i + 1]['average_nps'] for i in range(len(results) - 1)):
        warnings.append('部分难度的平均物量未递增；模型参数不保证实测难度，请结合预览与试玩评估。')
    if any(results[i + 1]['average_nps'] < results[i]['average_nps'] * 1.2
                       for i in range(len(results) - 1)):
        warnings.append('音乐起音候选或长条占用限制了难度差距，部分相邻档位密度差不足 20%；请结合预览评估。')
    for result in results:
        target = result['difficulty_adjustment']['target_active_nps']
        if result['average_nps'] < target * .75 or (result['key'] == 'lunatic' and result['average_nps'] < 20):
            warnings.append(f"{result['label']}实测密度低于目标；该曲可用起音较少，不会为凑物量在空白处加键。")
    warnings.append('已通过文件与轨道检查；旧版手机实际导入和手感仍需试玩确认。')
    if use_v32:
        warnings.append('V32 采用模型生成的分段节拍；生成步数设置仅适用于 MuG。滚速效果未导出，请在手机中确认同步与手感。')
        if options.get('bpm') is not None:
            warnings.append('填写的 BPM 仅用于音频分析显示；V32 曲包保留模型生成的分段 BPM。')
    report = {'title': options['title'], 'artist': options['artist'], 'duration': duration,
              'bpm': analysis['bpm'], 'engine': 'Mapperatorinator V32 mania' if use_v32 else 'MuG Diffusion v1.0.0',
              'device': engine_metadata.get('device', engine.device), 'engine_metadata': engine_metadata,
              'source': options.get('source', '用户上传'), 'seed': options['seed'], 'requested_ln_ratio': options['ln_ratio'],
              'steps': None if use_v32 else options['steps'],
              'elapsed_seconds': round(time.time() - started, 1), 'difficulties': results,
              'warnings': warnings, 'analysis': analysis, 'previews': previews,
              'audio_format': 'OGG Vorbis / 44100 Hz / stereo',
              'timing_policy': '保留 V32 分段 BPM；音符取模型锚点或实际音频起音，不强制节拍吸附' if use_v32 else '音符取 MuG 锚点或实际音频起音；分数拍序列化，不强制节拍吸附',
              'generation_settings': {key: value for key, value in options.items()
                                     if key != 'artwork_video_id'}}
    from .artwork import prepare_artwork
    progress('获取 YouTube 封面并检查曲包', 96)
    background, artwork = prepare_artwork(options.get('artwork_video_id'))
    report['artwork'] = artwork
    if background:
        for chart in charts.values():
            chart['meta']['background'] = 'background.jpg'
    elif options.get('artwork_video_id'):
        warnings.append('YouTube 封面获取失败，本次未包含背景图；可重新生成以重试。')
    else:
        warnings.append('上传音乐未关联 YouTube 封面；可在选曲时填写对应视频链接。')
    archive = package(directory, charts, audio, report, background=background)
    progress('生成完成', 100)
    return report, archive
