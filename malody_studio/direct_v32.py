"""Generate each difficulty/condition span directly; never apply an NPS quota."""
import copy
import hashlib
import json
from pathlib import Path
import time
import numpy as np
import soundfile as sf
from .paths import ROOT, PRESETS
from .nps_star_calibration import load_mapping, normalize_ranges, resolve_condition, chart_span_density

SR = 44100


def batch_identity(settings):
    from .v32_batch_streams import batch_identity as identity
    return identity(settings)


def stitch_boundary_tails(events, records, variant):
    """Candidate-only reconciliation of at most one V32 time step at a seam."""
    output = copy.deepcopy(events)
    cores = [r['core'] for r in records if r['variant'] == variant and r.get('status') == 'generated']
    def owner(event):
        return next((core for core in cores if core[0]*1000/SR <= event['start_ms'] < core[1]*1000/SR), None)
    pending, decisions = {}, []
    for event in sorted(output, key=lambda e:(e['start_ms'],e['lane'])):
        previous = pending.get(event['lane'])
        if previous and previous.get('end_ms') is not None and previous['end_ms'] > event['start_ms']+.1:
            left, right = owner(previous), owner(event)
            boundary = left[1]*1000/SR if left else None
            overlap = previous['end_ms']-event['start_ms']
            if (left and right and left[1] == right[0] and 0 < overlap <= 10.001
                    and boundary <= event['start_ms'] <= boundary+20.001
                    and previous['start_ms'] < boundary < previous['end_ms'] <= boundary+20.001):
                old_tail = previous['end_ms']
                previous['end_ms'] = event['start_ms']
                previous['tail_policy'] = dict(kind='cross_core_rearticulation_cap',model_end_ms=old_tail,
                                              next_model_head_ms=event['start_ms'])
                decisions.append(dict(type='boundary_hold_tail_cap',event_id=previous['id'],
                    next_event_id=event['id'],from_ms=old_tail,to_ms=event['start_ms'],
                    boundary_ms=boundary,overlap_ms=overlap,policy='direct-boundary-one-token-v1'))
        if previous is None or (event.get('end_ms') or event['start_ms']) >= (previous.get('end_ms') or previous['start_ms']):
            pending[event['lane']] = event
    return output, decisions


def silent_stem_request(role, message, request, full, rate, cache):
    """Measurements when a timing-less request on a mostly silent stem should be an empty
    contribution, else None. Only vocals/accompaniment input and only the exact upstream
    timing-empty error qualify; the level is measured on the audio actually sent to the model."""
    from . import stem_silence
    from .v32_recovery import timing_empty_message
    if role not in ('vocals', 'accompaniment') or not timing_empty_message(message):
        return None
    if 'levels' not in cache:
        cache['levels'] = stem_silence.frame_levels_db(full, rate)
        cache['peak'] = stem_silence.file_peak(full)
    core_ms = [value*1000/SR for value in request['core']]
    activity = stem_silence.range_activity(cache['levels'], *core_ms)
    if not activity['frames'] or activity['silent_fraction'] < stem_silence.REQUEST_SILENT_FRACTION:
        return None
    return {**stem_silence.policy_record(), **activity, 'source_audio': 'v32-direct-input.wav',
            'source_rate': rate, 'file_peak': cache['peak'], 'core_ms': core_ms, 'stem_role': role}


def reusable_cache(snapshot, requests, actual_sha):
    parent = snapshot.get('retry_of','')
    if len(parent) != 32 or any(c not in '0123456789abcdef' for c in parent):
        return None
    folder = ROOT/'outputs'/parent
    try:
        cache = json.loads((folder/'direct-generation-cache.json').read_text(encoding='utf-8'))
        previous = json.loads((folder/'queue-worker.json').read_text(encoding='utf-8'))['options']['_advanced']
        if (previous['settings'] != snapshot['settings'] or cache['policy'] != snapshot['direct_v32_policy']
                or cache.get('decode_batching_identity') != batch_identity(snapshot['settings'])
                or cache['source_pcm_sha256'] != actual_sha or cache['failed_variants'] or cache['errors']
                or len(cache['requests']) != len(requests)):
            return None
        for expected, saved in zip(requests,cache['requests']):
            if saved.get('status') != 'generated' or any(saved.get(k) != v for k,v in expected.items()):
                return None
        for variant in snapshot['variants']:
            raw = cache['raw'][variant['key']]
            if len(raw) != sum(r['owned_heads'] for r in cache['requests'] if r['variant'] == variant['key']):
                return None
        return cache
    except (OSError,ValueError,KeyError,TypeError):
        return None


def requests_for(plan, variants, settings, policy, bounds):
    """Coalesce adjacent equal classes only within one requested difficulty."""
    load_mapping(policy)
    ranges = normalize_ranges(settings.get('nps_ranges'), settings.get('difficulty_rules'))
    requests = []
    for variant in variants:
        spans = []
        for section in plan['sections']:
            left, right = max(bounds[0], section['core'][0]), min(bounds[1], section['core'][1])
            if left >= right:
                continue
            offset = section.get('bpm_bucket_offset',0) * settings.get('dynamic_strength',1.) * settings.get('bpm_bucket_range',.2)/.2
            resolved = resolve_condition(policy, ranges[variant['difficulty']], offset, settings.get('dynamic_enabled', True))
            detail = dict(section_id=section['id'], core=[left,right], bpm_bucket_id=section.get('bpm_bucket_id'), **resolved)
            if spans and spans[-1]['core'][1] == left and spans[-1]['sr'] == resolved['sr']:
                spans[-1]['core'][1] = right
                spans[-1]['sections'].append(detail)
            else:
                spans.append(dict(core=[left,right], sections=[detail], sr=resolved['sr']))
        for index, span in enumerate(spans):
            identity = dict(seed=settings['seed'], variant=variant['key'], core=span['core'], sr=span['sr'])
            seed = int(hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:8],16)%2147483641
            context = [max(0,span['core'][0]-4*SR),min(plan['samples'],span['core'][1]+4*SR)]
            requests.append(dict(key=variant['key']+'__direct'+str(index), label=variant['key'],
                                 variant=variant['key'], pattern=variant['pattern'], difficulty_key=variant['difficulty'],
                                 sr=span['sr'], seed=seed, core=span['core'], sections=span['sections'],
                                 start_time=int(round(context[0]*1000/SR)), end_time=int(round(context[1]*1000/SR)),
                                 core_start_time=int(round(span['core'][0]*1000/SR)),core_end_time=int(round(span['core'][1]*1000/SR)),
                                 context=context))
        cursor = bounds[0]
        for span in spans:
            if span['core'][0] != cursor:
                raise ValueError('直接生成分段未完整覆盖请求范围')
            cursor = span['core'][1]
        if cursor != bounds[1]:
            raise ValueError('直接生成分段未覆盖请求末端')
    return requests


def make_simple_plan(directory, settings, variants, policy, progress):
    from .workflow_log import stage, event
    from . import music_timing, arrangement
    info = sf.info(Path(directory)/'source.wav')
    project = dict(samples=info.frames, sample_rate=info.samplerate)
    with stage('direct_v32.music_evidence', source=str(Path(directory)/'source.wav'),
               sample_rate=info.samplerate, samples=info.frames, detector=policy['beat_analysis_policy']):
        evidence = music_timing.load_or_analyze(directory,project,progress,frozen_policy=policy['beat_analysis_policy'])
    with stage('direct_v32.select_timing_map', evidence_id=evidence.get('id'), candidates=[x.get('id') for x in evidence.get('candidates',[])]):
        timing = music_timing.select_timing(evidence)
    if not timing.get('provenance',{}).get('adapter','').startswith('beat_this'):
        raise ValueError('直接生成需要冻结的 Beat This 检测结果')
    with stage('direct_v32.build_bpm_bucket_plan', timing_map_id=timing.get('id'), settings=settings,
               variant_count=len(variants), bpm_bucket_version=policy.get('bpm_bucket_version')):
        # The version was frozen with the policy at submit; a historical policy has none (v2).
        from .bpm_buckets import DEFAULT_FROZEN_VERSION
        arranged = arrangement.build(evidence,timing,evidence['acoustic'],settings,variants,'keyboard',
                                     bucket_version=policy.get('bpm_bucket_version',DEFAULT_FROZEN_VERSION))
    event('plan_result','direct_v32.build_bpm_bucket_plan',section_plan=arranged['section_plan'],
          bpm_buckets=arranged['section_plan'].get('bpm_buckets'))
    return arranged['section_plan'], timing


def infer(source, directory, snapshot, progress):
    from .workflow_log import stage, event, file_identity
    from .mapperatorinator import generate
    from .advanced_generation import _stable_events
    from .advanced import atomic
    from .difficulty import V32_PATTERN_TAGS
    from .beat_analysis import mono_audio
    policy = snapshot['direct_v32_policy']
    load_mapping(policy)
    settings = snapshot['settings']
    directory = Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    with stage('direct_v32.load_and_verify_pcm', source=file_identity(source,hash_file=True),
               expected_source=snapshot.get('source'), expected_samples=snapshot['section_plan'].get('samples')):
        data, rate = sf.read(source,dtype='float32',always_2d=True)
    if rate != SR:
        raise ValueError('直接生成需要原曲 44100 Hz 采样时钟')
    descriptor = snapshot.get('source') or snapshot.get('original_source') or {}
    actual_sha = hashlib.sha256(data.astype('<f4').tobytes()).hexdigest()
    if descriptor.get('pcm_sha') and descriptor['pcm_sha'] != actual_sha:
        raise ValueError('直接生成输入与冻结音源不一致')
    plan = snapshot['section_plan']
    if plan['samples'] != len(data):
        raise ValueError('Beat This 分段计划与模型输入长度不一致')
    bounds = [snapshot['segment']['start_sample'],snapshot['segment']['end_sample']]
    from .audio_bounds import content_end
    source_stop = min(len(data),content_end(snapshot['project']))
    with stage('direct_v32.resolve_nps_to_star_requests', nps_ranges=settings.get('nps_ranges'),
               bpm_buckets=plan.get('bpm_buckets'), variants=snapshot['variants'], bounds=bounds):
        requests = requests_for(plan,snapshot['variants'],settings,policy,bounds)
    event('resolved_model_conditions','direct_v32.resolve_nps_to_star_requests',requests=requests)
    decode_identity = batch_identity(settings)
    atomic(directory/'direct-request-plan.json',dict(policy=policy, decode_batching_identity=decode_identity, section_plan_id=plan['id'],
                                                   source_pcm_sha256=actual_sha, requests=requests))
    cached = reusable_cache(snapshot,requests,actual_sha)
    if cached is not None:
        cached['reused_from_job'] = snapshot['retry_of']
        for record in cached['requests']:
            record['model_reused_cache'] = True
        atomic(directory/'direct-generation-cache.json',cached)
        event('native_cache_reused','direct_v32.generate_pattern',parent_job=snapshot['retry_of'],
              request_count=len(requests),new_model_calls=0)
        progress('复用已完成的模型原谱，重新检查候选',80)
        return cached['raw'],cached['requests'],[],set(),cached['metadata']
    model_input = directory/'v32-direct-input.wav'
    # Resampling changes the container sampling rate, never the absolute clock.
    import librosa
    with stage('direct_v32.prepare_v32_audio', source_samples=len(data), source_rate=rate,
               target_rate=22050, output=model_input):
        full = librosa.resample(mono_audio(data)[0],orig_sr=SR,target_sr=22050)
        sf.write(model_input,full,22050,subtype='FLOAT')
    supplied = {v['key']:[] for v in snapshot['variants']}
    records, errors, metadata = [], [], {}
    failed = set()
    role = snapshot.get('source', {}).get('source_role', 'mix')
    silence_cache = {}
    patterns = list(dict.fromkeys(v['pattern'] for v in snapshot['variants']))
    for pi, pattern in enumerate(patterns):
        selected = [r for r in requests if r['pattern']==pattern]
        if not selected:
            continue
        options = {**settings, 'title':snapshot['project']['title'], 'artist':snapshot['project']['artist'],
                   'difficulties':list(dict.fromkeys(r['difficulty_key'] for r in selected)),
                   '_advanced_presets':selected, 'v32_difficulty':selected[0]['sr'],
                   'direct_v32_policy':policy}
        options.pop('timing_reference',None)
        options.pop('timing_fallback_reference',None)
        tag = V32_PATTERN_TAGS.get(pattern)
        options['v32_descriptors'] = list(dict.fromkeys([*settings.get('v32_descriptors',[]), *([tag] if tag else [])]))
        folder = directory/pattern
        folder.mkdir(exist_ok=True)
        try:
            with stage('direct_v32.generate_pattern', pattern=pattern, request_count=len(selected),
                       requests=[{'key':r['key'],'sr':r['sr'],'seed':r['seed'],'core':r['core'],
                                  'context':[r['start_time'],r['end_time']]} for r in selected],
                       source=file_identity(model_input)):
                raw, detail = generate(model_input,folder,options,
                                       lambda message,value:progress(message,15+70*(pi+value/100)/len(patterns)))
            metadata[pattern] = detail
            event('pattern_generation_result','direct_v32.generate_pattern',pattern=pattern,metadata=detail,
                  returned_charts=list(raw))
        except Exception as exc:
            for r in selected:
                failed.add(r['variant'])
                errors.append(dict(variant=r['variant'],pattern=pattern,request_key=r['key'],core=r['core'],error=str(exc)))
                records.append({**copy.deepcopy(r),'status':'failed','error':str(exc)})
            continue
        for request in selected:
            record = copy.deepcopy(request)
            record['decode_batching'] = copy.deepcopy(detail.get('decode_batching'))
            if request['key'] not in raw:
                message = detail.get('rejected_charts',{}).get(request['key'],'模型未返回该独立请求的谱面')
                silent = silent_stem_request(role,message,request,full,22050,silence_cache)
                if silent is not None:
                    # Nothing to map: the stem is silent over this core. Not a failed difficulty.
                    percent = round(silent['silent_fraction']*100)
                    record.update(status='empty_silent_stem',model_heads=0,owned_heads=0,timing=None,inherited=None,
                                  suppressed_error=message,silence=silent)
                    errors.append(dict(variant=request['variant'],pattern=pattern,request_key=request['key'],core=request['core'],
                                       severity='warning',non_fatal=True,kind='empty_silent_stem',stage_role=role,
                                       error=f'声部在该请求核心区 {percent}% 为静音（低于峰值 {silent["threshold_db"]:g} dB），模型未生成节拍；按空贡献处理，不算失败',
                                       silence=silent))
                    records.append(record)
                    continue
                failed.add(request['variant'])
                errors.append(dict(variant=request['variant'],pattern=pattern,request_key=request['key'],core=request['core'],error=message))
                record.update(status='failed',error=message)
                records.append(record)
                continue
            notes, timing, inherited = raw[request['key']]
            a,b = (x*1000/SR for x in request['core'])
            with stage('direct_v32.own_core_events',request_key=request['key'],model_heads=len(notes),core=[a,b],
                       source_tail_ms=source_stop*1000/SR):
                events, diagnostics = _stable_events(notes,a,b,source_stop*1000/SR,request['key']+str(request['seed']),preserve_raw_heads=True)
            diagnostics['source_tail_clips'] = sum(n.end is not None and n.end>source_stop*1000/SR for n in notes if a<=n.start<b)
            supplied[request['variant']].extend(events)
            record.update(status='generated',model_heads=len(notes),owned_heads=len(events),
                          ownership=diagnostics, timing=timing, inherited=inherited)
            if diagnostics.get('discarded_invalid'):
                failed.add(request['variant'])
                record.update(status='failed_structure',error='核心区包含无效模型头；保留证据并拒绝成品')
                errors.append(dict(variant=request['variant'],pattern=pattern,request_key=request['key'],core=request['core'],error=record['error']))
            records.append(record)
    atomic(directory/'direct-generation-cache.json',dict(version=policy['version'], policy=policy,
                  decode_batching_identity=decode_identity,
                  source_pcm_sha256=actual_sha, requests=records, raw=supplied,
                  failed_variants=sorted(failed), metadata=metadata, errors=errors,
                  mother_chart=False, density_thinning=False, application_retries=0))
    return supplied, records, errors, failed, metadata


def quality_events(events, source, directory, snapshot, key, pattern):
    from .workflow_log import stage
    """Keep every model head; use the existing sustain/alignment quality pass."""
    from .quality_workflow import evidence_for
    from .chart_quality import apply
    original = snapshot.get('original_source',{}).get('path') or source
    role = snapshot.get('source',{}).get('source_role','mix')
    descriptors = [dict(source_role=role,path=str(source))] if role in ('vocals','accompaniment') else []
    with stage('quality.build_audio_evidence', original=original, descriptors=descriptors):
        evidence = evidence_for(directory,original,descriptors)
    with stage('quality.validate_model_events', variant=key, pattern=pattern, input_events=len(events),
               policy=snapshot.get('quality_policy') or snapshot['settings'].get('quality_policy')):
        result = apply(events,snapshot['settings'],evidence,key,pattern,
                       policy=snapshot.get('quality_policy') or snapshot['settings'].get('quality_policy'))
    if len(result['events']) != len(events) or {e['id'] for e in result['events']} != {e['id'] for e in events}:
        raise ValueError('直接生成质量处理不得增加或删除模型音符头')
    return result


def advanced_run(source,directory,options,progress):
    from .advanced import uid, as_notes, valid_events, RAW_HEAD_POLICY, atomic
    snapshot = options['_advanced']
    policy = snapshot['direct_v32_policy']
    supplied, records, errors, failed, metadata = infer(source,directory,snapshot,progress)
    # Non-fatal notices (empty contribution of a silent stem) are reported apart from failures.
    warnings = [e for e in errors if e.get('non_fatal')]
    errors = [e for e in errors if not e.get('non_fatal')]
    bounds = [snapshot['segment']['start_sample'],snapshot['segment']['end_sample']]
    a,b = (x*1000/SR for x in bounds)
    rows = []
    ranges = normalize_ranges(snapshot['settings'].get('nps_ranges'),snapshot['settings'].get('difficulty_rules'))
    for variant in snapshot['variants']:
        key, pattern, name = variant['difficulty'],variant['pattern'],variant['key']
        events = supplied[name]
        if name in failed:
            continue
        provenance = dict(direct_v32_policy=policy,mother_chart=False,global_budget_applied=False,
                          decode_batching_identity=batch_identity(snapshot['settings']),
                          decode_batching=metadata.get(pattern,{}).get('decode_batching'),
                          density_thinning=False,source_id=snapshot.get('source',{}).get('source_id','original'),
                          source_role=snapshot.get('source',{}).get('source_role','mix'),
                          parent_source_id=snapshot.get('source',{}).get('parent_source_id') or snapshot['project'].get('source_pcm_sha256'),
                          stem_set_id=snapshot.get('source',{}).get('stem_set_id'),
                          cache_file='direct-generation-cache.json',cache_job=Path(directory).name,
                          section_plan_id=snapshot['section_plan']['id'],core_conditions=[r for r in records if r['variant']==name],
                          model_version='Mapperatorinator V32 mania', raw_head_policy=RAW_HEAD_POLICY,
                          generation_context_policy=snapshot.get('generation_context_policy'))
        rid = uid()
        rows.append(dict(id=rid,variant=name,events=events,kind='stem_raw' if snapshot.get('_stem_raw_only') else 'model_raw',
                         settings=snapshot['settings'],provenance=provenance,activate_initial=False))
        if snapshot.get('_stem_raw_only'):
            continue
        try:
            stitched, seam_decisions = stitch_boundary_tails(events,records,name)
            atomic(Path(directory)/(name+'-boundary-stitch.json'),dict(policy='direct-boundary-one-token-v1',decisions=seam_decisions))
            checked = quality_events(stitched,source,directory,snapshot,key,pattern)
            candidate = checked['events']
            raw_by_id = {e['id']:e for e in events}
            for event in candidate:
                raw = raw_by_id[event['id']]
                event.setdefault('origins',[dict(revision_id=rid,note_id=raw['id'],
                    original_start_ms=raw['start_ms'],original_lane=raw['lane'],original_end_ms=raw.get('end_ms'))])
            valid_events(candidate,a,b,snapshot['project']['duration']*1000)
            density = chart_span_density(as_notes(candidate),ranges[key])
            rows.append(dict(id=uid(),variant=name,events=candidate,kind='direct',settings=snapshot['settings'],
                             provenance={**provenance,'parent':rid,'parents':[rid], 'quality_summary':checked['summary'],
                                         'quality_policy':snapshot.get('quality_policy'),'quality_decisions':checked.get('decisions',[]),
                                         'boundary_stitch_decisions':seam_decisions,
                                         'density_validation':density,'raw_head_policy':None},activate_initial=True))
        except Exception as exc:
            errors.append(dict(variant=name,pattern=pattern,error=str(exc),raw_revision_retained=True))
    if not rows:
        successful = sum(r.get('status') == 'generated' for r in records)
        silent = sum(r.get('status') == 'empty_silent_stem' for r in records)
        note = f'，另有 {silent} 个静音空请求' if silent else ''
        raise ValueError(f'独立 V32 生成未完整完成（{successful}/{len(records)} 个请求已生成{note}，原生结果保留）：'+json.dumps(errors,ensure_ascii=False))
    result = dict(advanced_result=rows,errors=errors,warnings=warnings,bounds=bounds,section_plan_id=snapshot['section_plan']['id'],
                  direct_v32_policy=policy,model_versions={v['pattern']:'Mapperatorinator V32 mania' for v in snapshot['variants']},timings={})
    if snapshot.get('generation_context_policy'):
        result.update(generation_context_policy=snapshot['generation_context_policy'],member_segment_ids=[p['id'] for p in snapshot.get('member_segments',[snapshot['segment']])])
    status_count = lambda name: sum(r.get('status') == name for r in records)
    atomic(Path(directory)/'direct-result-summary.json',dict(model_requests=len(records), errors=errors, warnings=warnings,
                                                          generated_requests=status_count('generated'),
                                                          empty_silent_requests=status_count('empty_silent_stem'),
                                                          failed_requests=sum(r.get('status') not in ('generated','empty_silent_stem') for r in records),
                                                          new_model_requests=sum(not r.get('model_reused_cache') for r in records),
                                                          mother_chart=False, density_thinning=False))
    return result


def simple_run(source,directory,options,progress):
    from .audio import convert
    from .simple_generation import validate, waveform_only
    from .advanced import defaults, merge, as_notes
    from .charts import chart_stats, validate_chart, package
    from .mapperatorinator import serialize_fixed_scroll
    from .naming import chart_id, chart_label, archive_stem, validate_creator
    from .difficulty import PATTERN_LABELS
    from .advanced import atomic
    from .workflow_log import stage, event, file_identity
    validate(options)
    if 'creator' in options:
        options = {**options, 'creator': validate_creator(options['creator'])}
    started = time.time()
    directory = Path(directory)
    with stage('direct_simple.convert_source_audio',source=file_identity(source),
               tail_trim_enabled=options.get('tail_trim_enabled',False)):
        y,sr,duration,audio = convert(source,directory,options.get('tail_trim_enabled',False))
    settings = merge(defaults(),{k:v for k,v in options.items() if k in defaults()})
    settings.update(engine='v32',strategy='independent',dynamic_enabled=options.get('dynamic_enabled',True))
    # This backend opt-in is intentionally absent from the UI/defaults schema.
    if 'parallel_streams' in options:
        from .v32_batch_streams import clamp_streams
        settings['parallel_streams'] = clamp_streams(options['parallel_streams'])
    patterns = options.get('patterns') or [options.get('pattern','balanced')]
    variants = [dict(key=chart_id(pattern,key),pattern=pattern,difficulty=key) for pattern in patterns for key in options['difficulties']]
    policy = options['direct_v32_policy']
    with stage('direct_simple.build_music_and_bpm_plan',settings=settings,variants=variants,policy=policy):
        plan,timing = make_simple_plan(directory,settings,variants,policy,progress)
    stop = min(timing['source']['effective_end_sample'],sf.info(directory/'retained.wav').frames)
    snapshot = dict(project=dict(title=options['title'],artist=options['artist'],duration=duration,samples=int(round(duration*SR))),
                    segment=dict(id='whole-song',start_sample=0,end_sample=stop), settings=settings,
                    variants=variants,section_plan=plan,direct_v32_policy=policy,
                    original_source=dict(path=str(directory/'source.wav')),quality_policy=options.get('quality_policy'))
    with stage('direct_simple.generate_independent_v32_variants',plan_id=plan.get('id'),nps_ranges=settings.get('nps_ranges'),
               bpm_buckets=plan.get('bpm_buckets'),variants=variants):
        supplied,records,errors,failed,metadata = infer(directory/'source.wav',directory/'direct-models',snapshot,progress)
    charts,results,previews = {},[],{}
    ranges = normalize_ranges(settings.get('nps_ranges'),settings.get('difficulty_rules'))
    for variant in variants:
        name,key,pattern = variant['key'],variant['difficulty'],variant['pattern']
        if name in failed:
            continue
        try:
            with stage('direct_simple.quality_check_variant',variant=name,model_events=len(supplied[name]),nps_range=ranges[key]):
                checked = quality_events(supplied[name],directory/'retained.wav',directory,snapshot,key,pattern)
            notes = as_notes(checked['events'],end=duration*1000)
            if not notes:
                raise ValueError('模型没有生成可导出的音符')
            with stage('direct_simple.serialize_validate_variant',variant=name,notes=len(notes),fixed_scroll_bpm=120):
                chart = serialize_fixed_scroll(notes,options['title'],options['artist'],chart_label(pattern,key))
                if 'creator' in options:
                    chart['meta']['creator'] = options['creator']
                validated = validate_chart(chart,duration*1000)
            density = chart_span_density(notes,ranges[key])
            stats = chart_stats(notes,duration)
            charts[name] = chart
            previews[name] = [[n.start,n.lane,n.end] for n in notes]
            results.append(dict(key=name,chart_id=name,difficulty=key,pattern=pattern,pattern_label=PATTERN_LABELS[pattern],
                                label=PRESETS[key]['label'],filename=name+'.mc',validation=validated,
                                density_validation=density,quality_summary=checked['summary'],quality_alerts=[] if density['status']=='in_range' else [dict(pattern=pattern,difficulty=key,chart_id=name,severity='warning',message='实际谱面跨度 NPS 不在所选范围内')],
                                model_raw_notes=len(supplied[name]),removed_notes=0,
                                difficulty_adjustment=dict(target_active_nps=(ranges[key]['min']+ranges[key]['max'])/2,
                                                           mother_chart=False,density_thinning=False), **stats))
        except Exception as exc:
            errors.append(dict(variant=name,pattern=pattern,error=str(exc)))
    if not charts:
        raise ValueError('独立生成没有可导出的谱面：'+json.dumps(errors,ensure_ascii=False))
    from .artwork import prepare_artwork
    with stage('direct_simple.prepare_artwork',video_id=options.get('artwork_video_id')):
        background,artwork = prepare_artwork(options.get('artwork_video_id'))
    if options.get('_music_asset'):
        from .music_assets import asset_cover
        saved = asset_cover(options['_music_asset']['id'])
        if saved:background=saved;artwork=dict(status='ready',source='registered_music',asset_id=options['_music_asset']['id'])
    if background:
        for chart in charts.values():chart['meta']['background']='background.jpg'
    analysis = waveform_only(y)
    analysis.update(bpm=120.,timing_source='fixed_export_clock',beat_analysis_adapter=timing['provenance']['adapter'])
    report = dict(schema_version=3,title=options['title'],artist=options['artist'],duration=duration,bpm=120.,
                  engine='Mapperatorinator V32 mania',device='desktop',engine_metadata=metadata,seed=options['seed'],
                  requested_ln_ratio=options['ln_ratio'],steps=None,elapsed_seconds=round(time.time()-started,1),
                  patterns=patterns,requested_patterns=patterns,charts=results,difficulties=results,analysis=analysis,
                  previews=previews,quality_alerts=[a for r in results for a in r['quality_alerts']],pattern_errors=[],chart_errors=errors,failed_combinations=errors,
                  partial=bool(errors),direct_v32_policy=policy,section_plan_id=plan['id'],
                  model_requests=records,application_model_calls=len(records),mother_chart=False,density_thinning=False,
                  generation_settings=options,source=options.get('source','用户上传'),artwork=artwork,
                  audio_format='OGG Vorbis / 44100 Hz / stereo',
                  timing_policy='fixed-scroll-120-v1; Beat This for BPM buckets, model note times retained',
                  warnings=['NPS 曲线是经验映射；实际物量见谱面跨度 NPS 范围检查，未通过时不删键凑数。'],
                  download_name=archive_stem(options['title'],'v32',patterns,options['difficulties'])+'.mcz',
                  tail_trim=json.loads((directory/'tail-analysis.json').read_text(encoding='utf-8')))
    if 'creator' in options:
        report['creator'] = options['creator']
    atomic(directory/'direct-section-plan.json',plan)
    with stage('direct_simple.package_mcz',chart_count=len(charts),audio=file_identity(audio),
               charts=[r['key'] for r in results],background=background):
        archive = package(directory,charts,audio,report,background=background,filenames={r['key']:r['filename'] for r in results})
    from .library import publish
    publish(ROOT,directory.name,archive,report,source=dict(type='song',job_id=directory.name))
    progress('独立生成完成',100)
    return report,archive
