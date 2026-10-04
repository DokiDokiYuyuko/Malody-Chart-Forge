"""Isolated segment inference. Workers return artifacts; only the server selects versions."""
import hashlib
import json
from pathlib import Path
import numpy as np
import soundfile as sf
import librosa
from .advanced import SR, uid, atomic, as_notes
from .charts import Note
from .difficulty import attacks, calibrate, V32_PATTERN_TAGS

def stable_seed(base, pattern, difficulty):
    return (int(base)+int(hashlib.sha256((pattern+'--'+difficulty).encode()).hexdigest()[:8],16))%2147483640

def _v32_time_bounds(sample_range):
    """Quantize sample bounds to the integer-ms timeline emitted by osu files."""
    return tuple(int(round(sample * 1000 / SR)) for sample in sample_range)

def _v32_owned_notes(notes, sample_range):
    start_ms,end_ms=_v32_time_bounds(sample_range)
    return [note for note in notes if start_ms<=note.start<end_ms]

def owned(raw, start, end, source_end):
    events=[]; dropped=[]; occupied=[-1e9]*4; heads=[-1e9]*4;leading=[]
    for n in sorted(raw,key=lambda n:(n.start,n.lane)):
        if n.end is not None and n.start<start<n.end:leading.append({'lane':n.lane,'start_ms':n.start,'end_ms':n.end,'type':'head_outside_segment'})
        if not start<=n.start<end:continue
        if n.lane not in range(4) or not np.isfinite(n.start) or n.start<=heads[n.lane]+.1 or n.start<occupied[n.lane]-.1 or (n.end is not None and (not np.isfinite(n.end) or n.end<=n.start)):
            dropped.append({'start_ms':n.start,'lane':n.lane,'reason':'结构无效或占轨冲突'});continue
        tail=min(n.end,source_end) if n.end is not None else None
        events.append({'id':uid(),'start_ms':float(n.start),'end_ms':float(tail) if tail is not None else None,'lane':int(n.lane)})
        occupied[n.lane]=min(tail,end) if tail is not None else n.start;heads[n.lane]=n.start
    return events,{'discarded_invalid':dropped,'excluded_leading_holds':leading}

def timing_reference_info(p):
    tempo = p.get('tempo', {})
    points = tempo.get('points') or [[0,tempo.get('bpm')]]
    previous = -1
    for row in points:
        if not isinstance(row,(list,tuple)) or len(row)!=2 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not np.isfinite(v) for v in row) or not row[0]>previous or not 20<=row[1]<=600:
            raise ValueError('原曲节拍参考不可用，请先确认项目 BPM 锚点')
        previous = row[0]
    if points[0][0] != 0:raise ValueError('原曲节拍参考首个锚点须在 0 ms')
    source = tempo.get('reference_source') or ('user_confirmed' if tempo.get('manual') else 'project_tempo_reference' if tempo.get('points') else 'original_audio_analysis')
    return {'source':source,'uncertain':bool(tempo.get('uncertain',not tempo.get('manual',False))),
            'points':[[float(t),float(bpm)] for t,bpm in points],
            'source_pcm_sha256':p.get('source_pcm_sha256'), 'points_metadata':tempo.get('points_metadata'),
            'reason':'声部缺少可靠节拍上下文时复用原曲参考；不将自动估计标为已确认'}


def write_reference(path,p):
    points=timing_reference_info(p)['points']
    text='osu file format v14\n\n[General]\nAudioFilename: source.wav\nMode: 3\n\n[Metadata]\nTitle: Segment reference\nArtist: Local\nCreator: Startrail\nVersion: Timing\n\n[Difficulty]\nCircleSize:4\nOverallDifficulty:8\nHPDrainRate:5\nApproachRate:5\nSliderMultiplier:1.4\nSliderTickRate:1\n\n[TimingPoints]\n'
    text+='\n'.join(f'{t},{60000/bpm},4,1,0,100,1,0' for t,bpm in points)+'\n\n[HitObjects]\n'
    path.write_text(text,encoding='utf-8');return path

def run(source,directory,options,progress):
    if options['_advanced']['settings'].get('dynamic_enabled',False):
        return _run_dynamic(source,directory,options,progress)
    snapshot=options['_advanced'];p=snapshot['project'];s=snapshot['segment'];settings=snapshot['settings'];selected=snapshot['variants']
    directory=Path(directory);directory.mkdir(exist_ok=True)
    data,rate=sf.read(source,dtype='float32',always_2d=True)
    if rate!=SR:raise ValueError('高级项目 PCM 采样率错误')
    descriptor = snapshot.get('source')
    if descriptor:
        from .separation import pcm_hash
        if len(data) != descriptor['frame_count'] or pcm_hash(data) != descriptor.get('pcm_sha'):raise ValueError('生成输入与冻结音源快照不匹配')
    start,end=s['start_sample']*1000/SR,s['end_sample']*1000/SR
    a=max(0,s['start_sample']-4*SR);b=min(len(data),s['end_sample']+4*SR);context_start=a*1000/SR
    y=librosa.resample(data[a:b].mean(axis=1),orig_sr=SR,target_sr=22050);duration=(b-a)*1000/SR
    patterns=list(dict.fromkeys(v['pattern'] for v in selected));output=[];errors=[]
    use_v32=settings['engine']=='v32';engine=None;wave=None
    if not use_v32:
        from .engine import Engine
        engine=Engine();wave=engine.prepare(y,22050,progress)
    else:
        full=librosa.resample(data.mean(axis=1),orig_sr=SR,target_sr=22050)
        sf.write(directory/'v32-input.wav',full,22050,subtype='FLOAT' if snapshot.get('source') else 'PCM_16')
    for pi,pattern in enumerate(patterns):
        keys=[v['difficulty'] for v in selected if v['pattern']==pattern]
        conditions=settings['conditions'][settings['engine']]
        fast=settings['strategy']=='fast';infer_keys=['master'] if fast else keys
        local={**settings,'title':p['title'],'artist':p['artist'],'pattern':pattern,'patterns':[pattern],'difficulties':keys}
        local['v32_descriptors']=list(dict.fromkeys([*settings['v32_descriptors'],*([V32_PATTERN_TAGS[pattern]] if pattern in V32_PATTERN_TAGS else [])]))
        if len(local['v32_descriptors'])>4:raise ValueError('排键倾向与 V32 标签合计不能超过四个')
        if local['v32_negative_descriptors'] and (settings['v32_cfg_scale']<=1 or len(local['v32_negative_descriptors'])!=len(local['v32_descriptors'])):raise ValueError('排除标签数量须匹配正向标签，且 CFG 大于 1')
        raw={};metadata={}
        try:
            if use_v32:
                from .mapperatorinator import generate
                pattern_dir=directory/pattern;pattern_dir.mkdir()
                local.update(start_time=start,end_time=end,_advanced_presets=[{'key':key,'label':key,'sr':max(conditions.values()) if fast else conditions[key], 'seed':stable_seed(settings['seed'],pattern,key)} for key in infer_keys])
                if p['tempo'].get('manual') or p['tempo'].get('points') or (descriptor or {}).get('source_role') in ('vocals','accompaniment'):
                    local['timing_reference']=str(write_reference(directory/'timing.osu',p))
                results,metadata=generate(directory/'v32-input.wav',pattern_dir,local,progress)
                if local.get('timing_reference'):metadata['timing_reference']=timing_reference_info(p)
                raw={key:result[0] for key,result in results.items()}
                metadata['timing']={key:result[1] for key,result in results.items()}
            else:
                for key in infer_keys:
                    cond=max(conditions.values()) if fast else conditions[key];local.update(seed=stable_seed(settings['seed'],pattern,key),mug_difficulty=cond)
                    result=engine.generate(wave,None,local,lambda x:progress(f'{pattern} · {key} 模型推理',20+(pi+x)/len(patterns)*65))
                    raw[key]=[Note(n.start+context_start,n.lane,n.end+context_start if n.end is not None else None) for n in result]
            cache={'format':1,'range':[s['start_sample'],s['end_sample']],'context_range':[a,b],'model':settings['engine'],'model_version':'MuG v1.0.0' if not use_v32 else 'Mapperatorinator V32',
                   'settings':settings,'pattern':pattern,'metadata':metadata,'raw':{key:[[n.start,n.lane,n.end] for n in rows] for key,rows in raw.items()}}
            cache_file=directory/(pattern+'-mother.json');atomic(cache_file,cache)
            if fast:
                master=raw['master'];relative=[Note(n.start-context_start,n.lane,n.end-context_start if n.end is not None else None) for n in master]
                candidates=attacks(y,22050,relative)
                cache['candidates']=[[t,strength,[[n.start,n.lane,n.end] for n in ns]] for t,strength,ns in candidates];atomic(cache_file,cache)
            for key in keys:
                if snapshot.get('_stem_raw_only'):
                    original=raw['master'] if fast else raw[key]
                elif fast:
                    n,_=calibrate(candidates,duration,key,settings['ln_ratio'],stable_seed(settings['seed'],pattern,key),settings['difficulty_rules'][key],pattern,min_notes=0)
                    original=[Note(x.start+context_start,x.lane,x.end+context_start if x.end is not None else None) for x in n]
                else:original=raw[key]
                events,diagnostics=owned(original,start,end,len(data)*1000/SR)
                output.append({'variant':pattern+'--'+key,'events':events,'kind':'stem_raw' if snapshot.get('_stem_raw_only') else 'fast' if fast else 'model_raw','settings':settings,
                    'provenance':{'seed':stable_seed(settings['seed'],pattern,'master' if fast else key),'model_version':cache['model_version'],'cache_job':directory.name,'cache_file':cache_file.name,'context_range':[a,b],'timing_reference':metadata.get('timing_reference'),**({k:v for k,v in descriptor.items() if k!='path'} if descriptor else {}),**diagnostics}})
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


V32_CONDITION_GROUP_TOLERANCE = .25


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
            if current and (not contiguous or max(conditions)-min(conditions)>tolerance or span_too_long):
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
        retry_rows=[row for row in members if row.get('retry_min_heads')]
        if retry_rows:
            grouped['retry_min_heads']=sum(row['retry_min_heads'] for row in retry_rows)
            grouped['retry_sections']=[{'start_time':row['start_time'],'end_time':row['end_time'],
                                        'retry_min_heads':row['retry_min_heads']}
                                       for row in retry_rows]
            grouped['retry_condition']=min(10.,condition+.35)
            from .section_plan import canonical_hash
            grouped['retry_seed']=int(canonical_hash({'group':key,'members':[row.get('retry_seed') for row in retry_rows]})[:8],16)%2147483640
        else:
            for name in ('retry_min_heads','retry_condition','retry_seed'):
                grouped.pop(name,None)
        merged.append(grouped)
        for row in members:owners[row['key']]=grouped
    return merged,owners


def _stable_events(raw,a,b,duration,recipe):
    from .section_plan import canonical_hash
    events,diagnostics=owned(raw,a,b,duration)
    for event in events:
        event['id']='note-'+canonical_hash({'recipe':recipe,'start':event['start_ms'],'lane':event['lane'],'end':event['end_ms']})[:32]
    return events,diagnostics


def _run_dynamic(source,directory,options,progress):
    from .section_plan import build_plan,validate_plan,canonical_hash,pcm_audio
    from .adaptive_difficulty import attacks_adaptive,calibrate_adaptive,repair_playable
    snapshot=options['_advanced'];p=snapshot['project'];segment=snapshot['segment'];settings=snapshot['settings'];variants=snapshot['variants']
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    data=pcm_audio(source);source_sha=hashlib.sha256(data.tobytes()).hexdigest()
    descriptor=snapshot.get('source') or {'source_id':'original','source_role':'mix','pcm_sha':source_sha,'parent_source_id':source_sha}
    if descriptor.get('pcm_sha') and descriptor['pcm_sha']!=source_sha:raise ValueError('模型输入声部 PCM 校验失败')
    parent_sha=descriptor.get('parent_source_id') or p.get('source_pcm_sha256') or source_sha
    plan=snapshot.get('section_plan') or build_plan(source,settings,p.get('tempo',{}))
    validate_plan(plan,parent_sha)
    if plan['samples']!=len(data):raise ValueError('段落计划与音源采样数不匹配')
    atomic(directory/'section-plan.json',plan)
    use_v32=settings['engine']=='v32'
    start,end=segment['start_sample']*1000/SR,segment['end_sample']*1000/SR
    if use_v32:start,end=_v32_time_bounds((segment['start_sample'],segment['end_sample']))
    cores=[]
    for section in plan['sections']:
        a,b=max(section['core'][0],segment['start_sample']),min(section['core'][1],segment['end_sample'])
        if a<b:cores.append((section,a,b,max(0,a-4*SR),min(len(data),b+4*SR)))
    if not cores:raise ValueError('段落计划未覆盖选定片段')
    fast=settings['strategy']=='fast';raw_only=snapshot.get('_stem_raw_only',False)
    full=librosa.resample(data.mean(axis=1),orig_sr=SR,target_sr=22050)
    if use_v32:sf.write(directory/'v32-input.wav',full,22050,subtype='FLOAT')
    engine=None;output=[];errors=[];timings={};models={}
    if not use_v32:
        from .engine import Engine
        engine=Engine()
    try:
        patterns=list(dict.fromkeys(v['pattern'] for v in variants))
        for pattern in patterns:
            keys=[v['difficulty'] for v in variants if v['pattern']==pattern]
            infer_keys=['master'] if fast else keys
            local={**settings,'title':p['title'],'artist':p['artist'],'pattern':pattern,'patterns':[pattern],'difficulties':keys}
            local['v32_descriptors']=list(dict.fromkeys([*settings.get('v32_descriptors',[]),*([V32_PATTERN_TAGS[pattern]] if pattern in V32_PATTERN_TAGS else [])]))
            if len(local['v32_descriptors'])>4:raise ValueError('排键倾向与 V32 标签合计不能超过四个')
            if local.get('v32_negative_descriptors') and (settings['v32_cfg_scale']<=1 or len(local['v32_negative_descriptors'])!=len(local['v32_descriptors'])):raise ValueError('排除标签数量须匹配正向标签，且 CFG 大于 1')
            raw={key:[] for key in infer_keys};records=[];metadata={};requests=[];failed_keys=set()
            for section,a,b,ca,cb in cores:
                for key in infer_keys:
                    detail=section['per_difficulty'][key]
                    condition=detail['model_condition']
                    if fast:
                        base=max(settings['conditions'][settings['engine']].values())
                        condition=float(np.clip(base+detail['model_condition']-detail['base_model_condition'],1 if use_v32 else 1.5,10 if use_v32 else 8))
                    seed=_dynamic_seed(settings['seed'],settings['engine'],descriptor,pattern,key,[a,b])
                    start_ms,end_ms=_v32_time_bounds((a,b))
                    request={'key':key+'__'+section['id'],'label':key+' '+section['id'],'sr':condition,'seed':seed,
                             'start_time':start_ms,'end_time':end_ms,'difficulty_key':key,
                             'core':[a,b],'context':[ca,cb],'section_id':section['id']}
                    # One bounded retry in the same V32 worker, only for a
                    # credible evidence-supported deficit below feasible caps.
                    expected=detail['target_heads_soft']*(b-a)/(section['core'][1]-section['core'][0])
                    onset_evidence=sum(max(0.,float(point.get('onset_rate',0))) *
                                       max(0,min(b,point['end_sample'])-max(a,point['start_sample']))/SR
                                       for point in section['profile'])
                    has_audible_rhythm=(section['active_seconds']>2 and
                                        onset_evidence>=max(3.,section['active_seconds']*.5))
                    if not fast and has_audible_rhythm and expected>=8 and expected<detail['hard_caps']['peak_1s']*(b-a)/SR:
                        request.update(retry_min_heads=round(expected*.75),retry_condition=min(10,condition+.35),
                                       retry_seed=_dynamic_seed(settings['seed'],settings['engine'],descriptor,pattern,key,[a,b],1))
                    request.update(active_seconds=round(float(section['active_seconds'])*(b-a)/max(1,section['core'][1]-section['core'][0]),3),
                                   onset_evidence=round(onset_evidence,2),has_audible_rhythm=bool(has_audible_rhythm))
                    requests.append(request)
            max_group_samples={'fine':16,'balanced':32,'coarse':48}.get(settings.get('section_granularity','balanced'),32)*SR
            inference_requests,request_groups=(_coalesce_v32_requests(requests,max_span_samples=max_group_samples)
                                               if use_v32 else (requests,{r['key']:r for r in requests}))
            try:
                if use_v32:
                    from .mapperatorinator import generate
                    folder=directory/pattern;folder.mkdir(exist_ok=True)
                    local.update(start_time=start,end_time=end,_advanced_presets=inference_requests)
                    if p.get('tempo',{}).get('manual') or p.get('tempo',{}).get('points') or descriptor.get('source_role') in ('vocals','accompaniment'):
                        local['timing_reference']=str(write_reference(directory/'timing.osu',p))
                    results,metadata=generate(directory/'v32-input.wav',folder,local,progress)
                    if local.get('timing_reference'):metadata['timing_reference']=timing_reference_info(p)
                    metadata['condition_coalescing']={'original_requests':len(requests),'model_passes':len(inference_requests),
                                                       'condition_tolerance':V32_CONDITION_GROUP_TOLERANCE}
                    for request in requests:
                        group=request_groups[request['key']]
                        attempt=results.get(group['key'])
                        valid_retry=results.get(group['key']+'__retry')
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
                        retry=results.get(group['key']+'__retry')
                        all_attempts=[] if recovered_primary else [{'retry':0,'notes':[[n.start,n.lane,n.end] for n in owned_notes]}]
                        if retry:
                            retry_notes=_v32_owned_notes(retry[0],request['core'])
                            retry_count=len(retry_notes)
                            all_attempts.append({'retry':1,'notes':[[n.start,n.lane,n.end] for n in retry_notes]})
                            if len(retry_notes)>len(owned_notes):owned_notes,timing=retry_notes,retry[1]
                        raw[request['difficulty_key']].extend(owned_notes)
                        records.append({**request,'inference_group':group['key'],'inference_condition':group['sr'],
                                        'first_heads':first_count,'retry_heads':retry_count,'chosen_heads':len(owned_notes),
                                        'recovered_from_rejected_primary':recovered_primary,
                                        'rejected_primary':metadata.get('rejected_charts',{}).get(group['key']),
                                        'underfilled_active_audio':bool(request.get('has_audible_rhythm') and
                                            request.get('retry_min_heads') and len(owned_notes)<request['retry_min_heads']),
                                        'attempts':all_attempts,'timing':timing})
                        if request['difficulty_key']==infer_keys[0]:
                            active=next((bpm for t,bpm in reversed(timing) if t<=a),timing[0][1])
                            timings.setdefault(pattern,[]).append([a,active])
                            timings[pattern].extend([t,bpm] for t,bpm in timing if a<t<b)
                else:
                    for section,a,b,ca,cb in cores:
                        y=librosa.resample(data[ca:cb].mean(axis=1),orig_sr=SR,target_sr=22050)
                        wave=engine.prepare(y,22050,progress)
                        for request in [r for r in requests if r['section_id']==section['id']]:
                            cfg={**local,'seed':request['seed'],'mug_difficulty':request['sr']}
                            generated=engine.generate(wave,None,cfg,lambda fraction:progress(pattern+' · '+request['label'],20+fraction*65))
                            notes=[Note(n.start+ca*1000/SR,n.lane,n.end+ca*1000/SR if n.end is not None else None) for n in generated]
                            attempts=[{'retry':0,'notes':[[n.start,n.lane,n.end] for n in notes]}]
                            if request.get('retry_min_heads') and sum(a*1000/SR<=n.start<b*1000/SR for n in notes)<request['retry_min_heads']:
                                cfg.update(seed=request['retry_seed'],mug_difficulty=min(8,request['sr']+.35))
                                generated=engine.generate(wave,None,cfg,lambda fraction:progress(pattern+' · 局部容量重试',20+fraction*65))
                                retry=[Note(n.start+ca*1000/SR,n.lane,n.end+ca*1000/SR if n.end is not None else None) for n in generated]
                                attempts.append({'retry':1,'notes':[[n.start,n.lane,n.end] for n in retry]})
                                if sum(a*1000/SR<=n.start<b*1000/SR for n in retry)>sum(a*1000/SR<=n.start<b*1000/SR for n in notes):notes=retry
                            raw[request['difficulty_key']].extend(n for n in notes if a*1000/SR<=n.start<b*1000/SR)
                            records.append({**request,'attempts':attempts})
                raw={key:sorted(notes,key=lambda n:(n.start,n.lane)) for key,notes in raw.items()}
                recipe=canonical_hash({'plan':plan['content_hash'],'source':descriptor,'pattern':pattern,'requests':requests,
                                       'inference_requests':inference_requests,'engine':settings['engine']})
                cache={'format':2,'range':[segment['start_sample'],segment['end_sample']],'context_range':[0,len(data)],
                       'model':settings['engine'],'model_version':'Mapperatorinator V32' if use_v32 else 'MuG v1.0.0',
                       'settings':settings,'pattern':pattern,'section_plan':plan,'metadata':metadata,'records':records,
                       'inference_groups':inference_requests if use_v32 else [],
                       'source':{k:v for k,v in descriptor.items() if k!='path'},'raw_recipe_hash':recipe,
                       'raw':{key:[[n.start,n.lane,n.end] for n in notes] for key,notes in raw.items()}}
                candidates=attacks_adaptive(full,22050,raw['master']) if fast and not raw_only else None
                if candidates is not None:cache['candidates']=[[t,score,[[n.start,n.lane,n.end] for n in notes]] for t,score,notes in candidates]
                cache_file=directory/(pattern+'-mother.json');atomic(cache_file,cache)
                models[pattern]=cache['model_version']
                for key in keys:
                    if ('master' if fast else key) in failed_keys:continue
                    originals=raw['master'] if fast else raw[key]
                    events,diagnostics=_stable_events(originals,start,end,len(data)*1000/SR,recipe+key)
                    provenance={'timing_reference':metadata.get('timing_reference'),'seed':settings['seed'],'model_version':cache['model_version'],'cache_job':directory.name,
                                'cache_file':cache_file.name,'section_plan_id':plan['id'],'applied_plan_hash':plan['content_hash'],
                                'raw_recipe_hash':recipe,'source_id':descriptor['source_id'],'source_role':descriptor.get('source_role','mix'),
                                'parent_source_id':parent_sha,'stem_set_id':descriptor.get('stem_set_id'),'context_range':[0,len(data)],
                                'core_conditions':[r for r in records if r['difficulty_key']==('master' if fast else key)],
                                'active_audio_gaps':([{'start_ms':round(r['core'][0]*1000/SR),'end_ms':round(r['core'][1]*1000/SR),
                                    'difficulty':key,'pattern':pattern,'notes':r['chosen_heads'],'expected_minimum':r['retry_min_heads'],
                                    'onset_evidence':r['onset_evidence'],'active_seconds':r['active_seconds']}
                                    for r in records if r['difficulty_key']==('master' if fast else key) and r.get('underfilled_active_audio')]
                                    if descriptor.get('source_role','mix') in ('mix','accompaniment') else []),**diagnostics}
                    output.append({'variant':pattern+'--'+key,'events':events,'kind':'stem_raw' if raw_only and descriptor.get('source_role') in ('vocals','accompaniment') else 'model_raw',
                                   'settings':settings,'provenance':{**provenance,'global_budget_applied':False},'activate_initial':False})
                    if raw_only:continue
                    rule=settings['difficulty_rules'][key]
                    if fast:
                        # Clip once before spending this segment's budget. Context
                        # attacks remain evidence, never consume other core quotas.
                        subset=[(t-start,score,[Note(n.start-start,n.lane,n.end-start if n.end is not None else None) for n in notes]) for t,score,notes in candidates if start<=t<end]
                        selected,report=calibrate_adaptive(subset,end-start,key,settings['ln_ratio'],_dynamic_seed(settings['seed'],settings['engine'],descriptor,pattern,key,[segment['start_sample'],segment['end_sample']]),rule,pattern,plan,origin_ms=start)
                        playable=[Note(n.start+start,n.lane,n.end+start if n.end is not None else None) for n in selected]
                    else:
                        playable,report=repair_playable(originals,len(data)*1000/SR,rule,pattern)
                    candidate_events,candidate_diag=_stable_events(playable,start,end,len(data)*1000/SR,recipe+key+'playable')
                    output.append({'variant':pattern+'--'+key,'events':candidate_events,'kind':'fast' if fast else 'rules',
                                   'settings':settings,'provenance':{**provenance,**candidate_diag,'playability':report,
                                   'raw_preserved':True,'global_budget_applied':bool(fast)},'activate_initial':True})
            except Exception as exc:errors.append({'pattern':pattern,'error':str(exc)})
    finally:
        if engine:engine.unload()
    if not output:raise ValueError('所有片段组合生成失败：'+json.dumps(errors,ensure_ascii=False))
    progress('分段原谱和可玩候选已生成',98)
    timings={pattern:sorted({float(t):float(bpm) for t,bpm in points}.items()) for pattern,points in timings.items()}
    return {'advanced_result':output,'errors':errors,'bounds':[segment['start_sample'],segment['end_sample']],
            'section_plan_id':plan['id'],'timings':timings,'model_versions':models}


def generate_sectioned(source_wav,directory,settings,variants,plan,progress,raw_only=False):
    """Ordinary pipeline hook: same core inference/raw/candidate policy as advanced."""
    options={'_advanced':{'project':{'title':settings['title'],'artist':settings['artist'],'tempo':plan.get('tempo_reference',{}),
                                   'source_pcm_sha256':plan['source_pcm_sha']},
                         'segment':{'start_sample':0,'end_sample':plan['samples']},'settings':settings,
                         'variants':variants,'section_plan':plan,'_stem_raw_only':raw_only}}
    return _run_dynamic(source_wav,directory,options,progress)

def rule_candidate(store,pid,rid,settings):
    r=store.revision(pid,rid);p=store.load(pid);s=store.segment(p,r['segment_id']);pattern,key=r['variant'].split('--');a,b=(v*1000/SR for v in r['range'])
    # Rule-only candidates retain their actual engine and sampling provenance.
    settings={**r['settings'],'difficulty_rules':settings['difficulty_rules'],'ln_ratio':settings['ln_ratio'],'seed':settings['seed']}
    prov=r.get('provenance',{});cache=None
    if settings['strategy']=='fast' and not prov.get('global_budget_applied') and prov.get('cache_job') and prov.get('cache_file'):
        from .paths import ROOT
        from .advanced import identifier,read
        name=prov['cache_file']
        if Path(name).name!=name:raise ValueError('缓存文件名无效')
        path=ROOT/'outputs'/identifier(prov['cache_job'])/name
        if path.is_file():cache=read(path)
    if settings.get('dynamic_enabled',False) or prov.get('global_budget_applied'):
        from .adaptive_difficulty import calibrate_adaptive,repair_playable
        from .section_plan import build_plan
        if not prov.get('global_budget_applied') and settings['strategy']=='fast' and cache and cache.get('format')==2 and cache.get('candidates'):
            plan=build_plan(store.directory(pid)/'source.wav',settings,p.get('tempo',{}))
            candidates=[(t-a,strength,[Note(x[0]-a,x[1],x[2]-a if x[2] is not None else None) for x in originals])
                        for t,strength,originals in cache['candidates'] if a<=t<b]
            notes,report=calibrate_adaptive(candidates,b-a,key,settings['ln_ratio'],settings['seed'],settings['difficulty_rules'][key],pattern,plan,origin_ms=a)
        else:
            notes,report=repair_playable(as_notes(r['events'],a,b),b-a,settings['difficulty_rules'][key],pattern)
        raw=[Note(n.start+a,n.lane,n.end+a if n.end is not None else None) for n in notes]
        ev,diagnostics=owned(raw,a,b,p['duration']*1000)
        previous={(event['start_ms'],event['lane'],event.get('end_ms')):event for event in r['events']}
        from .section_plan import canonical_hash
        for event in ev:
            old=previous.get((event['start_ms'],event['lane'],event.get('end_ms')))
            if old:
                event['id']=old['id']
                for field in ('origins','audio_evidence'):
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
        notes,_=calibrate(candidates,(context[1]-context[0])*1000/SR,key,settings['ln_ratio'],settings['seed'],settings['difficulty_rules'][key],pattern,min_notes=0)
        raw=[Note(n.start+origin,n.lane,n.end+origin if n.end is not None else None) for n in notes]
    else:
        from .charts import clean_notes
        cap=settings['difficulty_rules'][key]['hold_ms']
        originals=[Note(n.start,n.lane,min(n.end,n.start+cap) if n.end is not None else None) for n in as_notes(r['events'],a,b)]
        raw=clean_notes(originals,b-a,{**settings['difficulty_rules'][key],'min_notes':0})
        raw=[Note(n.start+a,n.lane,n.end+a if n.end is not None else None) for n in raw]
    ev,diagnostics=owned(raw,a,b,p['duration']*1000)
    return store.add_revision(pid,s['id'],r['variant'],ev,settings,'rules',{**prov,'parent':rid,**diagnostics},bounds=r['range'],activate_initial=False)
