"""Isolated segment inference. Workers return artifacts; only the server selects versions."""
import hashlib
import json
import traceback
from pathlib import Path
import numpy as np
import soundfile as sf
import librosa
from .advanced import SR, RAW_HEAD_POLICY, uid, atomic, as_notes
from .charts import Note
from .beat_analysis import mono_audio
from .difficulty import attacks, calibrate, V32_PATTERN_TAGS
from .density_calibration import highest_requested, condition_next, initial_condition
from .density_validation import evaluate as evaluate_density, candidate_rank

def stable_seed(base, pattern, difficulty):
    return (int(base)+int(hashlib.sha256((pattern+'--'+difficulty).encode()).hexdigest()[:8],16))%2147483640

def _v32_time_bounds(sample_range):
    """Quantize sample bounds to the integer-ms timeline emitted by osu files."""
    return tuple(int(round(sample * 1000 / SR)) for sample in sample_range)

def _v32_owned_notes(notes, sample_range):
    start_ms,end_ms=_v32_time_bounds(sample_range)
    return [note for note in notes if start_ms<=note.start<end_ms]

def owned(raw, start, end, source_end, *, preserve_raw_heads=False):
    events=[]; dropped=[]; occupied=[-1e9]*4; heads=[-1e9]*4;leading=[];retained=[]
    for n in sorted(raw,key=lambda n:(n.start,n.lane)):
        if n.end is not None and n.start<start<n.end:leading.append({'lane':n.lane,'start_ms':n.start,'end_ms':n.end,'type':'head_outside_segment'})
        if not start<=n.start<end:continue
        if n.lane not in range(4) or not np.isfinite(n.start) or n.start<=heads[n.lane]+.1 or (not preserve_raw_heads and n.start<occupied[n.lane]-.1) or (n.end is not None and (not np.isfinite(n.end) or n.end<=n.start)):
            dropped.append({'start_ms':n.start,'lane':n.lane,'reason':'结构无效或占轨冲突'});continue
        if preserve_raw_heads and n.start<occupied[n.lane]-.1:
            retained.append({'start_ms':float(n.start),'lane':int(n.lane),
                             'occupied_until_ms':float(occupied[n.lane]),'reason':'模型头保全；可玩候选另行检查占轨'})
        tail=min(n.end,source_end) if n.end is not None else None
        event={'id':uid(),'start_ms':float(n.start),'end_ms':float(tail) if tail is not None else None,'lane':int(n.lane)}
        if hasattr(n,'model_rhythm'):
            from copy import deepcopy
            event['model_rhythm']=deepcopy(n.model_rhythm)
        events.append(event)
        until=min(tail,end) if tail is not None else n.start
        occupied[n.lane]=max(occupied[n.lane],until) if preserve_raw_heads else until;heads[n.lane]=n.start
    return events,{'discarded_invalid':dropped,'excluded_leading_holds':leading,
                   **({'retained_model_head_conflicts':retained} if preserve_raw_heads else {})}

def timing_reference_info(p):
    rescue=p.get('timing_rescue')
    if rescue:
        from .paired_timing import _reference_project
        checked=_reference_project({k:v for k,v in p.items() if k!='timing_rescue'},rescue['points'],
                                   rescue['source'],rescue.get('authorized_missing_timing') is True)
        return {**checked['timing_rescue'],'uncertain':True,'source_pcm_sha256':p.get('source_pcm_sha256')}
    timing=p.get('timing_map')
    confirmed_manual=p.get('tempo',{}).get('manual') and not p.get('tempo',{}).get('uncertain',False)
    if timing is not None:
        from .music_timing import validate_timing
        validate_timing(timing)
        if (not timing['eligibility']['v32'] or not timing['tempo_points']) and not confirmed_manual:
            raise ValueError('共享节拍相位或拍号仍有歧义，不能提供强制节拍参考')
        if timing['eligibility']['v32'] and timing['tempo_points'] and not confirmed_manual:return {'source':'shared_timing_map','timing_map_id':timing['id'],'uncertain':False,
                'points':[[row['sample']*1000/timing['source']['sample_rate'],float(row['bpm'])] for row in timing['tempo_points']],
                'meter':timing['meter'],'source_pcm_sha256':timing['source']['pcm_sha256'],
                'confirmed':False,'reason':'冻结原曲实测拍号与相位；所有音轨共享绝对时钟'}
    tempo = p.get('tempo', {})
    if tempo.get('reference_available') is False or (not tempo.get('manual') and not tempo.get('points') and any('暂用' in w for w in tempo.get('warnings',[]))):
        raise ValueError('原曲节拍参考不可用，请先确认项目 BPM 锚点')
    if not confirmed_manual:
        raise ValueError('自动或未确认项目拍速不能作为强制节拍参考')
    points = tempo.get('points') or [[0,tempo.get('bpm')]]
    previous = -1
    for row in points:
        if not isinstance(row,(list,tuple)) or len(row)!=2 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not np.isfinite(v) for v in row) or not row[0]>previous or not 20<=row[1]<=600:
            raise ValueError('原曲节拍参考不可用，请先确认项目 BPM 锚点')
        previous = row[0]
    source = tempo.get('reference_source') or ('user_confirmed' if tempo.get('manual') else 'project_tempo_reference' if tempo.get('points') else 'original_audio_analysis')
    return {'source':source,'uncertain':bool(tempo.get('uncertain',not tempo.get('manual',False))),
            'points':[[float(t),float(bpm)] for t,bpm in points],
            'source_pcm_sha256':p.get('source_pcm_sha256'), 'points_metadata':tempo.get('points_metadata'),
            'reason':'声部缺少可靠节拍上下文时复用原曲参考；不将自动估计标为已确认'}


def write_reference(path,p):
    reference=timing_reference_info(p);points=reference['points'];meter=reference.get('meter',4)
    text='osu file format v14\n\n[General]\nAudioFilename: source.wav\nMode: 3\n\n[Metadata]\nTitle: Segment reference\nArtist: Local\nCreator: Startrail\nVersion: Timing\n\n[Difficulty]\nCircleSize:4\nOverallDifficulty:8\nHPDrainRate:5\nApproachRate:5\nSliderMultiplier:1.4\nSliderTickRate:1\n\n[TimingPoints]\n'
    text+='\n'.join(f'{t},{60000/bpm},{meter},1,0,100,1,0' for t,bpm in points)+'\n\n[HitObjects]\n'
    path.write_text(text,encoding='utf-8');return path


def attach_timing_reference(local, directory, project, descriptor):
    try:
        reference = str(write_reference(directory/'timing-fallback.osu',project))
    except ValueError as exc:
        if project.get('timing_map') is not None:local['timing_reference_rejection']=str(exc)
        return
    local['timing_fallback_reference'] = reference
    tempo = project.get('tempo', {})
    if project.get('timing_rescue') or project.get('timing_map') or tempo.get('manual'):
        local['timing_reference'] = reference

def run(source,directory,options,progress):
    from .workflow_log import stage, event, file_identity
    from .audio_bounds import content_end, exact_silence
    import copy
    options=copy.deepcopy(options)
    snapshot=options['_advanced'];p=snapshot['project'];segment=snapshot['segment']
    from .generation_context import continuous
    continuous_model=continuous(snapshot)
    with stage('advanced.verify_frozen_request', source=file_identity(source), project=p,
               segment=segment, variants=snapshot.get('variants'), settings=snapshot.get('settings'),
               plan_id=(snapshot.get('section_plan') or {}).get('id'), direct_v32=bool(snapshot.get('direct_v32_policy'))):
        p.setdefault('samples', sf.info(source).frames)
    original_range=[segment['start_sample'],segment['end_sample']]
    stop=min(segment['end_sample'],content_end(p))
    if stop<=segment['start_sample']:
        return {'advanced_result':[],'errors':[],'bounds':original_range,'skipped':'静音尾段已排除，无需生成'}
    if not continuous_model or snapshot.get('direct_v32_policy'):segment['end_sample']=stop
    descriptor=snapshot.get('source',{})
    original_path=snapshot.get('original_source',{}).get('path')
    original=Path(original_path) if original_path else (Path(source) if descriptor.get('source_role','mix')=='mix' else None)
    with stage('advanced.detect_silent_core', original=original, core=[segment['start_sample'],stop]):
        silent=bool(original and original.is_file() and exact_silence(original,segment['start_sample'],stop))
    if silent:
        progress('静音片段，无需模型推理',98)
        rows=[{'variant':v['key'],'events':[],'settings':snapshot['settings'],
               'kind':'stem_raw' if snapshot.get('_stem_raw_only') else 'silence','activate_initial':not snapshot.get('_stem_raw_only',False),
               'provenance':{**{k:v for k,v in descriptor.items() if k!='path'},'silence_verified':True,
                   'effective_range':[segment['start_sample'],stop],'inference_count':0}}
              for v in snapshot['variants']]
        return {'advanced_result':rows,'errors':[],'bounds':original_range}
    with stage('advanced.generate_frozen_variants', source=file_identity(source), segment=segment,
               continuous=continuous_model, engine=snapshot.get('settings',{}).get('engine'),
               variants=snapshot.get('variants'), direct_v32=bool(snapshot.get('direct_v32_policy'))):
        result=_run_untrimmed(source,directory,options,progress)
    result['bounds']=original_range
    for row in result['advanced_result']:
        row['provenance']['effective_range']=[segment['start_sample'],stop]
    event('generation_result','advanced.generate_frozen_variants',errors=result.get('errors'),
          result_count=len(result.get('advanced_result',[])),bounds=result.get('bounds'),
          section_plan_id=result.get('section_plan_id'),model_versions=result.get('model_versions'))
    return result


def _run_untrimmed(source,directory,options,progress):
    if options['_advanced'].get('direct_v32_policy'):
        from .direct_v32 import advanced_run
        return advanced_run(source,directory,options,progress)
    from .generation_context import continuous
    if continuous(options['_advanced']) or options['_advanced']['settings'].get('dynamic_enabled',False) or options['_advanced'].get('arrangement_plan',{}).get('sections') or options['_advanced'].get('density_policy',options['_advanced']['settings'].get('density_policy')):
        return _run_dynamic(source,directory,options,progress)
    snapshot=options['_advanced'];p=snapshot['project'];s=snapshot['segment'];settings=snapshot['settings'];selected=snapshot['variants']
    directory=Path(directory);directory.mkdir(exist_ok=True)
    from .workflow_log import stage, event, file_identity
    with stage('advanced.read_and_verify_pcm', source=file_identity(source,hash_file=True),
               expected_source=snapshot.get('source'), expected_sample_rate=SR):
        data,rate=sf.read(source,dtype='float32',always_2d=True)
    if rate!=SR:raise ValueError('高级项目 PCM 采样率错误')
    descriptor = snapshot.get('source')
    if descriptor:
        from .separation import pcm_hash
        if len(data) != descriptor['frame_count'] or pcm_hash(data) != descriptor.get('pcm_sha'):raise ValueError('生成输入与冻结音源快照不匹配')
    start,end=s['start_sample']*1000/SR,s['end_sample']*1000/SR
    from .audio_bounds import content_end
    source_stop=min(len(data),content_end(p))
    a=max(0,s['start_sample']-4*SR);b=min(source_stop,s['end_sample']+4*SR);context_start=a*1000/SR
    with stage('advanced.prepare_context_audio',source_range=[a,b],context_range=[a,b],source_samples=len(data),
               source_rate=rate,target_rate=22050):
        y=librosa.resample(mono_audio(data[a:b])[0],orig_sr=SR,target_sr=22050);duration=(b-a)*1000/SR
    patterns=list(dict.fromkeys(v['pattern'] for v in selected));output=[];errors=[]
    use_v32=settings['engine']=='v32';engine=None;wave=None
    if not use_v32:
        from .engine import Engine
        with stage('advanced.prepare_mug_latent',pattern_count=len(patterns),sample_count=len(y)):
            engine=Engine();wave=engine.prepare(y,22050,progress)
    else:
        full=librosa.resample(mono_audio(data[:source_stop])[0],orig_sr=SR,target_sr=22050)
        sf.write(directory/'v32-input.wav',full,22050,subtype='FLOAT')
    for pi,pattern in enumerate(patterns):
        keys=[v['difficulty'] for v in selected if v['pattern']==pattern]
        conditions=settings['conditions'][settings['engine']]
        fast=settings['strategy']=='fast';mother_key=highest_requested(keys,settings.get('difficulty_rules'));infer_keys=[mother_key] if fast else keys
        local={**settings,'title':p['title'],'artist':p['artist'],'pattern':pattern,'patterns':[pattern],'difficulties':keys}
        local['v32_descriptors']=list(dict.fromkeys([*settings['v32_descriptors'],*([V32_PATTERN_TAGS[pattern]] if pattern in V32_PATTERN_TAGS else [])]))
        if len(local['v32_descriptors'])>4:raise ValueError('排键倾向与 V32 标签合计不能超过四个')
        if local['v32_negative_descriptors'] and (settings['v32_cfg_scale']<=1 or len(local['v32_negative_descriptors'])!=len(local['v32_descriptors'])):raise ValueError('排除标签数量须匹配正向标签，且 CFG 大于 1')
        raw={};metadata={}
        try:
            if use_v32:
                from .mapperatorinator import generate
                pattern_dir=directory/pattern;pattern_dir.mkdir()
                context_bounds=_v32_time_bounds((a,b))
                core_bounds=_v32_time_bounds((s['start_sample'],s['end_sample']))
                local.update(start_time=context_bounds[0],end_time=context_bounds[1],_advanced_presets=[{
                    'key':key,'label':key,'sr':conditions[mother_key] if fast else conditions[key],
                    'requested_condition':conditions[mother_key] if fast else conditions[key],
                    'condition_rationale':'frozen_requested_condition',
                    'seed':stable_seed(settings['seed'],pattern,key),
                    'start_time':context_bounds[0],'end_time':context_bounds[1],
                    'core_start_time':core_bounds[0],'core_end_time':core_bounds[1],
                    'core':[s['start_sample'],s['end_sample']],'context':[a,b]} for key in infer_keys])
                attach_timing_reference(local,directory,p,descriptor)
                with stage('advanced.v32_pattern_generation',pattern=pattern,requested_difficulties=keys,
                           inferred_difficulties=infer_keys,conditions=local.get('_advanced_presets'),
                           source=file_identity(directory/'v32-input.wav')):
                    results,metadata=generate(directory/'v32-input.wav',pattern_dir,local,progress)
                event('pattern_result','advanced.v32_pattern_generation',pattern=pattern,metadata=metadata,
                      returned_keys=list(results))
                if local.get('timing_reference'):metadata['timing_reference']=timing_reference_info(p)
                raw={key:result[0] for key,result in results.items()}
                metadata['timing']={key:result[1] for key,result in results.items()}
            else:
                for key in infer_keys:
                    cond=conditions[mother_key] if fast else conditions[key];local.update(seed=stable_seed(settings['seed'],pattern,key),mug_difficulty=cond)
                    result=engine.generate(wave,None,local,lambda x:progress(f'{pattern} · {key} 模型推理',20+(pi+x)/len(patterns)*65))
                    raw[key]=[Note(n.start+context_start,n.lane,n.end+context_start if n.end is not None else None) for n in result]
            cache={'format':1,'range':[s['start_sample'],s['end_sample']],'context_range':[a,b],'model':settings['engine'],'model_version':'MuG v1.0.0' if not use_v32 else 'Mapperatorinator V32',
                   'settings':settings,'pattern':pattern,'metadata':metadata,
                   'candidate_policy':snapshot.get('candidate_policy',settings.get('candidate_policy')),
                   'raw':{key:[[n.start,n.lane,n.end] for n in rows] for key,rows in raw.items()}}
            if use_v32:
                from .v32_rhythm import note_evidence
                cache['model_rhythm']={key:note_evidence(notes) for key,notes in raw.items()}
            cache_file=directory/(pattern+'-mother.json');atomic(cache_file,cache)
            if fast:
                master=raw[mother_key];relative=[Note(n.start-context_start,n.lane,n.end-context_start if n.end is not None else None) for n in master]
                from .adaptive_difficulty import model_candidates
                selection_policy=frozen_candidate_options(snapshot)['selection_policy']
                candidates=(model_candidates(relative,selection_policy=selection_policy)
                            if selection_policy=='model_only' else attacks(y,22050,relative))
                cache['candidates']=[[t,strength,[[n.start,n.lane,n.end] for n in ns]] for t,strength,ns in candidates];atomic(cache_file,cache)
            for key in keys:
                if (mother_key if fast else key) not in raw:
                    errors.append({'pattern':pattern,'difficulty':key,'error':metadata.get('rejected_charts',{}).get(key,'模型输出未通过校验')})
                    continue
                if snapshot.get('_stem_raw_only'):
                    original=raw[mother_key] if fast else raw[key]
                elif fast:
                    if frozen_candidate_options(snapshot)['selection_policy']=='model_only':
                        from .adaptive_difficulty import calibrate_adaptive
                        n,_=calibrate_adaptive(candidates,duration,key,settings['ln_ratio'],stable_seed(settings['seed'],pattern,key),settings['difficulty_rules'][key],pattern,min_notes=0,**frozen_candidate_options(snapshot))
                    else:
                        n,_=calibrate(candidates,duration,key,settings['ln_ratio'],stable_seed(settings['seed'],pattern,key),settings['difficulty_rules'][key],pattern,min_notes=0)
                    original=[Note(x.start+context_start,x.lane,x.end+context_start if x.end is not None else None) for x in n]
                else:original=raw[key]
                if use_v32:
                    from .v32_rhythm import transfer_notes
                    transfer_notes(original,raw[mother_key if fast else key],(descriptor or {}).get('source_role'))
                events,diagnostics=owned(original,start,end,len(data)*1000/SR)
                output.append({'variant':pattern+'--'+key,'events':events,'kind':'stem_raw' if snapshot.get('_stem_raw_only') else 'fast' if fast else 'model_raw','settings':settings,
                    'provenance':{'seed':stable_seed(settings['seed'],pattern,mother_key if fast else key),'model_version':cache['model_version'],'cache_job':directory.name,'cache_file':cache_file.name,'context_range':[a,b],'timing_reference':metadata.get('timing_reference'),
                        'candidate_policy':snapshot.get('candidate_policy',settings.get('candidate_policy')),
                        'serialization_tempo':({'points':metadata['timing'][mother_key if fast else key],'source':'model_output'} if metadata.get('timing',{}).get(mother_key if fast else key) else {**p['tempo'],'source':'frozen_coordinate_clock','reference_available':False}),
                        **({k:v for k,v in descriptor.items() if k!='path'} if descriptor else {}),**diagnostics}})
        except Exception as exc:errors.append({'pattern':pattern,'error':str(exc)})
    if engine:engine.unload()
    if not output:raise ValueError('所有片段组合生成失败：'+json.dumps(errors,ensure_ascii=False))
    progress('片段候选版本已生成',98)
    return {'advanced_result':output,'errors':errors,'bounds':[s['start_sample'],s['end_sample']]}


def _dynamic_seed(base,engine,descriptor,pattern,key,core,retry=0):
    from .section_plan import canonical_hash
    return int(canonical_hash({'base':int(base),'engine':engine,'role':descriptor.get('source_role','mix'),
                              'source':descriptor.get('pcm_sha'),'pattern':pattern,'difficulty':key,
                              'core':core,'retry':retry})[:8],16)%2147483640


V32_CONDITION_GROUP_TOLERANCE = 0.


def _coalesce_v32_requests(requests, tolerance=V32_CONDITION_GROUP_TOLERANCE, max_span_samples=None):
    """Merge adjacent, near-identical V32 conditions into one model pass.

    The plan still retains its original per-section conditions and quality
    targets. Coalescing only removes repeated inference setup where the model
    conditions differ by at most ``tolerance``; returned notes are assigned
    back to their original half-open section cores by the caller.
    """
    if not requests:
        return [], {}
    groups=[]
    for difficulty in dict.fromkeys(row['difficulty_key'] for row in requests):
        rows=sorted((row for row in requests if row['difficulty_key']==difficulty),
                    key=lambda row:(row['core'][0],row['core'][1]))
        current=[]
        for row in rows:
            conditions=[float(item['sr']) for item in current]+[float(row['sr'])]
            contiguous=not current or current[-1]['core'][1]==row['core'][0]
            span_too_long=(max_span_samples is not None and current and
                           row['core'][1]-current[0]['core'][0]>max_span_samples)
            def retry_signature(item):
                return (item.get('disable_density_retry',False),item.get('density_policy'),
                        [r['condition'] for r in item.get('density_rounds',[])],
                        item.get('retry_condition'))
            incompatible=current and retry_signature(current[0])!=retry_signature(row)
            if current and current[0].get('bpm_bucket_id')!=row.get('bpm_bucket_id'):
                incompatible=True
            if current and (not contiguous or max(conditions)-min(conditions)>tolerance or span_too_long or incompatible):
                groups.append(current);current=[]
            current.append(row)
        if current:groups.append(current)
    merged=[];owners={}
    for index,members in enumerate(groups):
        first,last=members[0],members[-1]
        a,b=first['core'][0],last['core'][1]
        durations=[max(0,row['core'][1]-row['core'][0]) for row in members]
        total=max(1,sum(durations))
        condition=sum(float(row['sr'])*duration for row,duration in zip(members,durations))/total
        key=f"{first['difficulty_key']}__group-{index}-{a}-{b}"
        grouped={**first,'key':key,'label':f"{first['difficulty_key']} · 合并{len(members)}段",
                 'sr':condition,'core':[a,b],'context':[first['context'][0],last['context'][1]],
                 'section_id':key,'start_time':first['start_time'],'end_time':last['end_time'],
                 '_member_keys':[row['key'] for row in members]}
        from .section_plan import canonical_hash
        grouped['seed']=first.get('seed',0) if len(members)==1 else int(canonical_hash({'group':key,'member_seeds':[row.get('seed') for row in members]})[:8],16)%2147483640
        grouped['start_time'],grouped['end_time']=_v32_time_bounds(grouped['context'])
        grouped['core_start_time'],grouped['core_end_time']=_v32_time_bounds(grouped['core'])
        if first.get('density_policy'):
            grouped['density_sections']=[{'start_time':_v32_time_bounds(row['core'])[0],
                'end_time':_v32_time_bounds(row['core'])[1],'density_min_heads':row.get('density_min_heads',0),
                'key':row['key'],'density_target_heads':row.get('density_target_heads',row.get('density_min_heads',0)),
                'acoustic_times':row.get('density_acoustic_times',[])} for row in members]
            grouped['density_min_heads']=sum(row.get('density_min_heads',0) for row in members)
            grouped['density_rounds']=[{'condition':round_spec['condition'],
                'seed':round_spec['seed'] if len(members)==1 else int(canonical_hash({'group':key,'round':i,
                    'member_seeds':[row['density_rounds'][i]['seed'] for row in members]})[:8],16)%2147483640}
                for i,round_spec in enumerate(first.get('density_rounds',[])[:2])]
        retry_rows=[row for row in members if row.get('retry_min_heads')]
        if retry_rows:
            grouped['retry_min_heads']=sum(row['retry_min_heads'] for row in retry_rows)
            grouped['retry_sections']=[{'start_time':_v32_time_bounds(row['core'])[0],'end_time':_v32_time_bounds(row['core'])[1],
                                        'retry_min_heads':row['retry_min_heads']}
                                       for row in retry_rows]
            grouped['retry_condition']=condition if 'bpm_bucket_id' in first else min(10.,condition+.35)
            from .section_plan import canonical_hash
            grouped['retry_seed']=int(canonical_hash({'group':key,'members':[row.get('retry_seed') for row in retry_rows]})[:8],16)%2147483640
        else:
            for name in ('retry_min_heads','retry_condition','retry_seed'):
                grouped.pop(name,None)
        merged.append(grouped)
        for row in members:owners[row['key']]=grouped
    return merged,owners


def _stable_events(raw,a,b,duration,recipe,*,preserve_raw_heads=False):
    from .section_plan import canonical_hash
    events,diagnostics=owned(raw,a,b,duration,preserve_raw_heads=preserve_raw_heads)
    for event in events:
        event['id']='note-'+canonical_hash({'recipe':recipe,'start':event['start_ms'],'lane':event['lane'],'end':event['end_ms']})[:32]
    return events,diagnostics


def rank_playable_model_candidate(notes, *, plan, core, difficulty, settings,
                                  pattern, source_end_ms, evidence, timing, hold_evidence=None):
    """Score a frozen model round after its actual playable constraints.

    The temporary selection view is discarded. The selected original round
    remains the immutable source; no calibrated heads replace raw evidence.
    """
    from .chart_quality import classify_holds
    from .adaptive_difficulty import model_candidates, calibrate_adaptive
    a,b = _v32_time_bounds(core)
    events,diagnostics = owned(notes,a,b,source_end_ms,preserve_raw_heads=True)
    for index,event in enumerate(events):
        event['id'] = f'rank-head-{index}'
    available = classify_holds(events,evidence if hold_evidence is None else hold_evidence)['events']
    candidates = model_candidates(as_notes(available,a,b),evidence,timing,origin_ms=a,
                                  selection_policy='model_only')
    selected,playability = calibrate_adaptive(candidates,b-a,difficulty,settings['ln_ratio'],
        settings['seed'],settings['difficulty_rules'][difficulty],pattern,plan,origin_ms=a,
        evidence=evidence,timing=timing,selection_policy='model_only',audio_vote_cap=0.)
    view = [{'start_ms':float(n.start+a),'lane':int(n.lane),
             'end_ms':float(n.end+a) if n.end is not None else None} for n in selected]
    report = evaluate_density(view,plan,difficulty,core)
    score = candidate_rank(view,report,settings['difficulty_rules'][difficulty])
    score = (score[0]+len(diagnostics['discarded_invalid']),*score[1:])
    return score,{'policy':'frozen-playable-model-round-v1','raw_model_heads':len(events),
                  'calibrated_heads':len(view),'candidate_acoustic_heads':playability.get('candidate_acoustic_heads',0),
                  'evidence_id':evidence.get('id'),
                  'density':report,'discarded_invalid':diagnostics['discarded_invalid'],
                  'retained_model_head_conflicts':diagnostics.get('retained_model_head_conflicts',[])}


def validate_frozen_policies(snapshot):
    """Reject corrupted or unsupported contracts before running model workers."""
    from .density_calibration import VERSION as density_version
    from .density_validation import contract as density_contract
    from .chart_quality import contract as quality_contract
    settings=snapshot.get('settings',{})
    policy=snapshot.get('density_policy',settings.get('density_policy'))
    if policy and 'policy_hash' in policy:
        content={key:value for key,value in policy.items() if key!='policy_hash'}
        digest=hashlib.sha256(json.dumps(content,sort_keys=True,separators=(',',':'),
                                        ensure_ascii=False,allow_nan=False).encode()).hexdigest()
        if policy.get('policy_hash')!=digest:
            raise ValueError('冻结密度策略哈希校验失败')
        if policy.get('version')!=density_version or policy.get('contract')!=density_contract():
            raise ValueError('冻结密度策略版本不受支持，请重新提交生成任务')
    quality=snapshot.get('quality_policy',settings.get('quality_policy'))
    if quality and 'hash' in quality and quality!=quality_contract(quality.get('version')):
        raise ValueError('冻结谱面质量策略版本或哈希校验失败，请重新提交生成任务')
    candidates=snapshot.get('candidate_policy',settings.get('candidate_policy'))
    if candidates:
        from .quality_workflow import candidate_contract
        if candidates!=candidate_contract(candidates.get('version')):raise ValueError('冻结候选策略版本或哈希校验失败，请重新提交生成任务')


def frozen_candidate_options(snapshot):
    policy=snapshot.get('candidate_policy',snapshot.get('settings',{}).get('candidate_policy')) or {}
    return {'selection_policy':policy.get('selection_policy','ranked_beam' if policy else 'model_only'),
            'audio_vote_cap':policy.get('audio_vote_cap')}


def _run_dynamic(source,directory,options,progress):
    from .workflow_log import stage, event, file_identity
    from .section_plan import build_plan,validate_plan,canonical_hash,pcm_audio
    from .adaptive_difficulty import attacks_adaptive,calibrate_adaptive,repair_playable,model_candidates
    snapshot=options['_advanced'];p=snapshot['project'];segment=snapshot['segment'];settings=snapshot['settings'];variants=snapshot['variants']
    from .generation_context import continuous
    continuous_model=continuous(snapshot)
    validate_frozen_policies(snapshot)
    candidate_options=frozen_candidate_options(snapshot)
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    with stage('advanced.dynamic.read_and_verify_pcm', source=file_identity(source,hash_file=True),
               source_role=snapshot.get('source',{}).get('source_role'),expected_source=snapshot.get('source')):
        data=pcm_audio(source);source_sha=hashlib.sha256(data.tobytes()).hexdigest()
    p.setdefault('samples',len(data))
    descriptor=snapshot.get('source') or {'source_id':'original','source_role':'mix','pcm_sha':source_sha,'parent_source_id':source_sha}
    if descriptor.get('pcm_sha') and descriptor['pcm_sha']!=source_sha:raise ValueError('模型输入声部 PCM 校验失败')
    parent_sha=descriptor.get('parent_source_id') or p.get('source_pcm_sha256') or source_sha
    with stage('advanced.dynamic.resolve_section_and_bpm_plan', frozen_plan_id=(snapshot.get('section_plan') or {}).get('id'),
               settings=settings,tempo=p.get('tempo'),source_sha=source_sha):
        plan=snapshot.get('section_plan') or build_plan(source,settings,p.get('tempo',{}))
    bucket_model=bool(plan.get('bpm_buckets',{}).get('enabled'))
    with stage('advanced.dynamic.validate_plan',plan_id=plan.get('id'),parent_source_sha=parent_sha,
               samples=plan.get('samples'),sections=len(plan.get('sections',[])),bpm_buckets=plan.get('bpm_buckets')):
        validate_plan(plan,parent_sha)
    if plan['samples']!=len(data):raise ValueError('段落计划与音源采样数不匹配')
    with stage('advanced.dynamic.persist_plan',plan_id=plan['id'],output=directory/'section-plan.json'):
        atomic(directory/'section-plan.json',plan)
    event('plan_result','advanced.dynamic.resolve_section_and_bpm_plan',plan_id=plan['id'],
          bpm_buckets=plan.get('bpm_buckets'),section_count=len(plan.get('sections',[])))
    use_v32=settings['engine']=='v32'
    start,end=segment['start_sample']*1000/SR,segment['end_sample']*1000/SR
    if use_v32:start,end=_v32_time_bounds((segment['start_sample'],segment['end_sample']))
    from .audio_bounds import content_end
    source_stop=min(len(data),content_end(p))
    cores=[]
    for section in plan['sections']:
        a,b=max(section['core'][0],segment['start_sample']),min(section['core'][1],segment['end_sample'])
        b=min(b,source_stop)
        if a<b:cores.append((section,a,b,max(0,a-4*SR),min(source_stop,b+4*SR)))
    if not cores:raise ValueError('段落计划未覆盖选定片段')
    from .audio_bounds import exact_silence
    original=snapshot.get('original_source',{}).get('path') or (source if descriptor.get('source_role','mix')=='mix' else None)
    silent_cores=[row for row in cores if original and exact_silence(original,row[1],row[2])]
    if not continuous_model:cores=[row for row in cores if row not in silent_cores]
    fast=settings['strategy']=='fast';raw_only=snapshot.get('_stem_raw_only',False)
    sustain_evidence={}
    if not raw_only and original:
        from .quality_workflow import evidence_for
        role=descriptor.get('source_role','mix')
        descriptors=snapshot.get('input_sources') or ([{'source_role':role,'path':source}] if role in ('vocals','accompaniment') else [])
        sustain_evidence=evidence_for(Path(original).parent,original,descriptors)
    acoustic_cache=snapshot.get('evidence',{}).get('acoustic') or sustain_evidence
    if candidate_options.get('selection_policy')=='model_skeleton_then_audio' and sustain_evidence:
        acoustic_cache=sustain_evidence
    with stage('advanced.dynamic.prepare_model_audio',source_stop=source_stop,source_rate=SR,target_rate=22050,
               input_samples=source_stop,engine=settings['engine']):
        full=librosa.resample(mono_audio(data[:source_stop])[0],orig_sr=SR,target_sr=22050)
        if use_v32:sf.write(directory/'v32-input.wav',full,22050,subtype='FLOAT')
    engine=None;output=[];errors=[];timings={};models={}
    if not use_v32:
        from .engine import Engine
        engine=Engine()
    try:
        patterns=list(dict.fromkeys(v['pattern'] for v in variants))
        for pattern in patterns:
            keys=[v['difficulty'] for v in variants if v['pattern']==pattern]
            mother_key=highest_requested(keys,settings.get('difficulty_rules'));infer_keys=[mother_key] if fast else keys
            local={**settings,'title':p['title'],'artist':p['artist'],'pattern':pattern,'patterns':[pattern],'difficulties':keys}
            local['v32_descriptors']=list(dict.fromkeys([*settings.get('v32_descriptors',[]),*([V32_PATTERN_TAGS[pattern]] if pattern in V32_PATTERN_TAGS else [])]))
            if len(local['v32_descriptors'])>4:raise ValueError('排键倾向与 V32 标签合计不能超过四个')
            if local.get('v32_negative_descriptors') and (settings['v32_cfg_scale']<=1 or len(local['v32_negative_descriptors'])!=len(local['v32_descriptors'])):raise ValueError('排除标签数量须匹配正向标签，且 CFG 大于 1')
            raw={key:[] for key in infer_keys};records=[];metadata={'silent_cores':[{'core':[r[1],r[2]],'silence_verified':True,'inference_count':0} for r in silent_cores]};requests=[];failed_keys=set()
            for section,a,b,ca,cb in cores:
                for key in infer_keys:
                    detail=section['per_difficulty'][key]
                    condition=detail['model_condition']
                    if continuous_model and not bucket_model:
                        condition=settings['conditions'][settings['engine']][mother_key if fast else key]
                    elif fast:
                        base=settings['conditions'][settings['engine']][mother_key]
                        condition=float(np.clip(base+detail['model_condition']-detail['base_model_condition'],1 if use_v32 else 1.5,10 if use_v32 else 8))
                    policy=snapshot.get('density_policy',settings.get('density_policy'))
                    requested_condition=condition
                    if policy and not continuous_model and not bucket_model and not snapshot.get('density_condition_override'):
                        condition=initial_condition(policy,settings['engine'],key,condition,detail.get('target_rate'),
                                                    descriptor.get('source_role','mix'),pattern)
                    seed=_dynamic_seed(settings['seed'],settings['engine'],descriptor,pattern,key,[a,b])
                    start_ms,end_ms=_v32_time_bounds((a,b))
                    request={'key':key+'__'+section['id'],'label':key+' '+section['id'],'sr':condition,'seed':seed,
                             'start_time':start_ms,'end_time':end_ms,'difficulty_key':key,
                             'core':[a,b],'context':[ca,cb],'section_id':section['id'],
                             'requested_condition':requested_condition,'executed_condition':condition,
                             'condition_rationale':'bpm_bucket_actual_condition' if bucket_model else 'continuous_requested_difficulty_base' if continuous_model else 'qualified_calibration_opt_in' if condition!=requested_condition else 'frozen_requested_condition',
                             'regional_plan_condition':detail['model_condition']}
                    if bucket_model:request['bpm_bucket_id']=section.get('bpm_bucket_id')
                    # One bounded retry in the same V32 worker, only for a
                    # credible evidence-supported deficit below feasible caps.
                    frozen_report=evaluate_density([],plan,key,[a,b])
                    expected=frozen_report['target_heads']
                    onset_evidence=sum(max(0.,float(point.get('onset_rate',0))) *
                                       max(0,min(b,point['end_sample'])-max(a,point['start_sample']))/SR
                                       for point in section['profile'])
                    has_audible_rhythm=(section['active_seconds']>2 and
                                        onset_evidence>=max(3.,section['active_seconds']*.5))
                    if not snapshot.get('density_policy',settings.get('density_policy')) and not snapshot.get('disable_density_retry') and not fast and has_audible_rhythm and expected>=8 and expected<detail['hard_caps']['peak_1s']*(b-a)/SR:
                        request.update(retry_min_heads=round(expected*.75),retry_condition=condition if bucket_model else min(10,condition+.35),
                                       retry_seed=_dynamic_seed(settings['seed'],settings['engine'],descriptor,pattern,key,[a,b],1))
                    policy=snapshot.get('density_policy',settings.get('density_policy'))
                    if policy:
                        request['density_policy']=policy
                        request['disable_density_retry']=bool(snapshot.get('disable_density_retry') or
                            (raw_only and not snapshot.get('_density_raw_supply',False)))
                        acoustic=acoustic_cache
                        # Estimate model work against the remaining quota from frozen
                        # measured singletons; this counts supply, not a second budget.
                        from .adaptive_difficulty import evidence_candidates
                        acoustic_times=[] if candidate_options['selection_policy']=='model_only' else [
                            candidate[0] for candidate in evidence_candidates([],acoustic)
                            if start_ms<=candidate[0]<end_ms]
                        request['density_target_heads']=int(np.ceil(expected*.85-1e-6))
                        request['density_acoustic_times']=acoustic_times
                        request['density_min_heads']=max(0,request['density_target_heads']-len(acoustic_times))
                        used=[condition]; rounds=[]
                        if not request['disable_density_retry']:
                            for retry_index in range(2):
                                target_rate=settings['difficulty_rules'][key]['rate'] if continuous_model and not bucket_model else detail.get('target_rate')
                                next_condition=(condition if bucket_model else condition_next(policy,settings['engine'],key,target_rate,retry_index,used,
                                                              descriptor.get('source_role','mix'),pattern))
                                if next_condition is None:break
                                rounds.append({'condition':next_condition,'seed':_dynamic_seed(settings['seed'],settings['engine'],descriptor,pattern,key,[a,b],retry_index+1)})
                                used.append(next_condition)
                        request['density_rounds']=rounds
                    request.update(active_seconds=round(frozen_report['active_seconds'],5),
                                   onset_evidence=round(onset_evidence,2),has_audible_rhythm=bool(has_audible_rhythm))
                    requests.append(request)
            max_group_samples=None if continuous_model or bucket_model else {'fine':16,'balanced':32,'coarse':48}.get(settings.get('section_granularity','balanced'),32)*SR
            inference_requests,request_groups=(_coalesce_v32_requests(requests,tolerance=0. if plan.get('arrangement_enabled') else V32_CONDITION_GROUP_TOLERANCE,max_span_samples=max_group_samples)
                                               if use_v32 else (requests,{r['key']:r for r in requests}))
            try:
                if use_v32 and requests:
                    from .mapperatorinator import generate
                    folder=directory/pattern;folder.mkdir(exist_ok=True)
                    local.update(start_time=start,end_time=end,_advanced_presets=inference_requests)
                    attach_timing_reference(local,directory,p,descriptor)
                    results,model_metadata=generate(directory/'v32-input.wav',folder,local,progress)
                    metadata.update(model_metadata)
                    if local.get('timing_reference'):metadata['timing_reference']=timing_reference_info(p)
                    metadata['condition_coalescing']={'original_requests':len(requests),'model_passes':len(inference_requests),
                                                       'condition_tolerance':0. if plan.get('arrangement_enabled') else V32_CONDITION_GROUP_TOLERANCE}
                    if bucket_model:
                        metadata['bpm_buckets']=plan['bpm_buckets']
                        metadata['model_condition_scope']='bucket_actual_request'
                    metadata['decoder_context']={'scope':'per_inference_request',
                        'cross_request_continuation':False,
                        'audio_halo_samples':4*SR,
                        'ranges':[{'key':row['key'],'core':row['core'],'audio_context':row['context']}
                                  for row in inference_requests]}
                    if continuous_model:metadata['generation_context_policy']=snapshot['generation_context_policy']
                    for request in requests:
                        group=request_groups[request['key']]
                        attempt=results.get(group['key'])
                        valid_retry=results.get(group['key']+'__retry')
                        if request.get('density_policy') and valid_retry is None:
                            valid_retry=next((results.get(group['key']+'__density_retry'+str(i)) for i in (1,2)
                                              if results.get(group['key']+'__density_retry'+str(i))),None)
                        recovered_primary=attempt is None and valid_retry is not None
                        if attempt is None:
                            if valid_retry is None:
                                failed_keys.add(request['difficulty_key'])
                                errors.append({'pattern':pattern,'difficulty':request['difficulty_key'],
                                    'error':'核心输出与重试均无有效谱面：'+request['key'],
                                    'rejected_charts':metadata.get('rejected_charts',{})})
                                continue
                            attempt=valid_retry
                        notes,timing=attempt[0],attempt[1]
                        owned_notes=_v32_owned_notes(notes,request['core'])
                        first_count=None if recovered_primary else len(owned_notes);retry_count=None
                        chosen_round=0
                        selection_views={};selection_scores={}
                        retry=results.get(group['key']+'__retry')
                        all_attempts=[] if recovered_primary else [{'retry':0,'condition':group['sr'],'seed':group['seed'],'notes':[[n.start,n.lane,n.end] for n in owned_notes]}]
                        if retry:
                            retry_notes=_v32_owned_notes(retry[0],request['core'])
                            retry_count=len(retry_notes)
                            all_attempts.append({'retry':1,'condition':group['retry_condition'],'seed':group['retry_seed'],'notes':[[n.start,n.lane,n.end] for n in retry_notes]})
                            if recovered_primary or len(retry_notes)>len(owned_notes):
                                owned_notes,timing=retry_notes,retry[1];chosen_round=1
                        if request.get('density_policy'):
                            pool=[] if recovered_primary else [(owned_notes,timing,0)]
                            for retry_index in (1,2):
                                candidate=results.get(group['key']+'__density_retry'+str(retry_index))
                                if candidate:pool.append((_v32_owned_notes(candidate[0],request['core']),candidate[1],retry_index))
                            def rank(item):
                                if (continuous_model and not fast and not raw_only
                                        and descriptor.get('source_role','mix')=='mix'
                                        and candidate_options['selection_policy']=='model_only'):
                                    value,view=rank_playable_model_candidate(item[0],plan=plan,
                                        core=request['core'],difficulty=request['difficulty_key'],
                                        settings=settings,pattern=pattern,source_end_ms=len(data)*1000/SR,
                                        evidence=acoustic_cache,hold_evidence=sustain_evidence,
                                        timing=snapshot.get('timing_map'))
                                    selection_views[item[2]]=view;selection_scores[item[2]]=list(value)
                                    metadata['density_selection_policy']=view['policy']
                                    return value
                                ev,diagnostics=owned(item[0],*_v32_time_bounds(request['core']),len(data)*1000/SR)
                                report=evaluate_density(ev,plan,request['difficulty_key'],request['core'])
                                value=candidate_rank(ev,report,settings['difficulty_rules'][request['difficulty_key']])
                                return (value[0]+len(diagnostics['discarded_invalid']),*value[1:])
                            owned_notes,timing,chosen_round=min(pool,key=rank)
                            all_attempts=[{'retry':idx,
                                'condition':group['density_rounds'][idx-1]['condition'] if idx else group['sr'],
                                'seed':group['density_rounds'][idx-1]['seed'] if idx else group['seed'],
                                'notes':[[n.start,n.lane,n.end] for n in ns],
                                **({'selection_view':selection_views[idx],'selection_score':selection_scores[idx]}
                                   if idx in selection_views else {})} for ns,tm,idx in pool]
                        raw[request['difficulty_key']].extend(owned_notes)
                        from .advanced_execution import spent_retry_rounds
                        spent=spent_retry_rounds({'key':request['key'],'inference_group':group['key'],
                                                 'attempts':all_attempts,'retry_heads':retry_count},metadata)
                        selected_spec=(group.get('density_rounds',[])[chosen_round-1]
                            if request.get('density_policy') and chosen_round else
                            {'condition':group['retry_condition'],'seed':group['retry_seed']}
                            if chosen_round else {'condition':group['sr'],'seed':group['seed']})
                        records.append({**request,'inference_group':group['key'],'inference_condition':group['sr'],
                                        'inference_seed':group['seed'],
                                        'selected_round':chosen_round,'selected_condition':selected_spec['condition'],
                                        'selected_seed':selected_spec['seed'],
                                        **({'density_selection_policy':selection_views[chosen_round]['policy'],
                                            'selected_playable_heads':selection_views[chosen_round]['calibrated_heads']}
                                           if chosen_round in selection_views else {}),
                                        'spent_retry_rounds':spent,
                                        'first_heads':first_count,'retry_heads':retry_count,'chosen_heads':len(owned_notes),
                                        'recovered_from_rejected_primary':recovered_primary,
                                        'rejected_primary':metadata.get('rejected_charts',{}).get(group['key']),
                                        'underfilled_active_audio':bool(request.get('has_audible_rhythm') and
                                            request.get('retry_min_heads') and len(owned_notes)<request['retry_min_heads']),
                                        'attempts':all_attempts,'timing':timing})
                        if request['difficulty_key']==infer_keys[0]:
                            a_ms,b_ms=_v32_time_bounds(request['core'])
                            active=next((bpm for t,bpm in reversed(timing) if t<=a_ms),timing[0][1])
                            timings.setdefault(pattern,[]).append([a_ms,active])
                            timings[pattern].extend([t,bpm] for t,bpm in timing if a_ms<t<b_ms)
                else:
                    for section,a,b,ca,cb in cores:
                        y=librosa.resample(mono_audio(data[ca:cb])[0],orig_sr=SR,target_sr=22050)
                        wave=engine.prepare(y,22050,progress)
                        for request in [r for r in requests if r['section_id']==section['id']]:
                            cfg={**local,'seed':request['seed'],'mug_difficulty':request['sr']}
                            generated=engine.generate(wave,None,cfg,lambda fraction:progress(pattern+' · '+request['label'],20+fraction*65))
                            notes=[Note(n.start+ca*1000/SR,n.lane,n.end+ca*1000/SR if n.end is not None else None) for n in generated]
                            selected_round=0;selected_condition=request['sr'];selected_seed=request['seed']
                            attempts=[{'retry':0,'condition':request['sr'],'seed':request['seed'],'notes':[[n.start,n.lane,n.end] for n in notes]}]
                            if request.get('retry_min_heads') and sum(a*1000/SR<=n.start<b*1000/SR for n in notes)<request['retry_min_heads']:
                                cfg.update(seed=request['retry_seed'],mug_difficulty=request['sr'] if bucket_model else min(8,request['sr']+.35))
                                generated=engine.generate(wave,None,cfg,lambda fraction:progress(pattern+' · 局部容量重试',20+fraction*65))
                                retry=[Note(n.start+ca*1000/SR,n.lane,n.end+ca*1000/SR if n.end is not None else None) for n in generated]
                                attempts.append({'retry':1,'condition':cfg['mug_difficulty'],'seed':cfg['seed'],'notes':[[n.start,n.lane,n.end] for n in retry]})
                                if sum(a*1000/SR<=n.start<b*1000/SR for n in retry)>sum(a*1000/SR<=n.start<b*1000/SR for n in notes):
                                    notes=retry;selected_round=1;selected_condition=cfg['mug_difficulty'];selected_seed=cfg['seed']
                            if request.get('density_policy'):
                                def score(candidate):
                                    ev,diagnostics=owned(candidate,a*1000/SR,b*1000/SR,len(data)*1000/SR)
                                    available=model_candidates([Note(e['start_ms'],e['lane'],e.get('end_ms')) for e in ev],
                                        acoustic_cache,selection_policy=candidate_options['selection_policy'])
                                    supply=sum(max(1,len(c[2])) for c in available if a*1000/SR<=c[0]<b*1000/SR)
                                    report=evaluate_density(ev,plan,request['difficulty_key'],[a,b],candidates={
                                        'model_heads':len(ev),'candidate_heads':supply,
                                        'model_supply_failure':supply<request.get('density_target_heads',0)})
                                    value=candidate_rank(ev,report,settings['difficulty_rules'][request['difficulty_key']])
                                    return (value[0]+len(diagnostics['discarded_invalid']),*value[1:]),report
                                best_score,report=score(notes)
                                for retry_index,round_spec in enumerate(request.get('density_rounds',[])[:2],1):
                                    if not report['retry_allowed']:break
                                    cfg.update(seed=round_spec['seed'],mug_difficulty=round_spec['condition'])
                                    generated=engine.generate(wave,None,cfg,lambda fraction:progress(pattern+' · 密度校准',20+fraction*65))
                                    candidate=[Note(n.start+ca*1000/SR,n.lane,n.end+ca*1000/SR if n.end is not None else None) for n in generated]
                                    attempts.append({'retry':retry_index,'condition':round_spec['condition'],'seed':round_spec['seed'],'notes':[[n.start,n.lane,n.end] for n in candidate]})
                                    candidate_score,candidate_report=score(candidate)
                                    if candidate_score<best_score:
                                        notes,best_score,report=candidate,candidate_score,candidate_report
                                        selected_round=retry_index;selected_condition=round_spec['condition'];selected_seed=round_spec['seed']
                            raw[request['difficulty_key']].extend(n for n in notes if a*1000/SR<=n.start<b*1000/SR)
                            records.append({**request,'attempts':attempts,'selected_round':selected_round,
                                            'selected_condition':selected_condition,'selected_seed':selected_seed})
                raw={key:sorted(notes,key=lambda n:(n.start,n.lane)) for key,notes in raw.items()}
                recipe=canonical_hash({'plan':plan['content_hash'],'source':descriptor,'pattern':pattern,'requests':requests,
                                       'inference_requests':inference_requests,'engine':settings['engine'],
                                       **({'density_selection_policy':metadata['density_selection_policy']}
                                          if metadata.get('density_selection_policy') else {})})
                cache={'format':2,'range':[segment['start_sample'],segment['end_sample']],'context_range':[0,len(data)],
                       'model':settings['engine'],'model_version':'Mapperatorinator V32' if use_v32 else 'MuG v1.0.0',
                       'settings':settings,'pattern':pattern,'mother_key':mother_key if fast else None,'section_plan':plan,'metadata':metadata,'records':records,
                       'inference_groups':inference_requests if use_v32 else [],
                       'source':{k:v for k,v in descriptor.items() if k!='path'},'raw_recipe_hash':recipe,
                       'raw':{key:[[n.start,n.lane,n.end] for n in notes] for key,notes in raw.items()}}
                if use_v32:
                    from .v32_rhythm import note_evidence
                    cache['model_rhythm']={key:note_evidence(notes) for key,notes in raw.items()}
                # Classify a copy before the budget/lane planner sees occupancy.
                # Never spend an LN ratio on the unselected immutable model supply.
                supplies={};hold_classification={}
                if not raw_only:
                    from .chart_quality import classify_holds
                    for supply_key,notes in raw.items():
                        supplied=[{'id':recipe+':'+supply_key+':'+str(i),'start_ms':n.start,
                                   'lane':n.lane,'end_ms':n.end,'source_role':descriptor.get('source_role','mix')}
                                  for i,n in enumerate(notes)]
                        classified=classify_holds(supplied,sustain_evidence)
                        supplies[supply_key]=[Note(e['start_ms'],e['lane'],e.get('end_ms')) for e in classified['events']]
                        hold_classification[supply_key]={'version':classified['version'],
                            'evidence_id':sustain_evidence.get('id'),'decisions':classified['decisions']}
                model_only=bool(candidate_options['selection_policy']=='model_only' or plan.get('arrangement_enabled') or snapshot.get('density_policy',settings.get('density_policy')))
                if fast and not raw_only:
                    with stage('advanced.dynamic.build_candidate_pool',pattern=pattern,mother_key=mother_key,
                               supplied_model_notes=len(supplies[mother_key]),model_only=model_only,
                               selection_policy=candidate_options['selection_policy']):
                        candidates=(model_candidates(supplies[mother_key],acoustic_cache,snapshot.get('timing_map'),selection_policy=candidate_options['selection_policy'])
                                    if model_only else attacks_adaptive(full,22050,supplies[mother_key]))
                else:candidates=None
                event('candidate_pool_result','advanced.dynamic.build_candidate_pool',pattern=pattern,
                      candidate_count=len(candidates) if candidates is not None else None,
                      candidate_provenance_counts={kind:sum(1 for row in candidates if getattr(row,'provenance',{}).get('kind')==kind)
                                                   for kind in ('model','audio','mixed')} if candidates is not None else {})
                if candidates is not None:cache['candidates']=[[t,score,[[n.start,n.lane,n.end] for n in notes]] for t,score,notes in candidates]
                cache_file=directory/(pattern+'-mother.json');atomic(cache_file,cache)
                models[pattern]=cache['model_version']
                for key in keys:
                    if (mother_key if fast else key) in failed_keys:continue
                    originals=raw[mother_key] if fast else raw[key]
                    events,diagnostics=_stable_events(originals,start,end,len(data)*1000/SR,recipe+key,preserve_raw_heads=True)
                    provenance={'timing_reference':metadata.get('timing_reference'),'timing_map':snapshot.get('timing_map'),'seed':settings['seed'],'model_version':cache['model_version'],'cache_job':directory.name,
                                'cache_file':cache_file.name,'section_plan_id':plan['id'],'applied_plan_hash':plan['content_hash'],
                                'raw_recipe_hash':recipe,'source_id':descriptor['source_id'],'source_role':descriptor.get('source_role','mix'),
                                'parent_source_id':parent_sha,'stem_set_id':descriptor.get('stem_set_id'),'context_range':[0,len(data)],
                                'core_conditions':[r for r in records if r['difficulty_key']==(mother_key if fast else key)],
                                'decoder_context':metadata.get('decoder_context'),
                                'candidate_policy':snapshot.get('candidate_policy',settings.get('candidate_policy')),
                                'generation_context_policy':snapshot.get('generation_context_policy'),
                                'active_audio_gaps':([{'start_ms':round(r['core'][0]*1000/SR),'end_ms':round(r['core'][1]*1000/SR),
                                    'difficulty':key,'pattern':pattern,'notes':r['chosen_heads'],'expected_minimum':r['retry_min_heads'],
                                    'onset_evidence':r['onset_evidence'],'active_seconds':r['active_seconds']}
                                    for r in records if r['difficulty_key']==(mother_key if fast else key) and r.get('underfilled_active_audio')]
                                    if descriptor.get('source_role','mix') in ('mix','accompaniment') else []),**diagnostics}
                    # Preserve each difficulty's actual model timing. An
                    # uncertain detection map must not prevent playing/exporting
                    # absolute notes, or silently become an inference reference.
                    points=[]
                    for record in records:
                        if record['difficulty_key']!=(mother_key if fast else key) or not record.get('timing'):continue
                        a_ms,b_ms=(value*1000/SR for value in record['core'])
                        model_points=record['timing']
                        active=next((bpm for t,bpm in reversed(model_points) if t<=a_ms),model_points[0][1])
                        points.append([a_ms,active]);points.extend([t,bpm] for t,bpm in model_points if a_ms<t<b_ms)
                    provenance['serialization_tempo']=({'points':sorted({float(t):float(bpm) for t,bpm in points}.items()),'source':'model_output'}
                        if points else {**p['tempo'],'source':'frozen_coordinate_clock','reference_available':False})
                    if bucket_model:
                        provenance['model_serialization_tempo']=provenance['serialization_tempo']
                        provenance['serialization_tempo']={'bpm':120.,'points':[[0.,120.]],'source':'fixed-scroll-120-v1','reference_available':False}
                        provenance['bpm_buckets']=plan['bpm_buckets']
                    output.append({'variant':pattern+'--'+key,'events':events,'kind':'stem_raw' if raw_only and descriptor.get('source_role') in ('vocals','accompaniment') else 'model_raw',
                                   'settings':settings,'provenance':{**provenance,'raw_head_policy':RAW_HEAD_POLICY,'global_budget_applied':False,'budget_stage':0},'activate_initial':False})
                    if raw_only:continue
                    rule=settings['difficulty_rules'][key]
                    if fast:
                        # Clip once before spending this segment's budget. Context
                        # attacks remain evidence, never consume other core quotas.
                        from .adaptive_difficulty import Candidate
                        subset=[Candidate(t-start,score,[Note(n.start-start,n.lane,n.end-start if n.end is not None else None) for n in notes],
                            getattr(candidate,'provenance',{}),getattr(candidate,'support',{}))
                            for candidate in candidates for t,score,notes in [candidate] if start<=t<end]
                        with stage('advanced.dynamic.select_difficulty',pattern=pattern,difficulty=key,
                                   candidates=len(subset),range_ms=[start,end],rule=rule,selection_policy=candidate_options):
                            selected,report=calibrate_adaptive(subset,end-start,key,settings['ln_ratio'],_dynamic_seed(settings['seed'],settings['engine'],descriptor,pattern,key,[segment['start_sample'],segment['end_sample']]),rule,pattern,plan,origin_ms=start,evidence=acoustic_cache,timing=snapshot.get('timing_map'),**candidate_options)
                        playable=[Note(n.start+start,n.lane,n.end+start if n.end is not None else None) for n in selected]
                    else:
                        local_notes=[Note(n.start-start,n.lane,n.end-start if n.end is not None else None) for n in supplies[key] if start<=n.start<end]
                        evidence=acoustic_cache
                        with stage('advanced.dynamic.build_candidate_pool',pattern=pattern,difficulty=key,
                                   source_notes=len(local_notes),selection_policy=candidate_options['selection_policy']):
                            subset=model_candidates(local_notes,evidence,snapshot.get('timing_map'),origin_ms=start,selection_policy=candidate_options['selection_policy'])
                        with stage('advanced.dynamic.select_difficulty',pattern=pattern,difficulty=key,
                                   candidates=len(subset),range_ms=[start,end],rule=rule,selection_policy=candidate_options):
                            selected,report=calibrate_adaptive(subset,end-start,key,settings['ln_ratio'],settings['seed'],rule,pattern,plan,origin_ms=start,evidence=acoustic_cache,timing=snapshot.get('timing_map'),**candidate_options)
                        playable=[Note(n.start+start,n.lane,n.end+start if n.end is not None else None) for n in selected]
                    candidate_events,candidate_diag=_stable_events(playable,start,end,len(data)*1000/SR,recipe+key+'playable')
                    from .quality_workflow import candidate_events as materialize_candidates
                    materialized=materialize_candidates(selected,subset,events,origin_ms=start,
                        source_role=descriptor.get('source_role','mix'),source_id=descriptor.get('source_id'))
                    valid={(e['start_ms'],e['lane']) for e in candidate_events}
                    candidate_events=[e for e in materialized if (e['start_ms'],e['lane']) in valid]
                    output.append({'variant':pattern+'--'+key,'events':candidate_events,'kind':'arranged' if plan.get('arrangement_enabled') else 'fast' if fast else 'rules',
                                   'settings':settings,'provenance':{**provenance,**candidate_diag,'playability':report,
                                   'raw_preserved':True,'hold_classification':hold_classification[mother_key if fast else key],
                                   'global_budget_applied':True,'budget_stage':1},'activate_initial':True})
                    event('difficulty_result','advanced.dynamic.select_difficulty',pattern=pattern,difficulty=key,
                          raw_model_heads=len(events),selected_heads=len(candidate_events),playability=report,
                          density=output[-1]['provenance'].get('density_validation'))
            except Exception as exc:
                errors.append({'pattern':pattern,'error':str(exc)})
                event('pattern_failure','advanced.dynamic.generate_pattern',pattern=pattern,error_type=type(exc).__name__,
                      error=str(exc),traceback=traceback.format_exc())
    finally:
        if engine:engine.unload()
    if not output:raise ValueError('所有片段组合生成失败：'+json.dumps(errors,ensure_ascii=False))
    event('advanced_generation_result','advanced.dynamic.completed',errors=errors,variants=[row['variant'] for row in output],
          event_counts={row['variant']:len(row.get('events',[])) for row in output},plan_id=plan.get('id'),
          bucket_policy=plan.get('bpm_buckets'))
    progress('分段原谱和可玩候选已生成',98)
    timings={pattern:sorted({float(t):float(bpm) for t,bpm in points}.items()) for pattern,points in timings.items()}
    return {'advanced_result':output,'errors':errors,'bounds':[segment['start_sample'],segment['end_sample']],
            'section_plan_id':plan['id'],'timings':timings,'model_versions':models,
            **({'generation_context_policy':snapshot['generation_context_policy'],
                'member_segment_ids':[part['id'] for part in snapshot.get('member_segments',[segment])]}
               if continuous_model else {})}


def generate_sectioned(source_wav,directory,settings,variants,plan,progress,raw_only=False,_density_raw_supply=True):
    """Ordinary pipeline hook: same core inference/raw/candidate policy as advanced."""
    options={'_advanced':{'project':{'title':settings['title'],'artist':settings['artist'],'tempo':plan.get('tempo_reference',{}),
                                   'source_pcm_sha256':plan['source_pcm_sha'],'samples':plan['samples']},
                         'segment':{'start_sample':0,'end_sample':plan['samples']},'settings':settings,
                         'variants':variants,'section_plan':plan,'_stem_raw_only':raw_only,
                         '_density_raw_supply':bool(_density_raw_supply)}}
    return _run_dynamic(source_wav,directory,options,progress)

def rule_candidate(store,pid,rid,settings):
    r=store.revision(pid,rid);p=store.load(pid);s=store.segment(p,r['segment_id']);pattern,key=r['variant'].split('--');a,b=(v*1000/SR for v in r['range'])
    # Rule-only candidates retain their actual engine and sampling provenance.
    settings={**r['settings'],'difficulty_rules':settings['difficulty_rules'],'ln_ratio':settings['ln_ratio'],'seed':settings['seed']}
    from .quality_workflow import candidate_contract
    prov={**r.get('provenance',{}),'parent_candidate_policy':r.get('provenance',{}).get('candidate_policy'),
          'candidate_policy':candidate_contract()};cache=None
    settings['candidate_policy']=prov['candidate_policy']
    if settings['strategy']=='fast' and not prov.get('global_budget_applied') and prov.get('cache_job') and prov.get('cache_file'):
        from .paths import ROOT
        from .advanced import identifier,read
        name=prov['cache_file']
        if Path(name).name!=name:raise ValueError('缓存文件名无效')
        path=ROOT/'outputs'/identifier(prov['cache_job'])/name
        if path.is_file():cache=read(path)
    if settings.get('dynamic_enabled',False) or prov.get('global_budget_applied') or prov.get('budget_stage',0)>=1:
        from .adaptive_difficulty import calibrate_adaptive,repair_playable
        from .section_plan import build_plan
        if not prov.get('global_budget_applied') and settings['strategy']=='fast' and cache and cache.get('format')==2 and cache.get('candidates'):
            from .advanced_plans import get_or_build_plan
            from .bpm_buckets import DEFAULT_FROZEN_VERSION
            # A replay keeps the estimator the generated result was made with.
            plan=get_or_build_plan(store,p,settings,(prov.get('bpm_buckets') or {}).get('version',DEFAULT_FROZEN_VERSION))
            candidates=[(t-a,strength,[Note(x[0]-a,x[1],x[2]-a if x[2] is not None else None) for x in originals])
                        for t,strength,originals in cache['candidates'] if a<=t<b]
            frozen={'settings':settings,'candidate_policy':prov.get('candidate_policy',settings.get('candidate_policy'))}
            validate_frozen_policies(frozen)
            notes,report=calibrate_adaptive(candidates,b-a,key,settings['ln_ratio'],settings['seed'],settings['difficulty_rules'][key],pattern,plan,origin_ms=a,
                evidence=prov.get('evidence',{}).get('acoustic'),timing=prov.get('timing_map'),**frozen_candidate_options(frozen))
        else:
            notes,report=repair_playable(as_notes(r['events'],a,b),b-a,settings['difficulty_rules'][key],pattern)
        raw=[Note(n.start+a,n.lane,n.end+a if n.end is not None else None) for n in notes]
        if settings.get('engine')=='v32':
            from .v32_rhythm import transfer_notes
            from copy import deepcopy
            previous_notes=[Note(e['start_ms'],e['lane'],e.get('end_ms')) for e in r['events']]
            for note,event in zip(previous_notes,r['events']):
                if 'model_rhythm' in event:object.__setattr__(note,'model_rhythm',deepcopy(event['model_rhythm']))
            transfer_notes(raw,previous_notes,unavailable_on_missing=False)
        ev,diagnostics=owned(raw,a,b,p['duration']*1000)
        previous={(event['start_ms'],event['lane'],event.get('end_ms')):event for event in r['events']}
        from .section_plan import canonical_hash
        for event in ev:
            old=previous.get((event['start_ms'],event['lane'],event.get('end_ms')))
            if old:
                event['id']=old['id']
                for field in ('origins','audio_evidence','model_rhythm'):
                    if field in old:event[field]=old[field]
            else:
                event['id']='note-'+canonical_hash({'parent':rid,'event':{k:v for k,v in event.items() if k!='id'},'rules':settings['difficulty_rules'][key]})[:32]
                origins=[old for old in r['events'] if old['start_ms']==event['start_ms']]
                if len(origins)==1 and 'origins' in origins[0]:event['origins']=origins[0]['origins']
        return store.add_revision(pid,s['id'],r['variant'],ev,settings,'rules',{**prov,'parent':rid,'playability':report,**diagnostics},bounds=r['range'],activate_initial=False)
    # Fast stratification reuses the actual master and detected attacks.
    if settings['strategy']=='fast' and cache and cache.get('candidates'):
        context=cache['context_range'];origin=context[0]*1000/SR
        candidates=[(t,strength,[Note(*[x[0],x[1],x[2]]) for x in originals]) for t,strength,originals in cache['candidates']]
        frozen={'settings':settings,'candidate_policy':prov.get('candidate_policy',settings.get('candidate_policy'))}
        if frozen_candidate_options(frozen)['selection_policy']=='model_only':
            from .adaptive_difficulty import calibrate_adaptive
            notes,_=calibrate_adaptive(candidates,(context[1]-context[0])*1000/SR,key,settings['ln_ratio'],settings['seed'],settings['difficulty_rules'][key],pattern,min_notes=0,**frozen_candidate_options(frozen))
        else:
            notes,_=calibrate(candidates,(context[1]-context[0])*1000/SR,key,settings['ln_ratio'],settings['seed'],settings['difficulty_rules'][key],pattern,min_notes=0)
        raw=[Note(n.start+origin,n.lane,n.end+origin if n.end is not None else None) for n in notes]
    else:
        from .charts import clean_notes
        cap=settings['difficulty_rules'][key]['hold_ms']
        originals=[Note(n.start,n.lane,min(n.end,n.start+cap) if n.end is not None else None) for n in as_notes(r['events'],a,b)]
        raw=clean_notes(originals,b-a,{**settings['difficulty_rules'][key],'min_notes':0})
        raw=[Note(n.start+a,n.lane,n.end+a if n.end is not None else None) for n in raw]
    ev,diagnostics=owned(raw,a,b,p['duration']*1000)
    if settings.get('engine')=='v32':
        from .v32_rhythm import attach_notes, hydrate_cache
        if cache and cache.get('raw'):
            master_key=cache.get('mother_key') or highest_requested(list(cache.get('raw',{})),settings.get('difficulty_rules'))
            masters=[Note(*row) for row in cache.get('raw',{}).get(master_key,[])]
            hydrate_cache(cache,{master_key:masters},path.parent)
            from .v32_rhythm import transfer_notes
            transfer_notes(raw,masters,prov.get('source_role'))
        else:
            attach_notes(raw,[e for e in r['events'] if 'model_rhythm' in e],source_role=prov.get('source_role'))
        ev,diagnostics=owned(raw,a,b,p['duration']*1000)
    return store.add_revision(pid,s['id'],r['variant'],ev,settings,'rules',{**prov,'parent':rid,**diagnostics},bounds=r['range'],activate_initial=False)
