"""Frozen quality contracts, candidate derivation and final density reporting."""
import copy
from pathlib import Path

from .advanced import SR, atomic, identifier, read, uid
from .section_plan import canonical_hash


def candidate_contract(version=None):
    version=version or 'model-heads-only-v3'
    if version not in ('evidenced-single-heads-v1','evidenced-single-heads-v2','model-heads-only-v3'):
        raise ValueError('冻结候选策略版本不受支持')
    policy={'version':version,'extra_chords':False,'extra_holds':False,
        'budget_passes':1,'model_support_radius_ms':15.,'detector_duplicate_radius_ms':1.,
        'cross_detector_max_radius_ms':15.,'cached_multiscale_min_strength':.6,
        'seam_constraints':'selected_prefix','refill_scope':'frozen_phrase',
        'alignment':'model_heads_only'}
    if version=='evidenced-single-heads-v2':
        policy.update(selection_policy='model_skeleton_then_audio',audio_vote_cap=1.,
                      skeleton_qualification='multiscale_onset_or_spectral_sustain',
                      fill_constraints='selected_prefix_and_fixed_future_model_heads',
                      skeleton_scope='available_frozen_model_supply',full_replay_skeleton_scope='complete_frozen_plan',
                      stem_audio_original_audibility=True,fusion_version='stem-fusion-v5')
    if version=='model-heads-only-v3':
        from .fusion import VERSION
        policy.update(selection_policy='model_only', audio_vote_cap=0., candidate_source='immutable_model_heads',
                      acoustic_new_heads=False, refill_scope='model_heads_in_frozen_phrase',
                      model_chord_policy='whole_group_when_hard_constraints_allow',
                      stem_audio_original_audibility=True, fusion_version=VERSION)
    return {**policy,'hash':canonical_hash(policy)}


def freeze(settings):
    from .chart_quality import contract
    from .density_calibration import freeze as density_freeze, v32_execution_identity
    observations=[]
    from .paths import ROOT
    path=ROOT/'data'/'density-calibration'/'current.json'
    if path.is_file():
        record=read(path)
        if record.get('hash')!=canonical_hash({k:v for k,v in record.items() if k!='hash'}):
            raise ValueError('密度校准资料校验失败')
        observations=record.get('observations',[])
    identity=v32_execution_identity() if settings.get('engine')=='v32' else None
    return {'quality_policy': contract(), 'density_policy': density_freeze(settings,observations,execution_identity=identity),
            'candidate_policy':candidate_contract()}


def evidence_for(project_directory, original, descriptors=()):
    from .event_evidence import cached_evidence
    stems = {row['source_role']: row['path'] for row in descriptors
             if row.get('source_role') in ('vocals', 'accompaniment')}
    return cached_evidence(original, stems=stems, cache_dir=Path(project_directory)/'event-evidence')


def candidate_events(notes, candidates, raw_events, *, origin_ms=0., raw_revision_id=None,
                     source_role='mix', source_id=None):
    """Materialize selected real model heads or one evidenced acoustic singleton."""
    used=set();audio_used=set();output=[]
    candidate_index={round(c[0],6):c for c in candidates}
    for note in notes:
        absolute=note.start+origin_ms
        matches=[e for e in raw_events if e['id'] not in used and abs(e['start_ms']-absolute)<.01]
        candidate=candidate_index.get(round(note.start,6))
        if matches:
            head=min(matches,key=lambda e:e['lane']!=note.lane);used.add(head['id'])
            event=copy.deepcopy(head)
            event.setdefault('origins',[{'revision_id':raw_revision_id,'note_id':head['id'],
                'source_id':source_id,'stem_role':source_role,'original_start_ms':head['start_ms'],
                'original_lane':head['lane'],'original_end_ms':head.get('end_ms')}])
            event['candidate_provenance']={'kind':'model'}
        elif candidate is not None and not candidate[2] and getattr(candidate,'support',None):
            proof=copy.deepcopy(candidate.support)
            identity=canonical_hash({'source_id':source_id,'start_ms':absolute,'support':proof})
            if identity in audio_used:raise ValueError('同一起音证据不能生成多个声学音符头')
            audio_used.add(identity)
            event={'id':'audio-'+identity[:32],'origins':[{'note_id':'audio-'+identity[:32],
                'source_id':source_id,'stem_role':'original_acoustic','candidate_kind':'acoustic',
                'original_start_ms':absolute,'original_lane':None,'original_end_ms':None,'support':proof}],
                'candidate_provenance':copy.deepcopy(candidate.provenance),'candidate_support':proof}
            if note.end is not None:raise ValueError('声学单头候选不能凭空生成长条')
        else:raise ValueError('候选音符缺少原谱或冻结起音证据')
        event.update(start_ms=absolute,lane=note.lane,end_ms=None if note.end is None else note.end+origin_ms)
        if candidate is not None:event['candidate_support']=copy.deepcopy(getattr(candidate,'support',{}))
        output.append(event)
    return output


def finish(events, settings, snapshot, original, project_directory, *, candidates=None, attempts=0,
           evidence=None, pattern=None, difficulty=None, bounds=None):
    from .chart_quality import apply
    from .density_validation import evaluate
    from .advanced_generation import validate_frozen_policies
    validate_frozen_policies(snapshot)
    pattern = pattern or snapshot['variants'][0]['pattern']
    difficulty = difficulty or snapshot['variants'][0]['difficulty']
    bounds = bounds or [snapshot['segment']['start_sample'], snapshot['segment']['end_sample']]
    evidence = evidence or evidence_for(project_directory, original, snapshot.get('input_sources', ()))
    plan=copy.deepcopy(snapshot.get('section_plan'))
    if plan:plan['region_boundaries']=list(bounds)
    changed = apply(events, settings, evidence, difficulty, pattern,
                    timing=snapshot.get('timing_map'), plan=plan, policy=snapshot.get('quality_policy'))
    # The frozen candidate remains owned by its original half-open region.
    old = {row['id']:row for row in events}
    changed_ids = set()
    for row in changed['events']:
        if not bounds[0]*1000/SR <= row['start_ms'] < bounds[1]*1000/SR:
            row['start_ms'] = old[row['id']]['start_ms']; changed_ids.add(row['id'])
    if changed_ids:
        changed['decisions'].append({'type':'boundary_rollback','note_ids':sorted(changed_ids),
                                    'reason':'原片段归属保护'})
    from .audio_bounds import content_end
    validation = evaluate(changed['events'], snapshot.get('section_plan'), difficulty, bounds,
                          duration=content_end(snapshot['project'])/SR, candidates=candidates, attempts=attempts)
    return {**changed, 'density_validation':validation}


def derive_many(store, pid, payload):
    """Derive one full-chart policy pass without replacing any adopted version."""
    from .chart_quality import apply, contract
    from .density_validation import evaluate
    request_id = payload.get('request_id')
    if not isinstance(request_id,str) or not 8 <= len(request_id) <= 120 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in request_id):
        raise ValueError('修订请求 ID 无效')
    ids = payload.get('revision_ids')
    if not isinstance(ids,list) or not ids or len(ids)>200 or len(set(ids))!=len(ids):
        raise ValueError('请选择不重复的原谱版本')
    path = store.directory(pid)/'quality-requests'/(canonical_hash(request_id)+'.json')
    body = {'revision_ids':ids,'expected_revision':payload.get('expected_revision')}
    with store.lock:
        if path.is_file():
            previous = read(path)
            if previous['request']!=body:raise ValueError('同一请求 ID 不能提交不同修订内容')
            if previous.get('result'):return previous['result']
        project = store.load(pid);store.check(project,payload.get('expected_revision'))
        revisions = [store.revision(pid,identifier(rid)) for rid in ids]
        if len({row['variant'] for row in revisions})!=1:raise ValueError('一次质量修订须属于同一谱面组合')
        if len({row['segment_id'] for row in revisions})!=len(revisions):raise ValueError('同片段不能同时修订多个版本')
        for row in revisions:
            part = store.segment(project,row['segment_id'])
            if row['range']!=[part['start_sample'],part['end_sample']]:raise ValueError('片段范围已变化，请刷新')
        revisions.sort(key=lambda row:row['range'][0])
        if any(a['range'][1]>b['range'][0] for a,b in zip(revisions,revisions[1:])):raise ValueError('修订范围重叠')
        relevant=lambda row:{**{k:row['settings'].get(k) for k in ('ln_ratio','difficulty_rules','nps_ranges')},
                             'direct_v32_policy':row.get('provenance',{}).get('direct_v32_policy')}
        if any(relevant(row)!=relevant(revisions[0]) for row in revisions):raise ValueError('所选版本的冻结规则不同，请分别修订')
        frozen = copy.deepcopy(project)
        record = {'request_id':request_id,'request':body,'policy':contract(),'revisions':ids}
        atomic(path,record)
    directory = store.directory(pid);settings = revisions[0]['settings']
    pattern,key = revisions[0]['variant'].split('--')
    plan_id = revisions[0].get('provenance',{}).get('section_plan_id') or revisions[0].get('provenance',{}).get('plan_hash')
    plan = read(directory/'section-plans'/(plan_id+'.json')) if plan_id else None
    if plan:plan['region_boundaries']=sorted({point for row in revisions for point in row['range']})
    stem_set = revisions[0].get('provenance',{}).get('stem_set_id')
    descriptors=[]
    if stem_set:
        from .separation import resolve_source
        for role in ('vocals','accompaniment'):
            descriptors.append(resolve_source(store,pid,stem_set+':'+role))
    evidence = evidence_for(directory,directory/'source.wav',descriptors)
    events=[];owners={}
    for revision in revisions:
        for event in revision['events']:
            row=copy.deepcopy(event)
            identity=revision['id']+':'+event['id']
            row['id']=identity;owners[identity]=(revision,event)
            row.setdefault('origins',[{'revision_id':revision['id'],'note_id':event['id'],
                'stem_role':revision.get('provenance',{}).get('source_role','mix'),
                'original_start_ms':event['start_ms'],'original_lane':event['lane'],
                'original_end_ms':event.get('end_ms')}])
            events.append(row)
    timing=revisions[0].get('provenance',{}).get('timing_map')
    from .revision_rhythm import hydrate_events
    events=hydrate_events(store,pid,events,owners=owners,plan=plan)
    output=apply(events,settings,evidence,key,pattern,timing=timing,plan=plan,policy=record['policy'])
    if len(output['events'])!=len(events):raise ValueError('质量修订不能改变音符头数量')
    parts={row['id']:[] for row in revisions}
    for event in output['events']:
        revision,old=owners[event['id']]
        if not revision['range'][0]*1000/SR <= event['start_ms'] < revision['range'][1]*1000/SR:
            event['start_ms']=old['start_ms']
        parts[revision['id']].append(event)
    bounds=[revisions[0]['range'][0],revisions[-1]['range'][1]]
    from .audio_bounds import content_end
    duration=content_end(frozen)/SR
    validation=evaluate(output['events'],plan,key,bounds,duration=duration,candidates=len(events))
    saved=[]
    with store.lock:
        latest=store.load(pid);store.check(latest,body['expected_revision'])
        for revision in revisions:
            from .chart_quality import summarize_region
            before=[event for event in events if owners[event['id']][0]['id']==revision['id']]
            local=summarize_region(parts[revision['id']],output['decisions'],before_events=before,
                                   ln_target=output['summary']['ln_target'])
            provenance={**copy.deepcopy(revision.get('provenance',{})), 'parents':[revision['id']],
                        'quality_policy':record['policy'],'quality_revision_of':revision['id'],
                        'span_quality_summary':output['summary'],'span_quality_decisions':output['decisions'],
                        'quality_summary':local['summary'],'quality_evidence_id':evidence.get('id',evidence.get('cache_key')),
                        'quality_decisions':local['decisions'],
                        'density_validation':evaluate(parts[revision['id']],plan,key,revision['range'],
                                                       duration=duration,candidates=len(revision['events']))}
            result=store.add_revision(pid,revision['segment_id'],revision['variant'],parts[revision['id']],
                                      revision['settings'],'quality',provenance,revision['range'],activate_initial=False)
            saved.append({'id':result['id'],'revision_of':revision['id'],'segment_id':revision['segment_id']})
        result={'request_id':request_id,'revisions':saved,'quality_summary':output['summary'],
                'density_validation':validation,'project_revision':store.load(pid)['revision']}
        record['result']=result;atomic(path,record)
    return result


def finalize_generated(source, directory, options, result):
    snapshot=options.get('_advanced',{})
    if snapshot.get('direct_v32_policy'):
        # The direct generator already checks head preservation and quality.
        # Even a failed candidate must retain raw evidence without budget fallback.
        if result.get('direct_v32_policy') != snapshot['direct_v32_policy'] and not result.get('skipped'):
            if any(not r.get('provenance',{}).get('silence_verified') for r in result.get('advanced_result',[])):
                raise ValueError('直接生成结果缺少冻结策略身份')
        return result
    if not snapshot.get('quality_policy') or snapshot.get('task_type','generation')!='generation':return result
    from .advanced_generation import validate_frozen_policies
    validate_frozen_policies(snapshot)
    original=Path(snapshot.get('original_source',{}).get('path',source))
    project_directory=original.parent
    evidence=None;new=[]
    from .advanced_execution import spent_retry_rounds
    rows=result.get('advanced_result',[])
    # IDs must exist before selected heads point back to immutable model supply.
    for row in rows:
        row.setdefault('id',uid())
    raw_sources={r['variant']:r for r in rows if r['kind'] in ('model_raw','stem_raw')}
    playable_keys={r['variant'] for r in result.get('advanced_result',[])
                   if r['kind'] not in ('model_raw','stem_raw')}
    raw_counts={r['variant']:len(r['events']) for r in result.get('advanced_result',[])
                if r['kind'] in ('model_raw','stem_raw')}
    for row in result.get('advanced_result',[]):
        raw=row['kind'] in ('model_raw','stem_raw')
        if raw and (row['variant'] in playable_keys or snapshot.get('auto_fuse')):continue
        if row.get('provenance',{}).get('quality_summary'):
            if row['provenance'].get('quality_policy') == snapshot['quality_policy']:continue
            raise ValueError('候选质量策略与冻结任务不符，请从原谱派生新候选')
        if evidence is None:evidence=evidence_for(project_directory,original,snapshot.get('input_sources',()))
        variant=next(v for v in snapshot['variants'] if v['key']==row['variant'])
        selected=copy.deepcopy(row['events']);playability=None
        parent=raw_sources.get(row['variant'],row);traced=set()
        for event in selected:
            if event.get('origins'):
                for origin in event['origins']:
                    if origin.get('candidate_kind')!='acoustic' and not origin.get('revision_id'):
                        if origin.get('note_id') not in {e['id'] for e in parent['events']}:
                            raise ValueError('模型候选溯源与不可变原谱不符')
                        origin['revision_id']=parent['id']
                continue
            matches=[e for e in parent['events'] if e['id'] not in traced and abs(e['start_ms']-event['start_ms'])<.01]
            if not matches:raise ValueError('候选音符头缺少不可变模型原谱来源')
            head=min(matches,key=lambda e:e['lane']!=event['lane']);traced.add(head['id'])
            event['origins']=[{'revision_id':parent['id'],'note_id':head['id'],
                'stem_role':parent.get('provenance',{}).get('source_role','mix'),
                'original_start_ms':head['start_ms'],'original_lane':head['lane'],'original_end_ms':head.get('end_ms')}]
        if raw:
            from .chart_quality import classify_holds
            from .adaptive_difficulty import model_candidates,calibrate_adaptive
            from .advanced import as_notes
            from .charts import Note
            a,b=(snapshot['segment'][k]*1000/SR for k in ('start_sample','end_sample'))
            available=classify_holds(selected,evidence)['events']
            acoustic=snapshot.get('evidence',{}).get('acoustic') or evidence
            if snapshot.get('candidate_policy',{}).get('selection_policy')=='model_skeleton_then_audio':
                acoustic=evidence
            candidates=model_candidates(as_notes(available,a,b),acoustic,snapshot.get('timing_map'),origin_ms=a,
                selection_policy=snapshot.get('candidate_policy',{}).get('selection_policy','ranked_beam'))
            notes,playability=calibrate_adaptive(candidates,b-a,variant['difficulty'],row['settings']['ln_ratio'],
                row['settings']['seed'],row['settings']['difficulty_rules'][variant['difficulty']],
                variant['pattern'],snapshot['section_plan'],origin_ms=a,evidence=evidence,
                timing=snapshot.get('timing_map'),
                selection_policy=snapshot.get('candidate_policy',{}).get('selection_policy','ranked_beam'),
                audio_vote_cap=snapshot.get('candidate_policy',{}).get('audio_vote_cap'))
            selected=candidate_events(notes,candidates,available,origin_ms=a,raw_revision_id=parent['id'],
                source_role=parent.get('provenance',{}).get('source_role','mix'),
                source_id=snapshot['project'].get('source_pcm_sha256'))
        model_heads=raw_counts.get(row['variant'],len(row['events']))
        checked=finish(selected,row['settings'],snapshot,original,project_directory,
            pattern=variant['pattern'],difficulty=variant['difficulty'],evidence=evidence,
            candidates={'model_heads':model_heads,
                'candidate_heads':model_heads+(playability or row.get('provenance',{}).get('playability',{})).get('candidate_acoustic_heads',0),
                'acoustic_candidate_heads':(playability or row.get('provenance',{}).get('playability',{})).get('candidate_acoustic_heads'),
                'constraint_removed':(playability or row.get('provenance',{}).get('playability',{})).get('phone_filtered_notes',0)},
            attempts=max(row.get('provenance',{}).get('density_validation',{}).get('attempts',0),
                max((spent_retry_rounds(c) for c in row.get('provenance',{}).get('core_conditions',[])),default=0)))
        if raw:
            row.setdefault('id',uid());row['activate_initial']=False
            updated=copy.deepcopy(row);updated.update(id=uid(),kind='quality',activate_initial=True)
            updated['provenance']['parents']=[row['id']];new.append(updated)
            updated['provenance'].update(global_budget_applied=True,budget_stage=1,playability=playability)
        else:updated=row
        updated['events']=checked['events']
        updated['provenance'].update(quality_policy=snapshot['quality_policy'],quality_summary=checked['summary'],
            quality_decisions=checked['decisions'],quality_evidence_id=evidence.get('id',evidence.get('cache_key')),
            density_policy=snapshot.get('density_policy'),candidate_policy=snapshot.get('candidate_policy'),
            density_validation=checked['density_validation'])
    result['advanced_result'].extend(new)
    return result


def assembly_density(revisions, events, duration, mapping=None):
    reports=[r.get('provenance',{}).get('density_validation') for r in revisions]
    if not all(reports):return {'status':'not_evaluated','reason':'历史版本尚未评估'}
    if any(r.get('denominator')=='first_head_to_last_head_or_tail' for r in reports):
        from .nps_star_calibration import chart_span_density
        from .advanced import as_notes
        if not all(r.get('target_range')==reports[0].get('target_range') for r in reports):
            return {'status':'not_evaluated','reason':'组合包含不同 NPS 范围，需分别核查'}
        return chart_span_density(as_notes(events),reports[0]['target_range'])
    target=sum(r['target_heads'] for r in reports);active=sum(r['active_seconds'] for r in reports)
    times=sorted(e['start_ms'] for e in events);left=0;peak=0
    for right,time in enumerate(times):
        while times[left]<=time-1000:left+=1
        peak=max(peak,right-left+1)
    peak_cap=min(r['peak_cap'] for r in reports)
    local=[dict(row) for report in reports for row in report.get('regions',[])]
    from bisect import bisect_left
    for row in local:
        bounds=row.get('bounds',row.get('range'))
        if not bounds:continue
        spans=[bounds] if mapping is None else [
            [max(bounds[0],m['source_start'])-m['source_start']+m['output_start'],
             min(bounds[1],m['source_end'])-m['source_start']+m['output_start']]
            for m in mapping if max(bounds[0],m['source_start'])<min(bounds[1],m['source_end'])]
        row['output_ranges']=spans
        row['peak_1s']=max((bisect_left(times,t+1000)-i for i,t in enumerate(times)
            if any(t<end*1000/SR and t+1000>start*1000/SR for start,end in spans)),default=0)
        row['peak_violation']=row['peak_1s']>row['peak_ceiling']
    violated=any(r.get('peak_violation') for r in local) if local else peak>peak_cap
    passed=all(r.get('passed') for r in reports) and .85*target<=len(events)<=1.15*target and not violated
    status='pass' if passed else 'constraints' if violated else next((r['status'] for r in reports if not r.get('passed')),'underfilled')
    return {'version':reports[0]['version'],'status':status,'passed':passed,'target_heads':target,
        'actual_heads':len(events),'active_seconds':active,'target_nps':target/active if active else 0.,
        'active_nps':len(events)/active if active else 0.,'whole_nps':len(events)/duration if duration else 0.,
        'peak_nps':peak,'peak_cap':peak_cap,'completion':len(events)/target if target else 1.,
        'regions':local,'reason':'hard_constraints' if violated else next((r.get('reason') for r in reports if not r.get('passed')),None),
        'attempts':max(r.get('attempts',0) for r in reports)}


def derive_budget(store,pid,payload):
    """A new budget pass may start only from immutable model supply."""
    from .section_plan import validate_plan
    request_id=payload.get('request_id');identifier(request_id)
    ids=payload.get('revision_ids')
    if not isinstance(ids,list) or not 1<=len(ids)<=200 or len(set(ids))!=len(ids):raise ValueError('请选择不重复的模型原谱')
    path=store.directory(pid)/'quality-requests'/(canonical_hash(request_id)+'.json')
    body={k:payload.get(k) for k in ('revision_ids','expected_revision')};body['mode']='raw_budget'
    with store.lock:
        if path.is_file():
            previous=read(path)
            if previous['request']!=body:raise ValueError('同一请求 ID 不能提交不同修订内容')
            if previous.get('result'):return previous['result']
        p=store.load(pid);store.check(p,body['expected_revision'])
        parents=[store.revision(pid,identifier(rid)) for rid in ids]
        if len({(r['segment_id'],r['variant']) for r in parents})!=len(parents):raise ValueError('同区域组合不能重复编排')
        for row in parents:
            if row.get('provenance',{}).get('direct_v32_policy'):
                raise ValueError('独立星级原谱不能重新削减预算；请修改 NPS 范围后新建生成任务')
            part=store.segment(p,row['segment_id'])
            if row['kind'] not in ('model_raw','stem_raw') or row['provenance'].get('global_budget_applied'):raise ValueError('调整预算必须从原谱派生，不能再次削减成品')
            if row['range']!=[part['start_sample'],part['end_sample']]:raise ValueError('片段范围已变化，请刷新')
    directory=store.directory(pid);output=[]
    for parent in parents:
        row=copy.deepcopy(parent);prov=row['provenance'];pattern,key=row['variant'].split('--')
        plan_id=prov.get('section_plan_id') or prov.get('plan_hash')
        if not plan_id:raise ValueError('原谱缺少冻结编排计划')
        plan=read(directory/'section-plans'/(plan_id+'.json'));validate_plan(plan,p['source_pcm_sha256'])
        policies=freeze(row['settings'])
        descriptors=[]
        if prov.get('stem_set_id'):
            from .separation import resolve_source
            descriptors=[resolve_source(store,pid,prov['stem_set_id']+':'+role) for role in ('vocals','accompaniment')]
        for event in row['events']:
            event.setdefault('origins',[{'revision_id':parent['id'],'note_id':event['id'],
                'stem_role':prov.get('source_role','mix'),'original_start_ms':event['start_ms'],
                'original_lane':event['lane'],'original_end_ms':event.get('end_ms')}])
        snapshot={'project':p,'segment':store.segment(p,row['segment_id']),'settings':row['settings'],
                  'variants':[{'key':row['variant'],'pattern':pattern,'difficulty':key}],
                  'section_plan':plan,'timing_map':prov.get('timing_map'),'input_sources':descriptors,
                  'original_source':{'path':str(directory/'source.wav')},'auto_fuse':False,**policies}
        result=finalize_generated(directory/'source.wav',directory,{'_advanced':snapshot},{'advanced_result':[row]})
        derived=next(r for r in result['advanced_result'] if r['id']!=parent['id'])
        derived['id']=canonical_hash({'request':request_id,'parent':parent['id'],'mode':'raw_budget'})[:32]
        derived['provenance']['budget_revision_of']=parent['id'];output.append((parent,derived))
    with store.lock:
        store.check(store.load(pid),body['expected_revision'])
        for parent,row in output:
            store.add_revision(pid,parent['segment_id'],row['variant'],row['events'],row['settings'],'quality',
                               row['provenance'],parent['range'],activate_initial=False,revision_id=row['id'])
        result={'request_id':request_id,'revisions':[{'id':r['id'],'revision_of':p['id'],'segment_id':p['segment_id'],
                'variant':p['variant'],'density_validation':r['provenance']['density_validation']} for p,r in output],
                'project_revision':store.load(pid)['revision']}
        atomic(path,{'request':body,'result':result});return result


def derive_workflow(store,pid,payload):
    """Replay immutable two-stem supply under one frozen budget, then publish A/B."""
    from .section_plan import validate_plan
    from .separation import resolve_source
    from .stem_generation import _audio_evidence
    from .fusion import fuse_revisions
    from .chart_quality import apply,contract
    from .density_validation import evaluate
    from .audio_bounds import content_end
    request_id=identifier(payload.get('request_id'))
    ids=payload.get('revision_ids')
    if not isinstance(ids,list) or not 1<=len(ids)<=200 or len(ids)!=len(set(ids)):
        raise ValueError('请选择不重复的双路版本')
    directory=store.directory(pid)
    path=directory/'quality-requests'/(canonical_hash(request_id)+'.json')
    body={'mode':'workflow_replay','revision_ids':ids,'expected_revision':payload.get('expected_revision')}
    with store.lock:
        previous=read(path) if path.is_file() else None
        if previous:
            if previous['request']!=body:raise ValueError('同一请求 ID 不能提交不同修订内容')
            if previous.get('result'):return previous['result']
            if previous.get('pending'):
                pending=previous['pending'];current=store.load(pid)
                registered=[any(v['id']==r['id'] for v in store.segment(current,r['segment_id'])['versions'].get(r['variant'],[]))
                            for r in pending['rows']]
                if any(registered) and not all(registered):raise ValueError('候选批次登记不完整，需检查项目记录')
                if not all(registered):store.add_candidate_batch(pid,pending['rows'],body['expected_revision'])
                else:
                    for row in pending['rows']:
                        saved=store.revision(pid,row['id'])
                        if any(saved.get(k)!=row.get(k) for k in ('segment_id','variant','range','events','settings','kind','provenance')):
                            raise ValueError('候选批次校验失败')
                previous['result']=pending['result'];previous['result']['project_revision']=store.load(pid)['revision'];previous.pop('pending')
                atomic(path,previous);return previous['result']
        p=copy.deepcopy(store.load(pid));store.check(p,body['expected_revision'])
        revisions=[store.revision(pid,identifier(rid)) for rid in ids]
        if any(r.get('provenance',{}).get('direct_v32_policy') for r in revisions):
            raise ValueError('独立星级声部须使用保全模型头的融合通路；请新建生成任务')
        if len({r['variant'] for r in revisions})!=1 or len({r['segment_id'] for r in revisions})!=len(revisions):
            raise ValueError('一次重放须属于同一组合，每个区域选择一个版本')
        revisions.sort(key=lambda r:r['range'][0]);pairs=[]
        plan_ids=set();stem_ids=set()
        for row in revisions:
            part=store.segment(p,row['segment_id']);prov=row.get('provenance',{})
            if row['range']!=[part['start_sample'],part['end_sample']] or row['range'][1]>content_end(p):
                raise ValueError('区域或裁尾范围已变化，请刷新')
            parents=[store.revision(pid,identifier(rid)) for rid in prov.get('parents',[])]
            if len(parents)!=2 or {r.get('provenance',{}).get('source_role') for r in parents}!={'vocals','accompaniment'}:
                raise ValueError('双路重放须有完整人声与伴奏原谱')
            for parent in parents:
                if parent['kind']!='stem_raw' or parent['provenance'].get('global_budget_applied'):
                    raise ValueError('重放只能从不可变声部原谱开始，不能重复消费成品预算')
                if parent['range']!=row['range'] or parent['variant']!=row['variant'] or parent['segment_id']!=row['segment_id']:
                    raise ValueError('原谱范围或组合与区域不匹配')
            pairs.append({r['provenance']['source_role']:copy.deepcopy(r) for r in parents})
            plan_ids.add(prov.get('section_plan_id') or prov.get('applied_plan_hash') or prov.get('plan_hash'))
            stem_ids.add(prov.get('stem_set_id'))
        if any(a['range'][1]!=b['range'][0] for a,b in zip(revisions,revisions[1:])):
            raise ValueError('整曲重放区域须连续，不能跳过缺失区域')
        if len(plan_ids)!=1 or None in plan_ids or len(stem_ids)!=1 or None in stem_ids:
            raise ValueError('重放须使用同一冻结编排计划和分离版本')
        settings=revisions[0]['settings']
        if any(any(r['settings'].get(k)!=settings.get(k) for k in ('ln_ratio','difficulty_rules')) for r in revisions):
            raise ValueError('所选版本的冻结质量规则不同')
        plan_id=next(iter(plan_ids));stem_id=next(iter(stem_ids))
        record=previous or {'request':body,'request_id':request_id,'quality_policy':contract(),
            'candidate_policy':candidate_contract(),
            'source_pcm_sha256':p['source_pcm_sha256'],'plan_id':plan_id,'stem_set_id':stem_id}
        if previous and previous['quality_policy']!=contract():raise ValueError('处理中策略已变化，请使用新请求')
        if previous and previous['candidate_policy']!=candidate_contract():raise ValueError('处理中候选策略已变化，请使用新请求')
        atomic(path,record)
    plan=read(directory/'section-plans'/(plan_id+'.json'));validate_plan(plan,p['source_pcm_sha256'])
    descriptors=[resolve_source(store,pid,stem_id+':'+role) for role in ('vocals','accompaniment')]
    if any(pair[role]['provenance'].get('source_id')!=descriptor['source_id']
           or pair[role]['provenance'].get('parent_source_id')!=p['source_pcm_sha256']
           for pair in pairs for role,descriptor in zip(('vocals','accompaniment'),descriptors)):
        raise ValueError('原谱声部身份与原曲时钟不匹配')
    evidence=evidence_for(directory,directory/'source.wav',descriptors)
    manifest=read(directory/'stems'/stem_id/'manifest.json')
    manifest['stems']=[{**d,'role':d['source_role']} for d in descriptors]
    # This deterministic local RMS/leakage layer reuses complete frozen PCM;
    # it does not rerun onset, tempo, separation or model inference.
    import inspect
    energy_recipe={'schema':'stereo-local-energy-leakage-v1','source_pcm_sha':p['source_pcm_sha256'],
        'stem_set_id':stem_id,'parents':[pair[role]['id'] for pair in pairs for role in ('vocals','accompaniment')],
        'implementation_hash':canonical_hash(inspect.getsource(_audio_evidence))}
    energy_path=directory/'candidate-energy'/(canonical_hash(energy_recipe)+'.json')
    if energy_path.is_file():
        energy=read(energy_path)
        if canonical_hash({k:v for k,v in energy.items() if k!='id'})!=energy.get('id') or energy['recipe']!=energy_recipe:
            raise ValueError('冻结原曲相对声部证据校验失败')
        annotations={(r['revision_id'],r['note_id']):r['audio_evidence'] for r in energy['annotations']}
        for pair in pairs:
            for raw in pair.values():
                for event in raw['events']:event['audio_evidence']=copy.deepcopy(annotations[(raw['id'],event['id'])])
    else:
        whole={role:{'events':[event for pair in pairs for event in pair[role]['events']],
                     'provenance':pairs[0][role]['provenance']} for role in ('vocals','accompaniment')}
        _audio_evidence(whole,manifest,directory/'source.wav')
        energy={'recipe':energy_recipe,'annotations':[{'revision_id':raw['id'],'note_id':event['id'],
                'audio_evidence':event['audio_evidence']} for pair in pairs for raw in pair.values() for event in raw['events']]}
        energy['id']=canonical_hash(energy);atomic(energy_path,energy)
    selected=[];fused=[];owners={}
    pattern,key=revisions[0]['variant'].split('--')
    # Legacy frozen acoustic-refill policies require a future model skeleton.
    # Model-only selection consumes the frozen budget once in source order.
    skeletons=[];skeleton_prefix=[]
    legacy_refill=record['candidate_policy'].get('selection_policy')=='model_skeleton_then_audio'
    for parent,pair in zip(revisions,pairs) if legacy_refill else []:
        skeleton=fuse_revisions(pair['vocals'],pair['accompaniment'],plan,parent['settings'],evidence=evidence,
            timing=parent.get('provenance',{}).get('timing_map'),preceding_events=skeleton_prefix,
            model_energy_evidence_id=energy['id'],
            selection_policy=record['candidate_policy'].get('selection_policy','ranked_beam'),
            audio_vote_cap=record['candidate_policy'].get('audio_vote_cap'),fill_unused_quota=False)
        skeletons.append(skeleton['events']);skeleton_prefix.extend(skeleton['events'])
    for index,(parent,pair) in enumerate(zip(revisions,pairs)):
        skeleton_constraints={'fixed_skeleton_events':skeletons[index],
            'following_events':[event for group in skeletons[index+1:] for event in group]} if legacy_refill else {}
        output=fuse_revisions(pair['vocals'],pair['accompaniment'],plan,parent['settings'],evidence=evidence,
                              timing=parent.get('provenance',{}).get('timing_map'),preceding_events=selected,
                              model_energy_evidence_id=energy['id'],
                              selection_policy=record['candidate_policy'].get('selection_policy','ranked_beam'),
                              audio_vote_cap=record['candidate_policy'].get('audio_vote_cap'),
                              **skeleton_constraints)
        fused.append(output)
        for event in output['events']:
            if event['id'] in owners:raise ValueError('原谱候选身份重复')
            owners[event['id']]=parent;selected.append(event)
    checked_plan={**plan,'region_boundaries':sorted({point for row in revisions for point in row['range']})}
    changed=apply(selected,settings,evidence,key,pattern,timing=revisions[0]['provenance'].get('timing_map'),plan=checked_plan)
    if len(changed['events'])!=len(selected):raise ValueError('质量修订不能改变音符头数量')
    parts={r['id']:[] for r in revisions}
    for event in changed['events']:
        owner=owners[event['id']]
        if not owner['range'][0]*1000/SR<=event['start_ms']<owner['range'][1]*1000/SR:
            raise ValueError('质量修订改变了区域归属')
        parts[owner['id']].append(event)
    duration=content_end(p)/SR;saved=[]
    for parent,output in zip(revisions,fused):
        stats=output['stats'];model=sum(r['candidate_model_heads'] for r in stats);acoustic=sum(r['candidate_acoustic_heads'] for r in stats)
        inputs={'model_heads':model,'candidate_heads':model+acoustic,'acoustic_candidate_heads':acoustic,
                'constraint_removed':sum(1 for d in output['decisions'] if d.get('reason_category')=='hard_constraint')}
        validation=evaluate(parts[parent['id']],plan,key,parent['range'],duration=duration,candidates=inputs)
        provenance={**output['provenance'],'section_plan_id':plan_id,'workflow_revision_of':parent['id'],
            'quality_policy':record['quality_policy'],'candidate_policy':record['candidate_policy'],
            'quality_evidence_id':evidence.get('id',evidence.get('cache_key')),
            'quality_summary':changed['summary'],'quality_decisions':changed['decisions'],
            'density_validation':validation,'density_policy':freeze(parent['settings'])['density_policy']}
        saved.append({'id':canonical_hash({'request':request_id,'parent':parent['id'],
            'quality_policy':record['quality_policy'],'candidate_policy':record['candidate_policy']})[:32],
            'segment_id':parent['segment_id'],'variant':parent['variant'],'range':parent['range'],
            'kind':'quality','settings':parent['settings'],'events':parts[parent['id']],'provenance':provenance})
    validation=assembly_density(saved,changed['events'],duration)
    with store.lock:
        # The single manifest commit leaves every old adoption intact.
        fresh=store.load(pid);store.check(fresh,body['expected_revision'])
        result={'request_id':request_id,'revisions':[{'id':r['id'],'revision_of':parent['id'],
            'segment_id':r['segment_id'],'variant':r['variant'],'density_validation':r['provenance']['density_validation']}
            for r,parent in zip(saved,revisions)],'project_revision':fresh['revision']+1,
            'density_validation':validation,'quality_summary':changed['summary']}
        record['pending']={'rows':saved,'result':result};atomic(path,record)
        store.add_candidate_batch(pid,saved,body['expected_revision'])
        record.pop('pending')
        record['result']=result;atomic(path,record)
    return result
