import hashlib
import json
from pathlib import Path
import time
from .paths import ROOT, PRESETS
from .audio import convert, analyze
from .charts import serialize, validate_chart, chart_stats, package, ChartStructureError
from .engine import Engine
from .difficulty import PATTERN_LABELS, V32_PATTERN_TAGS
from .naming import chart_id, chart_stem, archive_stem

engine = Engine()


def _variant_seed(seed, pattern):
    digest = hashlib.sha256(f'malody-chart-forge-v1:{seed}:{pattern}'.encode('utf-8')).digest()
    return int.from_bytes(digest[:4], 'big') % 2147483641


def run(source, directory, options, progress):
    started = time.time()
    raw_progress=progress;progress_floor=[0]
    def progress(message,value):
        progress_floor[0]=max(progress_floor[0],min(95,float(value)))
        raw_progress(message,round(progress_floor[0]))
    directory = Path(directory)
    progress('转换并检查音频', 3)
    y, sr, duration, audio = convert(source, directory)
    progress('分析节拍与音乐波形', 8)
    analysis = analyze(y, sr, options.get('bpm'))
    use_v32 = options.get('engine', 'mug') == 'v32'
    from .advanced import defaults, merge
    dynamic_settings=merge(defaults(),options)
    dynamic_settings['engine']='v32' if use_v32 else 'mug'
    dynamic_settings['strategy']='fast'
    if 'conditions' not in options:
        dynamic_settings['conditions']={'v32':{key:options.get('v32_difficulty',8) for key in PRESETS},
                                       'mug':{key:options.get('mug_difficulty',4) for key in PRESETS}}
    section_plan=None
    if options.get('dynamic_enabled',False):
        from .section_plan import build_plan
        reference = {**analysis, 'manual': True, 'points': [[0, analysis['bpm']]]} if options.get('bpm') is not None else analysis
        section_plan=build_plan(audio, dynamic_settings, reference)
        (directory/'section-plan.json').write_text(json.dumps(section_plan,ensure_ascii=False),encoding='utf-8')
    patterns = options.get('patterns') or [options.get('pattern', 'balanced')]
    selected = [key for key in PRESETS if key in options['difficulties']]
    all_charts, results, previews, caches = {}, [], {}, {}
    quality_alerts, pattern_errors, chart_errors, engine_metadata = [], [], [], {}
    wave = None
    sectioned=None
    if section_plan:
        from .advanced_generation import generate_sectioned
        sectioned=generate_sectioned(audio,directory/'sectioned-models',dynamic_settings,
                                     [{'key':chart_id(pattern,key),'pattern':pattern,'difficulty':key} for pattern in patterns for key in selected],
                                     section_plan,progress,raw_only=True)
        pattern_errors.extend(sectioned['errors'])
    elif not use_v32:
        wave = engine.prepare(y, sr, progress)
    else:
        import soundfile as sf
        engine.unload()
        input_wave = directory / 'v32-input.wav'
        sf.write(input_wave, y, sr, subtype='PCM_16')

    for pattern_index, pattern in enumerate(patterns):
        local_options = dict(options)
        local_options['pattern'] = pattern
        local_options['seed'] = _variant_seed(options['seed'], pattern)
        if sectioned:
            from .charts import Note
            cache_file=directory/'sectioned-models'/(pattern+'-mother.json')
            if not cache_file.is_file():continue
            raw_cache=json.loads(cache_file.read_text(encoding='utf-8'))
            master=[Note(*row) for row in raw_cache['raw']['master']]
            timing=sectioned['timings'].get(pattern) if use_v32 else None
            inherited=0
            engine_metadata[pattern]={**raw_cache['metadata'],'core_conditions':[r for r in raw_cache['records']],
                                      'section_plan_id':section_plan['id'],'raw_preserved':True}
        elif use_v32:
            tag = V32_PATTERN_TAGS.get(pattern)
            local_options['v32_descriptors'] = list(dict.fromkeys(
                [*options.get('v32_descriptors_base', options.get('v32_descriptors', [])), *([tag] if tag else [])]))
            pattern_dir = directory / 'v32-original' / pattern
            pattern_dir.mkdir(parents=True, exist_ok=True)
            try:
                from .mapperatorinator import generate
                raw_charts, metadata = generate(directory / 'v32-input.wav', pattern_dir,
                    {**local_options, 'patterns': [pattern]},
                    lambda message, fraction: progress(
                        f'{PATTERN_LABELS[pattern]}：{message}',
                        12 + ((pattern_index + fraction / 100) / len(patterns)) * 74))
                master, timing, inherited = raw_charts[selected[0]]
                engine_metadata[pattern] = metadata
            except Exception as exc:
                pattern_errors.append({'pattern': pattern, 'error': str(exc)})
                continue
        else:
            try:
                master = engine.generate(wave, None, local_options,
                    lambda fraction: progress(f'{PATTERN_LABELS[pattern]} 母谱生成',
                        14 + ((pattern_index + fraction) / len(patterns)) * 72))
                timing, inherited = None, 0
            except Exception as exc:
                pattern_errors.append({'pattern': pattern, 'error': str(exc)})
                continue

        from .difficulty import attacks, calibrate
        if section_plan:
            from .adaptive_difficulty import attacks_adaptive
            candidates=attacks_adaptive(y,sr,master)
        else:candidates = attacks(y, sr, master)
        cache_candidates = [[round(float(timestamp), 4), round(float(strength), 6),
            [[round(float(note.start), 4), int(note.lane),
              round(float(note.end), 4) if note.end is not None else None] for note in originals]]
            for timestamp, strength, originals in candidates]
        pattern_charts = {}
        for difficulty_index, key in enumerate(selected):
            variant = chart_id(pattern, key)
            preset = PRESETS[key]
            progress(f'{PATTERN_LABELS[pattern]} · {preset["label"]} 校验',
                88 + ((pattern_index * len(selected) + difficulty_index + 1) /
                      (len(patterns) * len(selected))) * 6)
            if section_plan:
                from .adaptive_difficulty import calibrate_adaptive
                notes,adjustment=calibrate_adaptive(candidates,duration*1000,key,options['ln_ratio'],local_options['seed'],options.get('difficulty_rules',{}).get(key),pattern,section_plan)
            else:
                notes, adjustment = calibrate(candidates, duration * 1000, key, options['ln_ratio'],
                    local_options['seed'], options.get('difficulty_rules', {}).get(key), pattern=pattern)
            version_name = chart_stem(options['title'], 'v32' if use_v32 else 'mug', pattern, key)
            if use_v32:
                from .mapperatorinator import serialize_with_timing
                chart = serialize_with_timing(notes, options['title'], options['artist'], version_name, timing)
                chart['meta']['creator'] = 'Malody Chart Forge / Mapperatorinator V32 (AI)'
            else:
                chart = serialize(notes, options['title'], options['artist'], version_name, analysis['bpm'])
                chart['meta']['creator'] = 'Malody Chart Forge / MuG Diffusion v1.0.0'
            try:
                validation = validate_chart(chart, duration * 1000)
            except ChartStructureError as exc:
                alert = {'type': 'track_conflict', 'severity': 'error', 'difficulty': key,
                    'pattern': pattern, 'chart_id': variant, 'start_ms': exc.start_ms or 0,
                    'end_ms': exc.end_ms or exc.start_ms or 0, 'lane': exc.lane,
                    'metric': None, 'message': str(exc)}
                (directory / f'quality-failure-{variant}.json').write_text(json.dumps({
                    'difficulty': key, 'pattern': pattern, 'duration_seconds': duration,
                    'alerts': [alert], 'preview': [[round(n.start, 2), n.lane,
                    round(n.end, 2) if n.end else None] for n in notes]}, ensure_ascii=False), encoding='utf-8')
                chart_errors.append({'chart_id':variant,'pattern':pattern,'difficulty':key,'error':str(exc)})
                continue
            stats = chart_stats(notes, duration)
            from .quality import assess
            alerts = assess(notes, duration, adjustment['target_active_nps'], y, sr, chart, key)
            for alert in alerts:
                alert.update(pattern=pattern, chart_id=variant)
            quality_alerts.extend(alerts)
            all_charts[variant] = chart
            pattern_charts[variant] = chart
            filename = chart_stem(options['title'], 'v32' if use_v32 else 'mug', pattern, key) + '.mc'
            results.append({'key': variant, 'chart_id': variant, 'difficulty': key,
                'pattern': pattern, 'pattern_label': PATTERN_LABELS[pattern], 'label': preset['label'],
                'filename': filename,
                'model_strength': options.get('v32_difficulty', 8) if use_v32 else options.get('mug_difficulty', 4),
                'removed_notes': adjustment['phone_filtered_notes'], 'model_raw_notes': len(master),
                'difficulty_adjustment': adjustment, 'validation': validation,
                'quality_alerts': alerts, 'pattern_metrics': {
                    'same_lane_repeat_ratio': adjustment['same_lane_repeat_ratio'],
                    'hand_alternation_ratio': adjustment['hand_alternation_ratio']}, **stats})
            if use_v32:
                results[-1].update(timing_points=len(chart['time']),
                    inherited_points_not_exported=inherited, model_raw_notes=len(master))
            previews[variant] = [[round(n.start, 2), n.lane,
                round(n.end, 2) if n.end else None] for n in notes]

        caches[pattern] = {'format': 2, 'pattern': pattern, 'duration_ms': round(duration * 1000),
            'bpm': analysis['bpm'], 'engine': 'v32' if use_v32 else 'mug',
            'model_version': 'Mapperatorinator V32 mania' if use_v32 else 'MuG Diffusion v1.0.0',
            'section_plan':section_plan, 'candidates': cache_candidates, 'timings': {key: timing for key in selected} if use_v32 else {},
            'raw_count': len(master), 'options': {k: v for k, v in local_options.items()
                if not k.startswith('_')}}
        cache_dir = directory / 'chart-cache' / pattern
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / 'generation-cache.json').write_text(
            json.dumps(caches[pattern], ensure_ascii=False), encoding='utf-8')

    if not all_charts:
        failures=pattern_errors+chart_errors
        detail='；'.join(item['pattern'] + (' ' + item['difficulty'] if item.get('difficulty') else '') + '：' + item['error']
                         for item in failures)
        raise RuntimeError('所有排键均未生成可导出谱面' + ('：' + detail if detail else ''))
    # Keep the pre-combination cache alias for old single-pattern tasks/tools.
    if len(caches) == 1:
        (directory / 'generation-cache.json').write_text(
            json.dumps(next(iter(caches.values())), ensure_ascii=False), encoding='utf-8')
    del wave
    warnings = analysis['warnings'][:]
    if pattern_errors:
        warnings.append('部分排键生成失败：' + '；'.join(item['pattern'] + '：' + item['error']
                                                          for item in pattern_errors))
    if chart_errors:
        warnings.append('部分谱面因轨道结构冲突未导出：' + '；'.join(item['pattern'] + ' ' + item['difficulty'] + '：' + item['error'] for item in chart_errors))
    if use_v32 and any(data.get('discarded_invalid_lane_notes') for data in engine_metadata.values()):
        warnings.append('V32 少量超出四轨范围的原始音符已舍弃；对应原始输出保留供检查。')
    warnings.append('难度名称代表生成目标，排键名称代表生成倾向；实际手感请结合网页预览和试玩。')
    if any(abs(row['ln_ratio'] - options['ln_ratio']) > .15 for row in results):
        warnings.append('部分谱面的实际长条比例与目标偏差较大；生成条件不保证实际比例。')
    warnings.append('网页试玩使用 Malody V PC Normal（C 判）最佳判定窗口参考值；其他判定档及设备延迟可能不同。')
    report = {'schema_version': 3, 'title': options['title'], 'artist': options['artist'],
        'duration': duration, 'bpm': analysis['bpm'],
        'engine': 'Mapperatorinator V32 mania' if use_v32 else 'MuG Diffusion v1.0.0',
        'device': engine_metadata.get(patterns[0], {}).get('device', engine.device) if use_v32 else engine.device,
        'engine_metadata': engine_metadata, 'source': options.get('source', '用户上传'),
        'seed': options['seed'], 'requested_ln_ratio': options['ln_ratio'],
        'steps': None if use_v32 else options['steps'], 'elapsed_seconds': round(time.time() - started, 1),
        'patterns': list(dict.fromkeys(row['pattern'] for row in results)), 'requested_patterns': patterns,
        'pattern_errors': pattern_errors, 'chart_errors': chart_errors,
        'partial': bool(pattern_errors or chart_errors), 'failed_combinations': pattern_errors+chart_errors,
        'charts': results,
        'difficulties': results, 'quality_alerts': quality_alerts, 'warnings': warnings,
        'analysis': analysis, 'previews': previews,
        'section_plan_id':section_plan['id'] if section_plan else None,
        'audio_format': 'OGG Vorbis / 44100 Hz / stereo',
        'timing_policy': '保留 V32 分段 BPM；音符按排键倾向规划，并以音乐起音辅助分层' if use_v32 else
                         '音符按 MuG 条件和排键倾向规划，并以音乐起音辅助分层',
        'generation_settings': {key: value for key, value in options.items()
                                if key != 'artwork_video_id' and not key.startswith('_')}}
    from .artwork import prepare_artwork
    progress('获取封面并检查曲包', 96)
    background, artwork = prepare_artwork(options.get('artwork_video_id'))
    report['artwork'] = artwork
    if background:
        for chart in all_charts.values():
            chart['meta']['background'] = 'background.jpg'
    elif options.get('artwork_video_id'):
        warnings.append('YouTube 封面获取失败，本次未包含背景图。')
    else:
        warnings.append('上传音乐未关联 YouTube 封面。')
    filenames = {row['chart_id']: row['filename'] for row in results}
    report['download_name'] = archive_stem(options['title'], 'v32' if use_v32 else 'mug',
        patterns, selected) + '.mcz'
    archive = package(directory, all_charts, audio, report, background=background, filenames=filenames)
    progress('生成完成', 100)
    return report, archive
