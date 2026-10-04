"""Advanced authoring API; existing song/job contracts remain separate."""
import copy
import json
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from fastapi import APIRouter, Body, File, Form, UploadFile, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool
import soundfile as sf
from .advanced import ROOT, SR, ProjectStore, assemble, defaults, variants, identifier, uid, now, atomic, read, chart_events, validate_settings, merge, as_notes, project_timing
from . import advanced_agent as agent

router=APIRouter(prefix='/api/advanced',tags=['advanced'])
store=ProjectStore()
review_pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='chart-review')
agent.restore_config()
for path in store.root.glob('*/reviews/*.json'):
    task=read(path)
    if task.get('status') in ('queued','running'):
        task.update(status='interrupted',message='服务重启，评审已中断；不会自动重发收费请求');atomic(path,task)

def error(exc):
    return HTTPException(409 if any(s in str(exc) for s in ('已更新','已变化','过期','尚未')) else 400,str(exc))

@router.get('/defaults')
def get_defaults():return {'settings':defaults(),'patterns':list(__import__('malody_studio.difficulty',fromlist=['PATTERN_CHOICES']).PATTERN_CHOICES),'difficulties':list(defaults()['difficulty_rules'])}

@router.get('/projects')
def projects():return {'projects':store.list()}

def import_charts(p,charts):
    pid=p['id'];store.add_segment(pid,{'start_sample':0,'end_sample':p['samples'],'name':'完整原曲'})
    p=store.load(pid);s=p['segments'][0];settings=p['settings'];found=[]
    for index,chart in enumerate(charts):
        label=str(chart.get('meta',{}).get('version','')).lower();pattern=next((x for x in ('chordjack','jumpstream','handstream','technical','stamina','jackspeed','speed','stream','balanced') if x in label),'balanced')
        key=next((x for x in ('lunatic','master','expert','hard','medium','easy') if x in label),list(defaults()['difficulty_rules'])[min(index,5)])
        variant=pattern+'--'+key
        if variant in [v['key'] for v in found]:continue
        events=[e for e in chart_events(chart) if 0<=e['start_ms']<p['duration']*1000]
        store.add_revision(pid,s['id'],variant,events,settings,'imported_final',{'raw_available':False,'source_version':chart.get('meta',{}).get('version','')})
        found.append({'key':variant,'pattern':pattern,'difficulty':key})
    if found:
        from .charts import beat_value
        references=[]
        for chart in charts:
            points=sorted(chart['time'],key=lambda row:float(beat_value(row['beat'])))
            elapsed=0.; previous=0.; bpm=points[0]['bpm']; times=[]
            bgm=next((e for e in chart['note'] if e.get('type')==1 and e.get('sound')), {})
            offset=-bgm.get('offset',0)
            for row in points:
                beat=float(beat_value(row['beat']));elapsed+=(beat-previous)*60000/bpm
                bpm=row['bpm'];previous=beat;position=max(0.,elapsed+offset)
                if times and abs(position-times[-1][0])<.000001:times[-1]=[position,bpm]
                else:times.append([position,bpm])
            if times and times[0][0]>0:times.insert(0,[0.,times[0][1]])
            references.append({'source':'imported_chart','version':chart.get('meta',{}).get('version',''),'points':times})
        with store.lock:
            p=store.load(pid);p['variants']=found;p['tempo']['references']=references
            conflict=any(row['points']!=references[0]['points'] for row in references[1:])
            p['tempo']['reference_conflict']=conflict
            if not conflict:
                p['tempo'].update(points=references[0]['points'],bpm=references[0]['points'][0][1],reference_source='imported_chart',uncertain=True)
            store.save(p)
    return store.load(pid)

def job_audio_provenance(job,path):
    """Preserve recorded music identity without treating artwork as the source."""
    import hashlib
    from .artwork import video_id_from_url
    from .music import valid_id, url_for
    reference=job.get('source_ref') or {}; options=job.get('options') or {}
    video_id=None
    try:
        recorded=valid_id(reference.get('video_id')) if reference.get('type')=='youtube' else None
        linked=video_id_from_url(options.get('source',''))
        if recorded and linked and recorded!=linked:raise ValueError('音乐来源记录冲突')
        # An explicit upload is independent of any video chosen for its cover.
        if reference.get('type')!='upload':video_id=recorded or linked
    except (ValueError,TypeError,AttributeError):pass
    result={'kind':'imported_job_audio','direct_original':False,'filename':path.name,
            'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size,
            'source_job_id':job['id'],'source_identity':'youtube' if video_id else 'unknown'}
    if video_id:result.update(video_id=video_id,url=url_for(video_id))
    return result


def mcz_member_path(value,parent=''):
    """Resolve a logical archive member without ever extracting its path."""
    if not isinstance(value,str) or not value or any(ord(c)<32 for c in value):raise ValueError('曲包音源引用路径无效')
    value=value.replace('\\','/')
    if value.startswith('/') or ':' in value:raise ValueError('曲包音源引用不能使用绝对路径')
    parts=parent.split('/') if parent else []
    for part in value.split('/'):
        if part in ('','.'):continue
        if part=='..':
            if not parts:raise ValueError('曲包音源引用越过包内目录')
            parts.pop()
        else:parts.append(part)
    if not parts:raise ValueError('曲包音源引用路径无效')
    return '/'.join(parts)


def mcz_chart_audio(archive,entries):
    """Every imported difficulty must name the same unique BGM object."""
    members={};charts=[];targets=set()
    for entry in entries:
        if entry.is_dir():continue
        name=mcz_member_path(entry.filename)
        members.setdefault(name,[]).append(entry)
        if name.lower().endswith('.mc'):
            chart=json.loads(archive.read(entry).decode('utf-8-sig'))
            if not isinstance(chart,dict) or not isinstance(chart.get('note'),list) or any(not isinstance(row,dict) for row in chart['note']):raise ValueError('曲包 MC 谱面音符格式无效')
            bgm=[row for row in chart['note'] if row.get('type')==1 and 'sound' in row]
            if len(bgm)!=1:raise ValueError('每份 MC 谱面须明确引用一份背景音乐')
            parent=name.rsplit('/',1)[0] if '/' in name else ''
            targets.add(mcz_member_path(bgm[0]['sound'],parent))
            charts.append(chart)
    if not charts:raise ValueError('曲包中没有 MC 谱面')
    if len(targets)!=1:raise ValueError('曲包各谱面引用了不同背景音乐，请拆分为单曲包后导入')
    target=next(iter(targets));matches=members.get(target,[])
    if not matches:raise ValueError('谱面引用的背景音乐在曲包中不存在：'+target)
    if len(matches)!=1:raise ValueError('谱面引用的背景音乐在曲包中不唯一：'+target)
    audio=matches[0]
    if Path(target).suffix.lower() not in ('.ogg','.mp3','.wav','.m4a','.flac','.opus'):raise ValueError('谱面引用的背景音乐格式不支持')
    return charts,audio


def uploaded_project(path,title,artist):
    if path.suffix.lower()!='.mcz':return store.create(path,title or path.stem,artist)
    with zipfile.ZipFile(path) as z:
        entries=z.infolist()
        if sum(e.file_size for e in entries)>512*1024**2:raise ValueError('曲包解压数据超过 512 MB')
        charts,audio=mcz_chart_audio(z,entries)
        unpack=ROOT/'cache'/('advanced-import-'+uid());unpack.mkdir()
        audio_path=unpack/('audio'+Path(audio.filename.replace('\\','/')).suffix.lower());audio_path.write_bytes(z.read(audio))
        background=next((e for e in entries if e.filename.lower().endswith('.jpg')),None)
        bg=unpack/'background.jpg'
        if background:bg.write_bytes(z.read(background))
        meta=charts[0].get('meta',{}).get('song',{})
        p=store.create(audio_path,title or meta.get('title') or '导入曲包',artist or meta.get('artist',''),bg if background else None)
        return import_charts(p,charts)

@router.post('/projects/upload')
async def upload_project(file:UploadFile=File(...),title:str=Form(''),artist:str=Form('')):
    suffix=Path(file.filename or '').suffix.lower()
    if suffix not in ('.wav','.flac','.mp3','.m4a','.ogg','.opus','.mcz'):raise HTTPException(400,'请选择音乐或 MCZ 文件')
    path=ROOT/'cache'/('advanced-upload-'+uid()+suffix);size=0
    try:
        with path.open('wb') as out:
            while chunk:=await file.read(1024*1024):
                size+=len(chunk)
                if size>200*1024**2:raise HTTPException(413,'文件最大 200 MB')
                out.write(chunk)
        return await run_in_threadpool(uploaded_project,path,title,artist)
    except (ValueError,zipfile.BadZipFile) as exc:raise error(exc)
    finally:
        if path.exists():path.unlink()

@router.post('/projects/from-source')
def from_source(payload:dict=Body(...)):
    try:
        kind=payload.get('type')
        if kind=='job':
            from .server import get_job,_charts_from_disk
            job=get_job(payload['id'])
            if job.get('status')!='completed' or job.get('options',{}).get('_advanced'):raise ValueError('请选择已完成的普通曲包')
            directory=ROOT/'outputs'/identifier(job['id']);background=directory/'0'/'background.jpg'
            source=directory/'0'/'audio.ogg'
            p=store.create(source,job['title'],job['artist'],background if background.exists() else None)
            p['source_provenance']=job_audio_provenance(job,source)
            atomic(store.directory(p['id'])/'project.json',p)
            return import_charts(p,list(_charts_from_disk(job['id']).values()))
        if kind=='youtube':
            from .music import ready_original_audio
            from .artwork import prepare_artwork
            source,track=ready_original_audio(payload['id']);background,_=prepare_artwork(payload['id'])
            p=store.create(source,track.get('title','音乐'),track.get('artist') or track.get('channel',''),background)
            p['source_provenance']=copy.deepcopy(track['source_provenance'])
            atomic(store.directory(p['id'])/'project.json',p)
            return p
        raise ValueError('音乐来源无效')
    except (ValueError,KeyError) as exc:raise error(exc)

@router.get('/projects/{pid}')
def project(pid:str):
    try:return store.load(pid)
    except ValueError as exc:raise error(exc)

@router.patch('/projects/{pid}')
def update_project(pid:str,payload:dict=Body(...)):
    try:return store.update(pid,payload,payload.get('expected_revision'))
    except (ValueError,TypeError,KeyError) as exc:raise error(exc)

@router.get('/projects/{pid}/audio')
def source_audio(pid:str,request:Request):
    try:
        from .server import audio_response
        from .playback_audio import audition_file
        return audio_response(audition_file(store.directory(pid)/'source.wav',store.directory(pid)),request.headers.get('range'))
    except (ValueError,OSError) as exc:raise error(exc)

@router.get('/projects/{pid}/background')
def source_background(pid:str):
    try:
        path=store.directory(pid)/'background.jpg'
        if not path.is_file():raise HTTPException(404,'没有关联封面')
        return FileResponse(path,media_type='image/jpeg')
    except ValueError as exc:raise error(exc)

@router.post('/projects/{pid}/segments')
def add_segment(pid:str,payload:dict=Body(...)):
    try:
        from .advanced_history import perform
        return perform(store,pid,lambda:store.add_segment(pid,payload,payload.get('expected_revision')),'添加片段')
    except (ValueError,TypeError,KeyError) as exc:raise error(exc)

@router.patch('/projects/{pid}/segments/{sid}')
def edit_segment(pid:str,sid:str,payload:dict=Body(...)):
    try:
        from .advanced_history import perform
        return perform(store,pid,lambda:store.edit_segment(pid,sid,payload,payload.get('expected_revision')),'修改片段')
    except (ValueError,TypeError,KeyError) as exc:raise error(exc)

@router.delete('/projects/{pid}/segments/{sid}')
def delete_segment(pid:str,sid:str):
    try:
        from .advanced_history import perform
        def remove():
            p=store.load(pid);store.segment(p,sid);p['segments']=[s for s in p['segments'] if s['id']!=sid];return store.save(p)
        return perform(store,pid,remove,'移除片段')
    except ValueError as exc:raise error(exc)

@router.post('/projects/{pid}/segments/{sid}/split')
def split_segment(pid:str,sid:str,payload:dict=Body(...)):
    try:
        from .advanced_history import perform
        return perform(store,pid,lambda:store.split(pid,sid,payload['cuts'],payload.get('expected_revision')),'拆分片段')
    except (ValueError,TypeError,KeyError) as exc:raise error(exc)

@router.get('/projects/{pid}/edit-history')
def edit_history(pid:str):
    try:
        from .advanced_history import status
        return status(store,pid)
    except (ValueError,KeyError) as exc:raise error(exc)

@router.post('/projects/{pid}/undo')
def undo_segment_edit(pid:str,payload:dict=Body(default={})):
    try:
        from .advanced_history import undo
        if set(payload)-{'expected_revision'}:raise ValueError('撤销只使用服务器保存的记录，不接受自定义片段或版本')
        return undo(store,pid,payload.get('expected_revision'))
    except (ValueError,TypeError,KeyError) as exc:raise error(exc)

def prepare_generation(pid,sid,payload):
    from .server import ensure_engine
    with store.lock:
        p=store.load(pid);store.check(p,payload.get('expected_revision'));s=store.segment(p,sid)
        settings=validate_settings(merge(merge(p['settings'],payload.get('settings',{})),s['overrides']))
        explicit = 'source_id' in payload or 'stem_set_id' in payload
        descriptors = []
        if explicit:
            from .separation import resolve_source
            source_id = payload.get('source_id', 'original')
            if not isinstance(payload.get('auto_fuse', False), bool):raise ValueError('自动融合选项须为布尔值')
            if source_id == 'vocals_accompaniment':
                stem_id = str(payload.get('stem_set_id', ''))
                if len(stem_id) != 64 or any(c not in 'abcdef0123456789' for c in stem_id):raise ValueError('请选择有效分离版本')
                manifest_path = store.directory(pid)/'stems'/stem_id/'manifest.json'
                if not manifest_path.is_file():raise ValueError('所选分离版本不存在，请先完成分离并选择可用版本')
                from .separation import validate_manifest
                manifest = validate_manifest(manifest_path.parent,read(manifest_path))
                descriptors = [resolve_source(store,pid,row['source_id']) for row in manifest['stems']]
                if sorted(d['source_role'] for d in descriptors) != ['accompaniment','vocals']:raise ValueError('所选分离版本缺少可用人声或伴奏')
            else:
                descriptor = resolve_source(store,pid,str(source_id))
                if descriptor['source_role'] != 'mix':descriptors = [descriptor]
                else:original_descriptor = descriptor
            if descriptors and any(row['stem_set_id'] != payload.get('stem_set_id') for row in descriptors):raise ValueError('生成来源与所选分离版本不一致')
            settings['source_mode'] = 'mix'
        if not settings['fixed_seed']:settings['seed']=int(uid()[:8],16)%2147483640
        selection=payload.get('variants') or [v['key'] for v in p['variants']]
        chosen=[v for v in p['variants'] if v['key'] in selection]
        if not chosen or len(chosen)!=len(set(selection)):raise ValueError('请选择项目内的有效组合')
        snapshot={'project':{k:p[k] for k in ('id','title','artist','duration','samples','tempo','source_sha256','revision')},'segment':copy.deepcopy(s),'settings':settings,'variants':chosen}
        if explicit:
            snapshot['auto_fuse'] = payload.get('auto_fuse', False)
            if descriptors:
                snapshot.update(input_sources=descriptors,stem_set_id=payload['stem_set_id'])
            else:snapshot['source'] = original_descriptor
        snapshot['project'].update(profile=p.get('profile','phone'),source_pcm_sha256=p.get('source_pcm_sha256'))
        if settings.get('dynamic_enabled'):
            from .advanced_plans import get_or_build_plan
            plan=get_or_build_plan(store,p,settings)
            snapshot['section_plan']=plan
        options={'title':p['title']+' · '+s['name'],'artist':p['artist'],'engine':settings['engine'],'_advanced':snapshot}
        ensure_engine(options)
    return options

@router.post('/projects/{pid}/segments/{sid}/generate')
def generate_segment(pid:str,sid:str,payload:dict=Body(default={})):
    try:
        from .server import enqueue_job
        options=prepare_generation(pid,sid,payload)
        result=enqueue_job(options,{'type':'project_file','path':f'outputs/advanced/{pid}/source.wav'})
        return {**result,**generation_counts(options)}
    except (ValueError,TypeError,KeyError,OSError,RuntimeError) as exc:raise error(exc)


def generation_counts(options):
    snapshot=options['_advanced'];s=snapshot['segment'];settings=snapshot['settings'];chosen=snapshot['variants']
    count=sum(1 for row in snapshot.get('section_plan',{}).get('sections',[]) if row['core'][0]<s['end_sample'] and row['core'][1]>s['start_sample']) or 1
    roles=len(snapshot.get('input_sources',[])) or (2 if settings.get('source_mode')=='vocals_accompaniment' else 1)
    return {'inference_count':roles*count*(len(chosen) if settings['strategy']=='independent' else len(set(v['pattern'] for v in chosen))),
            'separation_count':1 if 'source_id' not in snapshot.get('source',{}) and settings.get('source_mode')=='vocals_accompaniment' else 0,
            'fusion_count':len(chosen) if snapshot.get('auto_fuse') and roles==2 else 0}


@router.post('/projects/{pid}/generation-batches')
def generation_batch(pid:str,payload:dict=Body(...)):
    try:
        from .server import enqueue_jobs,jobs,lock
        ids=payload.get('segment_ids')
        if not isinstance(ids,list) or not ids or len(ids)>100 or any(not isinstance(s,str) for s in ids) or len(set(ids))!=len(ids):raise ValueError('请选择 1–100 个不重复片段')
        identifier(payload['request_id'])
        with store.lock:
            p=store.load(pid);path=store.directory(pid)/'batches'/(payload['request_id']+'.json')
            if path.is_file():return read(path)
            with lock:
                accepted=[j for j in jobs.values() if j.get('options',{}).get('_advanced',{}).get('project',{}).get('id')==pid and j.get('options',{}).get('_advanced',{}).get('request_id')==payload['request_id']]
            if accepted:
                result={'id':accepted[0]['options']['_advanced']['batch_id'],'request_id':payload['request_id'],'created':accepted[0]['created'],'jobs':[{'id':j['id'],'segment_id':j['options']['_advanced']['segment']['id'],**generation_counts(j['options'])} for j in accepted]}
                atomic(path,result);return result
            store.check(p,payload.get('expected_revision'))
            bid=uid();entries=[];counts=[]
            for sid in sorted(ids,key=lambda sid:store.segment(p,sid)['start_sample']):
                options=prepare_generation(pid,sid,{**payload,'source_id':payload.get('source_id','original'),'auto_fuse':payload.get('source_id')=='vocals_accompaniment'})
                options['_advanced'].update(batch_id=bid,request_id=payload['request_id'],activate_initial=False)
                entries.append((options,{'type':'project_file','path':f'outputs/advanced/{pid}/source.wav'}));counts.append(generation_counts(options))
            accepted=enqueue_jobs(entries)
            result={'id':bid,'request_id':payload['request_id'],'created':now(),'jobs':[dict(job,segment_id=entry[0]['_advanced']['segment']['id'],**count) for job,entry,count in zip(accepted,entries,counts)]}
            atomic(path,result)
            return result
    except (ValueError,TypeError,KeyError,OSError,RuntimeError) as exc:raise error(exc)


@router.get('/projects/{pid}/section-plans')
def available_section_plans(pid:str):
    """Restore completed audio analysis without starting analysis on import."""
    try:
        from .section_plan import validate_plan, VERSION
        p=store.load(pid); plans=[]
        for path in sorted((store.directory(pid)/'section-plans').glob('*.json'),key=lambda x:x.stat().st_mtime,reverse=True):
            try:
                plan=read(path)
                if plan.get('fusion_only') or plan.get('version')!=VERSION or plan.get('samples')!=p['samples']:continue
                validate_plan(plan,p['source_pcm_sha256']);plans.append(plan)
            except (ValueError,KeyError,TypeError,OSError):continue
        return {'plans':plans}
    except (ValueError,FileNotFoundError) as exc:raise error(exc)

@router.post('/projects/{pid}/segmentation-preview')
def preview_segmentation(pid:str,payload:dict=Body(...)):
    try:
        from .advanced_workbench import segmentation_preview
        return segmentation_preview(store,pid,payload)
    except (ValueError,TypeError,KeyError,OSError) as exc:raise error(exc)


@router.post('/projects/{pid}/segmentation-apply')
def apply_segmentation(pid:str,payload:dict=Body(...)):
    try:
        from .advanced_workbench import segmentation_apply
        return segmentation_apply(store,pid,payload,project_tasks(pid)['tasks'])
    except (ValueError,TypeError,KeyError,OSError) as exc:raise error(exc)


@router.post('/projects/{pid}/select-many')
def batch_select(pid:str,payload:dict=Body(...)):
    try:
        from .advanced_workbench import select_many
        return select_many(store,pid,payload)
    except (ValueError,TypeError,KeyError,OSError) as exc:raise error(exc)


@router.get('/projects/{pid}/layouts')
def layouts(pid:str):
    try:
        store.load(pid)
        return {'layouts':sorted([read(path) for path in (store.directory(pid)/'layouts').glob('*.json')],key=lambda row:row['created'],reverse=True)}
    except (ValueError,OSError) as exc:raise error(exc)

@router.get('/projects/{pid}/generation-batches/{bid}')
def generation_batch_result(pid:str,bid:str):
    try:
        from .server import jobs,lock
        from .task_history import batch_detail
        with lock:records=copy.deepcopy(list(jobs.values()))
        return batch_detail(store,pid,bid,records)
    except (ValueError,TypeError,KeyError,OSError) as exc:raise error(exc)


def commit_generated(options,result,job_id=None):
    snapshot=options['_advanced'];pid=snapshot['project']['id'];sid=snapshot['segment']['id'];saved=[]
    for row in result['advanced_result']:
        # Cache ownership is immutable and independent of this submission's identity.
        provenance={**row['provenance'],'generation_batch_id':snapshot.get('batch_id') or job_id,
                    'generation_job_id':job_id or snapshot.get('generation_job_id')}
        saved.append(store.add_revision(pid,sid,row['variant'],row['events'],row['settings'],row['kind'],provenance,result['bounds'],activate_initial=snapshot.get('activate_initial',True) and row.get('activate_initial',True),revision_id=row.get('id'))['id'])
    return saved

@router.get('/projects/{pid}/tasks')
def project_tasks(pid:str):
    from .server import jobs,lock
    identifier(pid)
    with lock:
        tasks=[]
        for j in jobs.values():
            snapshot=j.get('options',{}).get('_advanced',{})
            if snapshot.get('project',{}).get('id')!=pid:continue
            row={k:v for k,v in j.items() if k!='options'}
            if snapshot.get('task_type','generation')=='generation':row['estimated_execution']=generation_counts(j['options'])
            row.update(batch_id=snapshot.get('batch_id'),task_type=snapshot.get('task_type','generation'),segment_id=snapshot.get('segment',{}).get('id'),
                       start_sample=snapshot.get('segment',{}).get('start_sample'),end_sample=snapshot.get('segment',{}).get('end_sample'),
                       source_id=snapshot.get('source',{}).get('source_id'),source_ids=[d['source_id'] for d in snapshot.get('input_sources',[])],
                       stem_set_id=j.get('stem_set_id') or snapshot.get('stem_set_id'),
                       variants=[v['key'] for v in snapshot.get('variants',[])])
            tasks.append(row)
        return {'tasks':tasks}

@router.get('/projects/{pid}/revisions/{rid}')
def revision(pid:str,rid:str):
    try:return store.local_chart(pid,rid)
    except (ValueError,FileNotFoundError) as exc:raise error(exc)

@router.get('/projects/{pid}/revisions/{rid}/audio')
def revision_audio(pid:str,rid:str,request:Request,source_id:str|None=None):
    try:
        from .workflow_api import source_path
        from .advanced import revision_audio_source
        from .playback_audio import audition_file, VERSION
        import hashlib
        r=store.revision(pid,rid)
        source_id=source_id if source_id is not None else revision_audio_source(r)
        if source_id.startswith('assembly:'):raise ValueError('片段试玩需使用原曲或对齐声部')
        source=audition_file(source_path(pid,source_id),store.directory(pid))
        key=hashlib.sha256((VERSION+':'+source_id).encode()).hexdigest()[:24]
        target=store.directory(pid)/'preview'/(rid+'-'+key+'.wav');target.parent.mkdir(exist_ok=True)
        with store.lock:
            if not target.exists():
                y,rate=sf.read(source,start=r['range'][0],stop=r['range'][1],dtype='float32',always_2d=True)
                if rate!=SR or len(y)!=r['range'][1]-r['range'][0]:raise ValueError('试听声部的采样时钟不匹配')
                temporary=target.with_name(rid+'.'+uid()+'.wav');sf.write(temporary,y,SR,subtype='FLOAT');temporary.replace(target)
        from .server import audio_response
        return audio_response(target,request.headers.get('range'))
    except (ValueError,FileNotFoundError) as exc:raise error(exc)

@router.post('/projects/{pid}/segments/{sid}/select')
def select_version(pid:str,sid:str,payload:dict=Body(...)):
    try:return store.select(pid,sid,payload['variant'],payload['revision_id'],payload.get('expected_revision'))
    except (ValueError,TypeError,KeyError) as exc:raise error(exc)

@router.post('/projects/{pid}/revisions/{rid}/feedback')
def feedback(pid:str,rid:str,payload:dict=Body(...)):
    try:return store.feedback(pid,rid,payload['status'],payload.get('reason',''))
    except (ValueError,KeyError) as exc:raise error(exc)

@router.post('/projects/{pid}/revisions/{rid}/rules')
def process_rules(pid:str,rid:str,payload:dict=Body(default={})):
    try:
        from .advanced_generation import rule_candidate
        p=store.load(pid);r=store.revision(pid,rid);s=store.segment(p,r['segment_id']);settings=validate_settings(merge(p['settings'],s['overrides']))
        return rule_candidate(store,pid,rid,settings)
    except (ValueError,KeyError) as exc:raise error(exc)

@router.post('/projects/{pid}/assemblies')
def create_assembly(pid:str,payload:dict=Body(default={})):
    try:return assemble(store,pid,payload.get('preroll',1.5),payload.get('expected_revision'))
    except (ValueError,KeyError) as exc:raise error(exc)

@router.get('/projects/{pid}/assemblies/{aid}')
def assembly_report(pid:str,aid:str):
    try:return read(store.directory(pid)/'assemblies'/identifier(aid)/'report.json')
    except (ValueError,FileNotFoundError) as exc:raise error(exc)

@router.get('/projects/{pid}/assemblies/{aid}/audio')
def assembled_audio(pid:str,aid:str,request:Request):
    try:
        from .server import audio_response
        return audio_response(store.directory(pid)/'assemblies'/identifier(aid)/'audio.ogg',request.headers.get('range'))
    except (ValueError,OSError) as exc:raise error(exc)

@router.get('/projects/{pid}/assemblies/{aid}/charts/{variant}')
def assembled_chart(pid:str,aid:str,variant:str):
    try:
        directory=store.directory(pid)/'assemblies'/identifier(aid);report=read(directory/'report.json');row=next((r for r in report['charts'] if r['key']==variant),None)
        if not row:raise ValueError('成品组合不存在')
        chart=next((read(path) for path in (directory/'0').glob('*.mc') if read(path)['meta']['version']==variant),None)
        # Version metadata includes the variant key, independently of the filesystem name.
        return {'chart':chart,'title':report['title'],'artist':store.load(pid)['artist'],'pattern':row['pattern'],'difficulty':row['difficulty'],
            'audio_url':f'/api/advanced/projects/{pid}/assemblies/{aid}/audio'}
    except (ValueError,FileNotFoundError) as exc:raise error(exc)

@router.get('/projects/{pid}/assemblies/{aid}/download')
def assembly_download(pid:str,aid:str):
    try:
        from .naming import safe_component
        from .advanced import assembly_archive
        p=store.load(pid);return FileResponse(assembly_archive(store,pid,aid),media_type='application/octet-stream',filename=safe_component(p['title']+'-剪辑版')+'.mcz')
    except (ValueError,FileNotFoundError) as exc:raise error(exc)

@router.get('/agent/config')
def agent_config():return agent.config_public()

@router.post('/agent/config')
def configure_agent(payload:dict=Body(...)):
    try:return agent.save_config(payload)
    except (ValueError,TypeError,KeyError) as exc:raise error(exc)

@router.post('/agent/probe/{role}')
def probe_agent(role:str):
    if role not in ('repair','audio'):raise HTTPException(400,'API 角色无效')
    try:return agent.probe(role)
    except (ValueError,TypeError,KeyError) as exc:raise error(exc)

@router.get('/projects/{pid}/features')
def project_features(pid:str):
    try:return agent.features(store,pid)
    except ValueError as exc:raise error(exc)

@router.get('/projects/{pid}/revisions/{rid}/alignment')
def alignment(pid:str,rid:str,start_ms:float,end_ms:float):
    try:
        r=store.revision(pid,rid);p=store.load(pid)
        if not 0<=start_ms<end_ms<=p['duration']*1000 or end_ms-start_ms>16000:raise ValueError('对齐图支持原曲内最多 16 秒范围')
        return Response(agent.alignment_png(store,pid,r,start_ms,end_ms),media_type='image/png')
    except ValueError as exc:raise error(exc)

@router.post('/projects/{pid}/reviews')
def create_review(pid:str,payload:dict=Body(...)):
    try:
        p=store.load(pid);r=store.revision(pid,payload['revision_id']);s=store.segment(p,r['segment_id']);a,b=(v*1000/SR for v in r['range'])
        if s['active'].get(r['variant'])!=r['id']:raise ValueError('请先使用这个版本，再进行 Agent 分析')
        start=payload.get('start_ms',a);end=payload.get('end_ms',min(b,start+12000))
        if any(isinstance(v,bool) or not isinstance(v,(float,int)) for v in (start,end)) or not a<=start<end<=b:raise ValueError('评审范围必须在当前片段中')
        c=agent.configured('repair')
        if not c.get('capabilities',{}).get('text'):raise ValueError('请先执行 Agent 连接检查')
        goal=str(payload.get('goal','保留音乐节奏与排键意图，修正有证据的局部问题'))[:2000]
        tid=uid();task={'id':tid,'base_revision':r['id'],'start_ms':start,'end_ms':end,'goal':goal,'send_audio':bool(payload.get('send_audio',False)),
            'status':'queued','created':now(),'message':'等待分析','decisions':[],'audio_ranges':[]}
        with store.lock:
            p=store.load(pid);p['reviews'].append({'id':tid,'base_revision':r['id'],'created':task['created']});store.save(p);atomic(store.directory(pid)/'reviews'/(tid+'.json'),task)
        review_pool.submit(agent.run_review,store,pid,tid);return task
    except (ValueError,TypeError,KeyError) as exc:raise error(exc)

@router.get('/projects/{pid}/reviews/{tid}')
def get_review(pid:str,tid:str):
    try:
        task=read(store.directory(pid)/'reviews'/(identifier(tid)+'.json'))
        if task.get('status')=='completed' and task.get('candidate_events'):
            from .mapperatorinator import serialize_with_timing
            p=store.load(pid);base=store.revision(pid,task['base_revision']);start,end=(v*1000/SR for v in base['range'])
            notes=as_notes(task['candidate_events'],start,end)
            task['candidate_chart']=serialize_with_timing(notes,p['title'],p['artist'],base['variant'],project_timing(p,start,end))
            task['audio_url']=f'/api/advanced/projects/{pid}/revisions/{base["id"]}/audio'
            task.update(range=base['range'],timeline_id=p['source_sha256'],base_revision_id=base['id'])
        return task
    except (ValueError,FileNotFoundError) as exc:raise error(exc)

@router.post('/projects/{pid}/reviews/{tid}/apply')
def apply_review(pid:str,tid:str,payload:dict=Body(...)):
    try:
        if not isinstance(payload.get('activate',True),bool):raise ValueError('采用选项须为布尔值')
        return agent.apply_review(store,pid,identifier(tid),payload['groups'],activate=payload.get('activate',True))
    except (ValueError,TypeError,KeyError) as exc:raise error(exc)

@router.post('/projects/{pid}/examples')
def add_example(pid:str,payload:dict=Body(...)):
    try:
        with store.lock:
            p=store.load(pid);r=store.revision(pid,payload['revision_id']);s=store.segment(p,r['segment_id'])
            v=next(x for x in s['versions'][r['variant']] if x['id']==r['id'])
            if v['review']['status']!='usable':raise ValueError('请先将这个版本评为可用，再加入示例库')
            if r['id'] not in p['examples']:p['examples'].append(r['id'])
            return store.save(p)
    except (ValueError,KeyError,StopIteration) as exc:raise error(exc)

@router.post('/projects/{pid}/evaluations')
def record_evaluation(pid:str,payload:dict=Body(...)):
    try:
        before=store.revision(pid,payload['before']);after=store.revision(pid,payload['after']);p=store.load(pid)
        if before['variant']!=after['variant'] or before['range']!=after['range']:raise ValueError('A/B 必须是同范围、同排键和难度的两个版本')
        if payload['preference'] not in ('before','after','equal'):raise ValueError('请选择更喜欢的版本或相当')
        row={'created':now(),'profile':p['profile'],'before':before['id'],'after':after['id'],'variant':before['variant'],'preference':payload['preference'],'reason':str(payload.get('reason',''))[:2000]}
        with store.lock:
            path=store.directory(pid)/'evaluations.json';rows=read(path) if path.exists() else [];rows.append(row);atomic(path,rows)
        return {'count':len(rows),'target':20,'evaluation':row}
    except (ValueError,KeyError) as exc:raise error(exc)

from .workflow_api import router as workflow_router
router.include_router(workflow_router)
