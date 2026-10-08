"""Asynchronous music evidence and a server-owned confirm-to-generate contract."""
import copy
from fastapi import APIRouter, Body
from .advanced import atomic, read, uid, now, identifier, validate_settings, merge, variants
from . import music_timing

router = APIRouter(tags=['music-workflow'])

def artifact_id(value):
    if not isinstance(value,str) or len(value)!=64 or any(c not in 'abcdef0123456789' for c in value):
        raise ValueError('音乐证据标识无效')
    return value


def api():
    from . import advanced_api
    return advanced_api


def source_options(store, pid, payload):
    from .separation import resolve_source, validate_manifest
    source = payload.get('source_id', 'original')
    result = {'source_id': source}
    if source == 'vocals_accompaniment':
        stem = payload.get('stem_set_id', '')
        if not stem:return {**result,'auto_prepare_stems':True}
        if not isinstance(stem,str) or len(stem)!=64 or any(c not in 'abcdef0123456789' for c in stem):
            raise ValueError('请选择有效的声部分离版本')
        directory = store.directory(pid)/'stems'/stem
        manifest = validate_manifest(directory, read(directory/'manifest.json'))
        if sorted(row['role'] for row in manifest['stems']) != ['accompaniment','vocals']:
            raise ValueError('声部分离版本不完整')
        project = store.load(pid)
        if manifest.get('source_pcm_sha') != project['source_pcm_sha256'] or manifest.get('frame_count') != project['samples']:
            raise ValueError('声部分离版本与当前完整原曲不符')
        result['stem_set_id'] = stem
    else:
        descriptor = resolve_source(store,pid,source)
        if descriptor.get('stem_set_id'):
            if payload.get('stem_set_id') != descriptor['stem_set_id']:
                raise ValueError('声部输入与分离版本不一致')
            result['stem_set_id'] = descriptor['stem_set_id']
    return result


def run_analysis(source, directory, options, progress):
    """Queue worker stage: prepare sources and freeze evidence before confirmation."""
    from .workflow_log import stage, event, file_identity
    from pathlib import Path
    from .advanced import ProjectStore
    from .paths import ROOT
    state = options['_advanced']
    if state.get('analysis_version',music_timing.VERSION)!=music_timing.VERSION:
        raise ValueError('音乐分析版本已改变，请重新分析')
    project = state['project']
    store = ProjectStore(ROOT/'outputs/advanced')
    project_directory = store.directory(project['id'])
    if Path(source).resolve() != (project_directory/'source.wav').resolve():
        raise ValueError('音乐分析须使用完整项目原曲')
    aid = identifier(state['analysis_id'])
    path = project_directory/'music-analysis'/(aid+'.json')
    task = read(path)
    def update(message, value):
        task.update(status='running', message=message, progress=value)
        atomic(path, task)
        progress(message, value)
    try:
        with stage('music_analysis.verify_source_contract', project=project,
                   source=file_identity(project_directory/'source.wav',hash_file=True)):
            music_timing.source_contract(project_directory, project)
        chosen = copy.deepcopy(state['source_choice'])
        if chosen.get('auto_prepare_stems'):
            from .separation_tasks import run as prepare_stems
            update('正在准备人声与伴奏', 0)
            with stage('music_analysis.prepare_source_stems', settings=state['separation_settings']):
                prepared = prepare_stems(source, directory, {'_advanced': {
                    'task_type': 'separation', 'project': project,
                    'settings': state['separation_settings']}},
                    lambda message, value: update(message, value*.55))
            chosen = {'source_id': 'vocals_accompaniment', 'stem_set_id': prepared['stem_set_id']}
        with stage('music_analysis.resolve_source_choice', source_choice=chosen):
            chosen = source_options(store, project['id'], chosen)
        if chosen.get('auto_prepare_stems'):
            raise ValueError('音乐输入尚未准备完成')
        task['resolved_source'] = chosen
        update('正在分析音乐节奏', 55 if state['source_choice'].get('auto_prepare_stems') else 0)
        with stage('music_analysis.extract_and_cache_evidence', policy=state.get('analysis_policy'),
                   project_id=project['id'], source_sha256=project.get('source_pcm_sha256')):
            evidence=music_timing.load_or_analyze(project_directory, project,
                lambda message, value: update(message, 55 + value*.45 if state['source_choice'].get('auto_prepare_stems') else value),
                state.get('analysis_policy'))
        with stage('music_analysis.select_timing_map', evidence_id=evidence.get('id'),
                   candidates=[row.get('id') for row in evidence.get('candidates',[])]):
            timing=music_timing.select_timing(evidence)
        with stage('music_analysis.persist_evidence', evidence_id=evidence['id'], timing_map_id=timing['id']):
            atomic(project_directory/'music-evidence'/(evidence['id']+'.json'),evidence)
            atomic(project_directory/'timing-maps'/(timing['id']+'.json'),timing)
        event('analysis_result','music_analysis.complete',status='ambiguous' if timing['uncertain'] else 'ready',
              evidence=evidence,timing_map=timing,resolved_source=chosen)
        task.update(status='ambiguous' if timing['uncertain'] else 'ready',evidence_id=evidence['id'],timing_map_id=timing['id'],progress=100)
        return {key:copy.deepcopy(task[key]) for key in ('analysis_id', 'status', 'evidence_id', 'timing_map_id', 'resolved_source')}
    except Exception as exc:
        task.update(status='failed',error=str(exc),message='音乐分析失败，可以重试')
        raise
    finally:
        atomic(path,task)


@router.post('/projects/{pid}/music-analysis')
def start_analysis(pid:str,payload:dict=Body(...)):
    a=api();store=a.store
    try:
        with store.lock:
            project=store.load(pid);store.check(project,payload.get('expected_revision'))
            source=source_options(store,pid,payload)
            from .separation import resolve_source, validated_settings, deployment
            resolve_source(store,pid,'original')
            separation = validated_settings(payload.get('separation_settings', {'model':project['settings'].get('separation_preset','htdemucs')})) if source.get('auto_prepare_stems') else None
            if separation:deployment(separation['model'])
            policy=music_timing.selected_policy()
            identity=music_timing.digest({'source_pcm_sha256':project['source_pcm_sha256'],'source':source,'separation':separation,'policy':policy,'version':music_timing.VERSION})
            folder=store.directory(pid)/'music-analysis'
            from .server import jobs,lock,enqueue_job
            for path in folder.glob('*.json'):
                old=read(path)
                with lock:
                    job=next((row for row in reversed(list(jobs.values()))
                        if row.get('options',{}).get('_advanced',{}).get('analysis_id')==old['id']),jobs.get(old.get('task_id'),{}))
                if old.get('identity')==identity and (old['status'] in ('ready','ambiguous') or job.get('status') in ('queued','running')):return old
            tid=uid();path=folder/(tid+'.json')
            task={'id':tid,'analysis_id':tid,'status':'queued','created':now(),'identity':identity,'source':source,'message':'等待音乐准备与分析'}
            snapshot = {'task_type':'music_analysis', 'analysis_id':tid, 'source_choice':source,
                        'project':{key:copy.deepcopy(project[key]) for key in ('id','title','artist','samples','sample_rate','duration','source_sha256','source_pcm_sha256','revision','tail_trim')},
                        'settings':copy.deepcopy(project['settings']), 'separation_settings':separation,
                        'analysis_policy':copy.deepcopy(policy),'analysis_version':music_timing.VERSION}
            atomic(path,task)
            try:
                job=enqueue_job({'title':project['title']+' · 音乐分析','artist':project['artist'],'_advanced':snapshot},
                                {'type':'project_file','path':f'outputs/advanced/{pid}/source.wav'})
            except Exception:
                path.unlink(missing_ok=True)
                raise
            task['task_id']=job['id']
            # A fast cached worker may have completed before enqueue returns.
            latest=read(path);latest['task_id']=job['id'];atomic(path,latest);task=latest
            return task
    except (ValueError,KeyError,TypeError,OSError,RuntimeError) as exc:raise a.error(exc)


@router.get('/projects/{pid}/music-analysis/{aid}')
def analysis_status(pid:str,aid:str):
    a=api();store=a.store
    try:
        store.load(pid);task=read(store.directory(pid)/'music-analysis'/(identifier(aid)+'.json'))
        from .server import jobs,lock
        with lock:
            job=copy.deepcopy(next((row for row in reversed(list(jobs.values()))
                if row.get('options',{}).get('_advanced',{}).get('analysis_id')==aid),jobs.get(task.get('task_id'),{})))
        if job:task['task_id']=job['id']
        if task['status'] not in ('ready','ambiguous'):
            status=job.get('status','interrupted')
            task.update(status=status,message=job.get('message',task.get('message')),progress=job.get('progress',task.get('progress',0)))
            if status in ('failed','interrupted','cancelled','paused'):
                task['error']=job.get('error') or '音乐准备或分析已暂停，可以重新分析'
        if task['status'] in ('ready','ambiguous'):
            task['evidence']=music_timing.validate_evidence(read(store.directory(pid)/'music-evidence'/(artifact_id(task['evidence_id'])+'.json')))
            task['timing_map']=music_timing.validate_timing(read(store.directory(pid)/'timing-maps'/(artifact_id(task['timing_map_id'])+'.json')),task['evidence']['source'])
            source_options(store,pid,task.get('resolved_source',task['source']))
        return task
    except (ValueError,KeyError,TypeError,OSError) as exc:raise a.error(exc)


def preview(store,pid,payload):
    from .advanced_plans import get_or_build_plan
    from .advanced_workbench import segmentation_preview
    with store.lock:
        project=store.load(pid);store.check(project,payload.get('expected_revision'))
        evidence=music_timing.validate_evidence(read(store.directory(pid)/'music-evidence'/(artifact_id(payload['evidence_id'])+'.json')))
        timing=music_timing.validate_timing(read(store.directory(pid)/'timing-maps'/(artifact_id(payload['timing_map_id'])+'.json')),evidence['source'])
        if evidence['source']['pcm_sha256'] != project['source_pcm_sha256'] or timing['id'] not in [row['id'] for row in evidence['candidates']]:
            raise ValueError('音乐证据不是当前原曲的结果')
        settings=validate_settings(merge(project['settings'],payload.get('settings',{})))
        chosen=variants(payload.get('patterns',list(dict.fromkeys(row['pattern'] for row in project['variants']))),payload.get('difficulties',list(dict.fromkeys(row['difficulty'] for row in project['variants']))))
        if not chosen:raise ValueError('至少选择一个目标组合')
        source=source_options(store,pid,payload);profile=payload.get('profile',project['profile'])
        if source.get('auto_prepare_stems'):raise ValueError('人声与伴奏尚未准备完成，请等待音乐分析结束')
        settings['source_mode']='vocals_accompaniment' if source['source_id']=='vocals_accompaniment' else 'mix'
        if profile not in ('keyboard','phone'):raise ValueError('目标玩法无效')
        from . import arrangement as arrangements, acoustic_evidence
        acoustic=evidence.get('acoustic') or acoustic_evidence.load_or_analyze(store.directory(pid),project)
        from .bpm_buckets import VERSION as bucket_version,summarize_range
        # This is a new analysis: it uses the current estimator and freezes its version.
        arrangement=arrangements.build(evidence,timing,acoustic,settings,chosen,profile,source['source_id'],source.get('stem_set_id'),bucket_version=bucket_version)
        region_policy=arrangement.get('region_policy',{})
        plan=arrangement['section_plan']
        atomic(store.directory(pid)/'section-plans'/(plan['id']+'.json'),plan)
        draft=segmentation_preview(store,pid,{**payload,'plan_id':plan['id']})
        # Rebuild only the derived plan around the preserved user layout.
        regions=[]
        requested=payload.get('regions',[])
        for index,row in enumerate(draft['segments']):
            override=requested[index] if index<len(requested) else {}
            regions.append({'start_sample':row['start_sample'],'end_sample':row['end_sample'],
                            'included':row['included'],**{k:override[k] for k in ('intensity','factor') if k in override}})
        # Existing layouts may deliberately omit gaps; section evidence still
        # covers the original clock, while regions keep user ownership.
        arrangement=arrangements.build(evidence,timing,acoustic,settings,chosen,profile,source['source_id'],source.get('stem_set_id'),regions,bucket_version=bucket_version)
        regions=arrangement['regions']
        plan=arrangement['section_plan']
        atomic(store.directory(pid)/'section-plans'/(plan['id']+'.json'),plan)
        atomic(store.directory(pid)/'arrangement-plans'/(arrangement['id']+'.json'),arrangement)
        policy=plan.get('bpm_buckets') or {}
        # Server-side facts for each user region, so the UI never pairs by index or guesses.
        segment_tempo=[{'start_sample':row['start_sample'],'end_sample':row['end_sample'],
                        **summarize_range(policy,row['start_sample'],row['end_sample'])} for row in draft['segments']] if policy.get('segments') else []
        draft.update(arrangement_plan_id=arrangement['id'],evidence_id=evidence['id'],timing_map_id=timing['id'],regions=regions,
                     segment_tempo=segment_tempo,bpm_bucket_version=policy.get('version'),
                     region_policy={**region_policy,'automatic_boundaries':'cuts' not in payload,
                                    'preserved_boundary_count':len(draft['preserved_boundaries'])},
                     options={'settings':settings,'variants':[row['key'] for row in chosen],'profile':profile,**source})
        draft['request_hash']=music_timing.digest({k:v for k,v in draft.items() if k not in ('request_hash','created')})
        atomic(store.directory(pid)/'segmentation-drafts'/(draft['id']+'.json'),draft)
        return draft


@router.post('/projects/{pid}/confirm-and-generate')
def confirm(pid:str,payload:dict=Body(...)):
    a=api();store=a.store
    reservation=None;modified=False;record=None
    try:
        allowed={'request_id','expected_revision','draft_id','arrangement_plan_id'}
        if set(payload)-allowed:raise ValueError('确认仅接受服务器保存的方案标识')
        rid=identifier(payload['request_id']);request_hash=music_timing.digest(payload)
        path=store.directory(pid)/'workflow-requests'/(rid+'.json')
        with store.lock:
            record=read(path) if path.is_file() else None
            if record and record['request_hash']!=request_hash:raise ValueError('同一请求 ID 的确认内容已变化')
            if record and record.get('result'):return record['result']
            project=store.load(pid)
            draft=read(store.directory(pid)/'segmentation-drafts'/(identifier(payload['draft_id'])+'.json'))
            if draft.get('arrangement_plan_id')!=payload['arrangement_plan_id']:raise ValueError('编排方案已变化，请重新确认')
            if draft.get('request_hash')!=music_timing.digest({k:v for k,v in draft.items() if k not in ('request_hash','created')}):raise ValueError('已保存的方案校验失败，请重新分析')
            snapshot={key:read(store.directory(pid)/folder/(artifact_id(draft[field])+'.json')) for key,folder,field in [('evidence','music-evidence','evidence_id'),('timing_map','timing-maps','timing_map_id'),('arrangement_plan','arrangement-plans','arrangement_plan_id')]}
            evidence=music_timing.validate_evidence(snapshot['evidence']);timing=music_timing.validate_timing(snapshot['timing_map'],evidence['source']);arrangement=snapshot['arrangement_plan']
            if arrangement.get('id')!=music_timing.digest({k:v for k,v in arrangement.items() if k!='id'}):raise ValueError('编排方案封印校验失败')
            if evidence['source']['pcm_sha256']!=project['source_pcm_sha256'] or evidence['source']['samples']!=project['samples'] or evidence['source']['sample_rate']!=project['sample_rate']:raise ValueError('音乐证据与当前原曲身份不符')
            if timing['id'] not in [row['id'] for row in evidence['candidates']] or arrangement['evidence_id']!=evidence['id'] or arrangement['timing_map_id']!=timing['id']:raise ValueError('编排与音乐证据引用不符')
            if any(not 0<=row['start_sample']<row['end_sample']<=project['samples'] for row in draft['segments']):raise ValueError('方案区域越过原曲范围')
            source_options(store,pid,draft['options'])
            from .server import reserve_queue_capacity,release_queue_capacity
            reserve_queue_capacity(rid,len([row for row in draft['segments'] if row['included']]));reservation=rid
            if record is None:
                store.check(project,payload.get('expected_revision'));store.check(project,draft['project_revision'])
                if not any(row['included'] for row in draft['segments']):raise ValueError('请至少保留一个音乐区域')
                source_options(store,pid,draft['options'])
                from .server import ensure_engine
                ensure_engine({'engine':draft['options']['settings']['engine']})
                history_path=store.directory(pid)/'segment-edits.json'
                record={'request_hash':request_hash,'phase':'prepared','before':copy.deepcopy(project),'before_history':read(history_path) if history_path.is_file() else None,'draft_id':draft['id']}
                atomic(path,record)
            if record['phase']=='prepared':
                before=record['before']
                # Recover an interrupted apply only when this exact layout and
                # revision prove no intervening user edit occurred.
                layout=lambda rows:[[row['start_sample'],row['end_sample'],row['included']] for row in rows]
                already_saved=(project['revision']==before['revision']+2 and project.get('workflow',{}).get('draft_id')==draft['id'] and layout(project['segments'])==layout(draft['segments']))
                if already_saved:
                    record.update(phase='applied',project_revision=project['revision']);atomic(path,record)
                elif project['revision']==before['revision']:
                    from .advanced_workbench import segmentation_apply
                    project=segmentation_apply(store,pid,{'draft_id':draft['id'],'expected_revision':before['revision']},a.project_tasks(pid)['tasks'])
                    modified=True
                elif project['revision']!=before['revision']+1 or layout(project['segments'])!=layout(draft['segments']):
                    raise ValueError('项目已更新，请重新预览方案')
                if not already_saved:
                    opts=draft['options'];project['settings']=opts['settings'];project['profile']=opts['profile']
                    arrangement=read(store.directory(pid)/'arrangement-plans'/(draft['arrangement_plan_id']+'.json'))
                    project['variants']=arrangement['variants'];project['workflow']={**{k:opts[k] for k in ('source_id','stem_set_id') if k in opts},'arrangement_plan_id':arrangement['id'],'draft_id':draft['id']}
                    project=store.save(project);record.update(phase='applied',project_revision=project['revision']);atomic(path,record)
            elif project['revision']!=record['project_revision']:
                # Accepted jobs are recoverable even after their results update
                # the project; generation_batch owns request-to-job identity.
                batch_path=store.directory(pid)/'batches'/(rid+'.json')
                from .server import jobs,lock
                with lock:accepted=any(job.get('options',{}).get('_advanced',{}).get('request_id')==rid and job.get('options',{}).get('_advanced',{}).get('project',{}).get('id')==pid for job in jobs.values())
                if not batch_path.is_file() and not accepted:raise ValueError('项目已更新，请重新确认方案')
            opts=draft['options']
            batch=a.generation_batch(pid,{**opts,**snapshot,'request_id':rid,'expected_revision':record['project_revision'],'segment_ids':[row['id'] for row in project['segments'] if row['included']],'initial_plan_id':draft['arrangement_plan_id'],'_queue_reservation':rid})
            result={'project':store.load(pid),'batch':batch};record.update(phase='accepted',result=result);atomic(path,record)
            return result
    except Exception as exc:
        if modified and record:
            from .server import jobs,lock
            with lock:accepted=any(job.get('options',{}).get('_advanced',{}).get('request_id')==rid and job.get('options',{}).get('_advanced',{}).get('project',{}).get('id')==pid for job in jobs.values())
            if not accepted:
                atomic(store.directory(pid)/'project.json',record['before'])
                history_path=store.directory(pid)/'segment-edits.json'
                if record.get('before_history') is None:history_path.unlink(missing_ok=True)
                else:atomic(history_path,record['before_history'])
                path.unlink(missing_ok=True)
        if isinstance(exc,(ValueError,KeyError,TypeError,OSError,RuntimeError)):raise a.error(exc)
        raise
    finally:
        if reservation:
            from .server import release_queue_capacity
            release_queue_capacity(reservation)
