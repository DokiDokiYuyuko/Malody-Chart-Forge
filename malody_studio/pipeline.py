import hashlib
import json
from pathlib import Path
import time
from .paths import ROOT, PRESETS
from .audio import convert, analyze
from .charts import serialize, validate_chart, chart_stats, package, ChartStructureError
from .engine import Engine
from .difficulty import PATTERN_LABELS, V32_PATTERN_TAGS
from .naming import chart_id, chart_stem, archive_stem, validate_creator

engine = Engine()


def _variant_seed(seed, pattern):
    digest = hashlib.sha256(f'malody-chart-forge-v1:{seed}:{pattern}'.encode('utf-8')).digest()
    return int.from_bytes(digest[:4], 'big') % 2147483641


def run(source, directory, options, progress):
    from .workflow_log import stage, event as log_event, file_identity
    if 'creator' in options:
        options = {**options, 'creator': validate_creator(options['creator'])}
    if options.get('direct_v32_policy'):
        from .direct_v32 import simple_run
        with stage('simple.direct_v32_workflow', source=file_identity(source),
                   policy=options.get('direct_v32_policy'), settings=options):
            return simple_run(source,directory,options,progress)
    whole_song = 'simple_generation_policy' in options
    if whole_song:
        from .simple_generation import validate
        validate(options)
    started = time.time()
    raw_progress=progress;progress_floor=[0]
    def progress(message,value):
        progress_floor[0]=max(progress_floor[0],min(95,float(value)))
        raw_progress(message,round(progress_floor[0]))
    directory = Path(directory)
    progress('转换并检查音频', 3)
    with stage('simple.convert_and_tail_analyze', source=file_identity(source),
               tail_trim_enabled=options.get('tail_trim_enabled', False)):
        y, sr, duration, audio = convert(source, directory, options.get('tail_trim_enabled', False))
    use_v32 = options.get('engine', 'mug') == 'v32'
    if whole_song and use_v32:
        from .simple_generation import waveform_only
        progress('准备音乐波形', 8)
        with stage('simple.waveform_prepare', sample_rate=sr, samples=len(y), v32=True):
            analysis = waveform_only(y)
    else:
        progress('分析节拍与音乐波形', 8)
        with stage('simple.audio_and_tempo_analysis', sample_rate=sr, samples=len(y), manual_bpm=options.get('bpm')):
            analysis = analyze(y, sr, options.get('bpm'))
    log_event('analysis_result','simple.audio_analysis',analysis=analysis)
    from .advanced import defaults, merge
    dynamic_settings=merge(defaults(),options)
    dynamic_settings['engine']='v32' if use_v32 else 'mug'
    dynamic_settings['strategy']='fast'
    if whole_song:
        dynamic_settings['dynamic_enabled']=False
    quality_enabled=bool(options.get('quality_policy'))
    from .advanced_generation import frozen_candidate_options
    candidate_policy=frozen_candidate_options(options)['selection_policy']
    if quality_enabled:
        from .advanced_generation import validate_frozen_policies
        validate_frozen_policies(options)
        dynamic_settings.update({k:options[k] for k in ('quality_policy','density_policy','candidate_policy') if k in options})
    if 'conditions' not in options:
        dynamic_settings['conditions']={'v32':{key:options.get('v32_difficulty',8) for key in PRESETS},
                                       'mug':{key:options.get('mug_difficulty',4) for key in PRESETS}}
    section_plan=None
    if not whole_song and (options.get('dynamic_enabled',False) or quality_enabled):
        from .section_plan import build_plan,canonical_hash
        reference = {**analysis, 'manual': True, 'points': [[0, analysis['bpm']]]} if options.get('bpm') is not None else analysis
        with stage('simple.build_section_plan', dynamic_settings=dynamic_settings, reference=reference):
            section_plan=build_plan(directory/'retained.wav', dynamic_settings, reference)
        if quality_enabled:
            section_plan['arrangement_enabled']=True
            body={k:v for k,v in section_plan.items() if k not in ('id','content_hash')}
            section_plan.update(id=canonical_hash(body),content_hash=canonical_hash(body))
        (directory/'section-plan.json').write_text(json.dumps(section_plan,ensure_ascii=False),encoding='utf-8')
    validation_plan=section_plan
    if whole_song:
        from .simple_generation import density_target
        with stage('simple.build_density_validation_plan', dynamic_settings=dynamic_settings):
            validation_plan=density_target(directory/'retained.wav',dynamic_settings)
        (directory/'simple-density-target.json').write_text(json.dumps(validation_plan,ensure_ascii=False),encoding='utf-8')
    patterns = options.get('patterns') or [options.get('pattern', 'balanced')]
    selected = [key for key in PRESETS if key in options['difficulties']]
    all_charts, results, previews, caches = {}, [], {}, {}
    quality_alerts, pattern_errors, chart_errors, engine_metadata = [], [], [], {}
    wave = None
    sectioned=None
    if section_plan:
        from .advanced_generation import generate_sectioned
        with stage('simple.sectioned_model_generation', plan_id=section_plan['id'], patterns=patterns,
                   difficulties=selected, engine=dynamic_settings['engine']):
            sectioned=generate_sectioned(directory/'retained.wav',directory/'sectioned-models',dynamic_settings,
                                         [{'key':chart_id(pattern,key),'pattern':pattern,'difficulty':key} for pattern in patterns for key in selected],
                                         section_plan,progress,raw_only=True)
        pattern_errors.extend(sectioned['errors'])
    elif not use_v32:
        with stage('simple.mug_prepare_wave', sample_rate=sr, samples=len(y)):
            wave = engine.prepare(y, sr, progress)
    else:
        import soundfile as sf
        engine.unload()
        input_wave = directory / 'v32-input.wav'
        with stage('simple.write_v32_input', output=input_wave, sample_rate=sr, samples=len(y)):
            sf.write(input_wave, y, sr, subtype='PCM_16')

    for pattern_index, pattern in enumerate(patterns):
        local_options = dict(options)
        local_options['pattern'] = pattern
        local_options['seed'] = options['seed'] if whole_song and len(patterns)==1 else _variant_seed(options['seed'], pattern)
        if sectioned:
            from .charts import Note
            cache_file=directory/'sectioned-models'/(pattern+'-mother.json')
            if not cache_file.is_file():continue
            raw_cache=json.loads(cache_file.read_text(encoding='utf-8'))
            master=[Note(*row) for row in raw_cache['raw'][raw_cache.get('mother_key') or 'master']]
            if use_v32:
                from .v32_rhythm import hydrate_cache
                hydrate_cache(raw_cache,{raw_cache.get('mother_key') or 'master':master},cache_file.parent)
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
                if not whole_song:
                    from .advanced_generation import attach_timing_reference
                    reference={**analysis,'manual':options.get('bpm') is not None}
                    attach_timing_reference(local_options,directory,{'tempo':reference},{'source_role':'mix'})
                with stage('simple.v32_pattern_generation', pattern=pattern, difficulties=selected,
                           condition=local_options.get('v32_difficulty'), seed=local_options.get('seed'),
                           source=file_identity(directory/'v32-input.wav')):
                    raw_charts, metadata = generate(directory / 'v32-input.wav', pattern_dir,
                        {**local_options, 'patterns': [pattern]},
                        lambda message, fraction: progress(
                            f'{PATTERN_LABELS[pattern]}：{message}',
                            12 + ((pattern_index + fraction / 100) / len(patterns)) * 74))
                log_event('model_result','simple.v32_pattern_generation',pattern=pattern,metadata=metadata,
                      chart_keys=list(raw_charts))
                master, timing, inherited = raw_charts[selected[0]]
                if whole_song:
                    from .simple_generation import hydrate_rhythm, contract
                    hydrate_rhythm(master,metadata)
                    if analysis['bpm'] is None:
                        # Display metadata comes from the successful model output.
                        # Export continues to use its complete, original timing map.
                        origin_bpm = next((b for t,b in reversed(timing) if t <= 0), timing[0][1])
                        analysis.update(bpm=round(origin_bpm,3), timing_source='model_output')
                    metadata['simple_generation_policy']=contract()
                    metadata['application_model_calls']=1
                    metadata['automatic_density_retries']=0
                engine_metadata[pattern] = metadata
            except Exception as exc:
                pattern_errors.append({'pattern': pattern, 'error': str(exc)})
                continue
        else:
            try:
                with stage('simple.mug_pattern_generation', pattern=pattern, difficulties=selected,
                           seed=local_options['seed'], settings=local_options):
                    master = engine.generate(wave, None, local_options,
                        lambda fraction: progress(f'{PATTERN_LABELS[pattern]} 母谱生成',
                            14 + ((pattern_index + fraction) / len(patterns)) * 72))
                timing, inherited = None, 0
                if whole_song:
                    from .simple_generation import contract
                    engine_metadata[pattern]={'simple_generation_policy':contract(),
                                              'application_model_calls':1,'automatic_density_retries':0}
            except Exception as exc:
                pattern_errors.append({'pattern': pattern, 'error': str(exc)})
                continue

        from .difficulty import attacks, calibrate
        if quality_enabled:
            from .adaptive_difficulty import model_candidates
            from .quality_workflow import evidence_for
            sound=evidence_for(directory,directory/'source.wav')
            from .chart_quality import classify_holds
            from .charts import Note
            available=master
            if not whole_song:
                mother_events=[{'id':'mother:'+str(i),'start_ms':n.start,'lane':n.lane,'end_ms':n.end}
                               for i,n in enumerate(master)]
                classified=classify_holds(mother_events,sound)
                available=[Note(e['start_ms'],e['lane'],e['end_ms']) for e in classified['events']]
                engine_metadata[pattern]['preplanning_hold_decisions']=classified['decisions']
            candidates=model_candidates(available,sound,selection_policy=candidate_policy)
        elif candidate_policy=='model_only':
            from .adaptive_difficulty import model_candidates
            candidates=model_candidates(master,selection_policy=candidate_policy)
        elif section_plan:
            from .adaptive_difficulty import attacks_adaptive
            with stage('simple.extract_timing_candidates', pattern=pattern, model_heads=len(master), adaptive=True):
                candidates=attacks_adaptive(y,sr,master)
        else:
            with stage('simple.extract_timing_candidates', pattern=pattern, model_heads=len(master), adaptive=False):
                candidates = attacks(y, sr, master)
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
            if section_plan or candidate_policy=='model_only':
                from .adaptive_difficulty import calibrate_adaptive
                with stage('simple.select_difficulty_notes', pattern=pattern, difficulty=key,
                           candidate_count=len(candidates), policy=candidate_policy):
                    notes,adjustment=calibrate_adaptive(candidates,duration*1000,key,options['ln_ratio'],local_options['seed'],options.get('difficulty_rules',{}).get(key),pattern,section_plan,
                        evidence=sound if quality_enabled else None,
                        selection_policy=candidate_policy,
                        audio_vote_cap=options.get('candidate_policy',{}).get('audio_vote_cap'))
            else:
                with stage('simple.select_difficulty_notes', pattern=pattern, difficulty=key,
                           candidate_count=len(candidates), settings=options.get('difficulty_rules',{}).get(key)):
                    notes, adjustment = calibrate(candidates, duration * 1000, key, options['ln_ratio'],
                        local_options['seed'], options.get('difficulty_rules', {}).get(key), pattern=pattern)
            log_event('difficulty_selection_result','simple.select_difficulty_notes',pattern=pattern,difficulty=key,
                  selected_notes=len(notes),adjustment=adjustment)
            density_validation={'status':'not_evaluated','reason':'历史任务未冻结密度策略'}
            quality_summary=None
            if quality_enabled:
                from .chart_quality import apply
                from .advanced import as_notes
                from .density_validation import evaluate
                from .quality_workflow import candidate_events
                raw_identity=hashlib.sha256(json.dumps([[n.start,n.lane,n.end] for n in master]).encode()).hexdigest()
                raw_events=[{'id':raw_identity+':'+str(i),'start_ms':n.start,'lane':n.lane,'end_ms':n.end}
                            for i,n in enumerate(master)]
                if use_v32:
                    from .v32_rhythm import unavailable
                    for event,note in zip(raw_events,master):
                        event['model_rhythm']=getattr(note,'model_rhythm',unavailable('model_events_sidecar_missing'))
                events=candidate_events(notes,candidates,raw_events,raw_revision_id=raw_identity,
                    source_role='mix',source_id=validation_plan['source_pcm_sha'])
                checked=apply(events,dynamic_settings,sound,key,pattern,plan=section_plan,policy=options.get('quality_policy'))
                notes=as_notes(checked['events']);quality_summary=checked['summary']
                from .advanced_execution import spent_retry_rounds
                rounds=0 if whole_song else max((spent_retry_rounds(r,raw_cache.get('metadata')) for r in raw_cache.get('records',[])),default=0)
                density_validation=evaluate(checked['events'],validation_plan,key,[0,validation_plan['samples']],duration=duration,
                    candidates={'model_heads':len(master),
                        'candidate_heads':adjustment.get('candidate_model_heads',len(master))+adjustment.get('candidate_acoustic_heads',0),
                        'acoustic_candidate_heads':adjustment.get('candidate_acoustic_heads'),
                        'constraint_removed':adjustment.get('phone_filtered_notes',0)},attempts=rounds)
                if whole_song:
                    density_validation.update(retry_allowed=False,automatic_retry_enabled=False,
                                              denominator=validation_plan['measurement']['version'])
                (directory/('quality-'+variant+'.json')).write_text(json.dumps({**checked,'density_validation':density_validation,
                    'candidate_policy':options.get('candidate_policy')},ensure_ascii=False),encoding='utf-8')
            from .naming import chart_label
            version_name = chart_label(pattern,key)
            with stage('simple.serialize_chart',pattern=pattern,difficulty=key,notes=len(notes),
                       engine='v32' if use_v32 else 'mug',timing_points=len(timing or [])):
                if use_v32:
                    from .mapperatorinator import serialize_with_timing
                    chart = serialize_with_timing(notes, options['title'], options['artist'], version_name, timing)
                    chart['meta']['creator'] = options.get('creator', 'Malody Chart Forge / Mapperatorinator V32 (AI)')
                else:
                    chart = serialize(notes, options['title'], options['artist'], version_name, analysis['bpm'])
                    chart['meta']['creator'] = options.get('creator', 'Malody Chart Forge / MuG Diffusion v1.0.0')
            try:
                with stage('simple.validate_chart_structure',pattern=pattern,difficulty=key,note_events=len(chart.get('note',[])),
                           duration_ms=duration*1000):
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
            with stage('simple.assess_audio_and_density',pattern=pattern,difficulty=key,notes=len(notes),
                       target_nps=adjustment['target_active_nps'],sample_rate=sr):
                alerts = assess(notes, duration, adjustment['target_active_nps'], y, sr, chart, key)
            for alert in alerts:
                alert.update(pattern=pattern, chart_id=variant)
            quality_alerts.extend(alerts)
            all_charts[variant] = chart
            pattern_charts[variant] = chart
            filename = variant + '.mc'
            results.append({'key': variant, 'chart_id': variant, 'difficulty': key,
                'pattern': pattern, 'pattern_label': PATTERN_LABELS[pattern], 'label': preset['label'],
                'filename': filename,
                'model_strength': options.get('v32_difficulty', 8) if use_v32 else options.get('mug_difficulty', 4),
                'removed_notes': adjustment['phone_filtered_notes'], 'model_raw_notes': len(master),
                'difficulty_adjustment': adjustment, 'validation': validation,
                'density_validation':density_validation,'quality_summary':quality_summary,
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
            'section_plan':section_plan, 'candidate_policy':{'selection_policy':candidate_policy},
            'candidates': cache_candidates, 'timings': {key: timing for key in selected} if use_v32 else {},
            'raw_count': len(master), 'options': {k: v for k, v in local_options.items()
                if not k.startswith('_')}}
        if whole_song:
            caches[pattern].update(simple_generation_policy=options['simple_generation_policy'],
                                   density_plan=validation_plan,raw_events=raw_events if quality_enabled else [])
        cache_dir = directory / 'chart-cache' / pattern
        cache_dir.mkdir(parents=True, exist_ok=True)
        with stage('simple.persist_generation_cache',pattern=pattern,cache_file=cache_dir/'generation-cache.json',
                   model_heads=caches[pattern]['raw_count'],candidate_count=len(caches[pattern]['candidates'])):
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
    if 'creator' in options:
        report['creator'] = options['creator']
    from .artwork import prepare_artwork
    progress('获取封面并检查曲包', 96)
    with stage('simple.prepare_artwork',video_id=options.get('artwork_video_id'),
               music_asset=options.get('_music_asset')):
        background, artwork = prepare_artwork(options.get('artwork_video_id'))
    if options.get('_music_asset'):
        from .music_assets import asset_cover
        saved_cover=asset_cover(options['_music_asset']['id'])
        if saved_cover:
            background=saved_cover
            artwork={'status':'ready','source':'registered_music','asset_id':options['_music_asset']['id']}
    report['artwork'] = artwork
    if background:
        for chart in all_charts.values():
            chart['meta']['background'] = 'background.jpg'
    elif options.get('artwork_video_id'):
        warnings.append('YouTube 封面获取失败，本次未包含背景图。')
    else:
        warnings.append('上传音乐未关联 YouTube 封面。')
    filenames = {row['chart_id']: row['filename'] for row in results}
    report['tail_trim']=json.loads((directory/'tail-analysis.json').read_text(encoding='utf-8'))
    report['download_name'] = archive_stem(options['title'], 'v32' if use_v32 else 'mug',
        [row['pattern'] for row in results], [row['difficulty'] for row in results]) + '.mcz'
    with stage('simple.package_mcz', chart_count=len(all_charts), audio=file_identity(audio),
               chart_files=filenames, background=background):
        archive = package(directory, all_charts, audio, report, background=background, filenames=filenames)
    from .library import publish
    publish(ROOT,directory.name,archive,report,source={'type':'song','job_id':directory.name})
    progress('生成完成', 100)
    return report, archive
