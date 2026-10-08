"""Source-bound analysis and explicitly selected fusion candidates."""
from pathlib import Path
import threading
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse, Response
from .advanced import identifier, read, atomic, validate_settings, merge, SR

router = APIRouter()
_spectra = {}
_guard = threading.Lock()

def _store():
    from .advanced_api import store
    return store

def source_path(pid, source_id='original'):
    store = _store(); directory = store.directory(pid); store.load(pid)
    def local(path):
        if not path.resolve().is_relative_to(directory.resolve()):raise ValueError('音源路径越过项目目录')
        if not path.is_file():raise ValueError('音频来源不存在，请先完成分离或拼接')
        return path
    if source_id == 'original':return local(directory/'source.wav')
    if source_id.startswith('assembly:'):
        return local(directory/'assemblies'/identifier(source_id.split(':',1)[1])/'assembled.wav')
    for manifest in (directory/'stems').glob('**/manifest.json'):
        data = read(manifest)
        for stem in data.get('stems',[]):
            if stem.get('source_id') == source_id:
                if not isinstance(stem.get('path'),str):raise ValueError('声部音源路径缺失，请重新分离')
                path = Path(stem['path']).resolve()
                if not path.is_relative_to(directory.resolve()):raise ValueError('声部路径越过项目目录')
                return local(path)
    raise ValueError('音频来源不存在，请先完成分离或拼接')

def spectrum_service(pid):
    from .spectrum import SpectrumService
    directory = _store().directory(pid)
    with _guard:
        if pid not in _spectra:_spectra[pid] = SpectrumService(directory)
        return _spectra[pid]

def failure(exc):return HTTPException(400, str(exc))

@router.post('/projects/{pid}/analysis')
def analyze_source(pid:str, payload:dict=Body(default={})):
    try:
        source_id = str(payload.get('source_id','original'))
        return spectrum_service(pid).request(source_id,source_path(pid,source_id),payload.get('start_ms',0),payload.get('end_ms',5000))
    except (ValueError,OSError) as exc:raise failure(exc)

@router.get('/projects/{pid}/analysis/{analysis_id}')
def analysis_manifest(pid:str,analysis_id:str,start_ms:float|None=None,end_ms:float|None=None):
    try:return spectrum_service(pid).manifest(analysis_id,start_ms,end_ms)
    except (ValueError,OSError) as exc:raise failure(exc)

@router.get('/projects/{pid}/analysis/{analysis_id}/tiles/{level}/{index}')
def analysis_tile(pid:str,analysis_id:str,level:int,index:int):
    try:
        path = spectrum_service(pid).tile(analysis_id,level,index)
        return FileResponse(path,media_type='application/octet-stream') if path else Response(status_code=202,headers={'Retry-After':'1'})
    except (ValueError,OSError) as exc:raise failure(exc)

@router.get('/projects/{pid}/sources/{source_id}/audio')
def source_audio(pid:str,source_id:str,request:Request):
    try:
        from .server import audio_response
        from .playback_audio import audition_file
        return audio_response(audition_file(source_path(pid,source_id),_store().directory(pid)),request.headers.get('range'))
    except (ValueError,OSError) as exc:raise failure(exc)

@router.get('/projects/{pid}/stems')
def stems(pid:str):
    directory = _store().directory(pid);_store().load(pid)
    from .separation_tasks import present
    return {'stem_sets':[present(read(path)) for path in (directory/'stems').glob('**/manifest.json')]}

@router.get('/projects/{pid}/audition-metadata')
def audition_metadata(pid:str,source_id:str='original'):
    try:
        from .playback_audio import audition_metadata as metadata
        return metadata(source_path(pid,source_id),_store().directory(pid))
    except (ValueError,OSError) as exc:raise failure(exc)

@router.post('/projects/{pid}/segments/{sid}/fuse')
def fuse(pid:str,sid:str,payload:dict=Body(...)):
    try:
        from .stem_generation import fuse_selected
        return fuse_selected(_store(),pid,sid,payload)
    except (ValueError,OSError,KeyError) as exc:raise failure(exc)

@router.post('/projects/{pid}/section-plans')
def section_plan(pid:str,payload:dict=Body(default={})):
    try:
        from .advanced_plans import get_or_build_plan
        store = _store();p = store.load(pid)
        settings = validate_settings(merge(p['settings'],payload.get('settings',{})))
        plan = get_or_build_plan(store,p,settings)
        return plan
    except (ValueError,OSError,KeyError,RuntimeError) as exc:raise failure(exc)

@router.get('/projects/{pid}/section-plans/{plan_id}')
def get_plan(pid:str,plan_id:str):
    try:
        if len(plan_id) not in (32,64) or any(c not in 'abcdef0123456789' for c in plan_id):raise ValueError('计划 ID 无效')
        return read(_store().directory(pid)/'section-plans'/(plan_id+'.json'))
    except (ValueError,OSError) as exc:raise failure(exc)


@router.post('/projects/{pid}/separations')
def separate(pid:str,payload:dict=Body(default={})):
    try:
        from .server import enqueue_job
        from .separation import validated_settings, deployment
        store = _store()
        with store.lock:
            p = store.load(pid); store.check(p,payload.get('expected_revision'))
            settings = validated_settings(payload.get('settings', {}))
            snapshot = {'task_type':'separation','project':{k:p[k] for k in ('id','samples','source_pcm_sha256','revision')},'settings':settings}
        try:
            deployment(settings['model'])
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            raise HTTPException(409, str(exc))
        result = enqueue_job({'title':p['title']+' · 音频分离','artist':p['artist'],'_advanced':snapshot},
                             {'type':'project_file','path':f'outputs/advanced/{pid}/source.wav'})
        return {**result,'task_type':'separation'}
    except (ValueError,OSError,KeyError,TypeError) as exc:raise failure(exc)


@router.get('/separation/models')
def separation_models():
    from .separation import get_separation_models
    return {'models': list(get_separation_models(include_status=True).values()),
            'defaults': {'fast': 'htdemucs', 'quality': 'melband_roformer_kim'}}


@router.post('/projects/{pid}/separation-trials')
def separation_trial(pid:str,payload:dict=Body(...)):
    try:
        from .server import enqueue_job
        from .separation import deployment, resolve_source
        from .separation_trials import snapshot
        store = _store()
        with store.lock:
            p = store.load(pid); store.check(p,payload.get('expected_revision'))
            state = snapshot(p,payload['start_sample'],payload['end_sample'],payload.get('settings',{}))
            # Read and verify the full source before freezing a queue request.
            resolve_source(store,pid,'original')
        try:
            deployment(state['settings']['model'])
        except (OSError,ValueError,KeyError,RuntimeError) as exc:
            raise HTTPException(409,str(exc))
        result = enqueue_job({'title':p['title']+' · 局部试分离','artist':p['artist'],'_advanced':state},
                             {'type':'project_file','path':f'outputs/advanced/{pid}/source.wav'})
        return {**result,'task_type':'separation_trial','trial_id':state['trial_id'],
                'target_range':state['target_range'],'context_range':state['context_range']}
    except (ValueError,OSError,KeyError,TypeError) as exc:raise failure(exc)


def _trial_tasks(pid):
    from .server import jobs,lock
    with lock:
        result=[]
        for job in jobs.values():
            state=job.get('options',{}).get('_advanced',{})
            if state.get('project',{}).get('id') != pid or state.get('task_type') != 'separation_trial':continue
            result.append({**{k:v for k,v in job.items() if k!='options'},'task_type':'separation_trial',
                           'trial_id':state['trial_id'],'target_range':state['target_range'],'context_range':state['context_range']})
        return result


@router.get('/projects/{pid}/separation-trials')
def separation_trials(pid:str):
    try:
        from .separation_trials import load,present
        store=_store();p=store.load(pid);folder=store.directory(pid)/'separation-trials'
        saved=[present(load(store.directory(pid),path.parent.name,p)) for path in folder.glob('*/manifest.json')]
        return {'trials':sorted(saved,key=lambda row:row['created'],reverse=True),'tasks':_trial_tasks(pid)}
    except (ValueError,OSError,KeyError,TypeError) as exc:raise failure(exc)


@router.get('/projects/{pid}/separation-trials/{trial_id}')
def get_separation_trial(pid:str,trial_id:str):
    try:
        from .separation_trials import load,present
        store=_store();p=store.load(pid);identifier(trial_id)
        if (store.directory(pid)/'separation-trials'/trial_id/'manifest.json').is_file():
            return present(load(store.directory(pid),trial_id,p))
        task=next((task for task in _trial_tasks(pid) if task['trial_id']==trial_id),None)
        if task is None:raise HTTPException(404,'项目中没有这份试分离试听')
        return task
    except (ValueError,OSError,KeyError,TypeError) as exc:raise failure(exc)


@router.get('/projects/{pid}/separation-trials/{trial_id}/audio/{role}')
def trial_audio(pid:str,trial_id:str,role:str,request:Request):
    try:
        from .separation_trials import audio_path
        from .server import audio_response
        from .playback_audio import audition_file
        store=_store();p=store.load(pid)
        path=audio_path(store.directory(pid),trial_id,role,p)
        return audio_response(audition_file(path,store.directory(pid)),request.headers.get('range'))
    except (ValueError,OSError,KeyError,TypeError) as exc:raise failure(exc)


@router.get('/projects/{pid}/separation-trials/{trial_id}/waveform')
def trial_waveform(pid:str,trial_id:str,role:str='original',start_ms:float=0,end_ms:float|None=None,points:int=2400,bins:int|None=None):
    try:
        from .separation_trials import audio_path
        store=_store();p=store.load(pid)
        return _waveform(audio_path(store.directory(pid),trial_id,role,p),'trial:'+trial_id+':'+role,start_ms,end_ms,points,bins)
    except (ValueError,OSError,KeyError,TypeError,RuntimeError) as exc:raise failure(exc)


@router.get('/projects/{pid}/separations')
def separations(pid:str):
    _store().load(pid)
    from .advanced_api import project_tasks
    from .server import jobs,lock
    with lock:
        tasks = [{k:v for k,v in j.items() if k!='options'} for j in jobs.values()
                 if j.get('options',{}).get('_advanced',{}).get('project',{}).get('id')==pid
                 and j['options']['_advanced'].get('task_type')=='separation']
    for task in tasks:task['task_type']='separation'
    return {'tasks':tasks,**stems(pid)}


@router.get('/projects/{pid}/separations/{task_id}')
def separation_status(pid:str,task_id:str):
    task = next((t for t in separations(pid)['tasks'] if t['id']==task_id),None)
    if task is None:raise HTTPException(404,'项目中没有这份分离任务')
    return task


@router.get('/projects/{pid}/waveform')
def waveform(pid:str,source_id:str='original',start_ms:float=0,end_ms:float|None=None,points:int=2400,bins:int|None=None):
    try:
        _store().load(pid)
        return _waveform(source_path(pid,source_id),source_id,start_ms,end_ms,points,bins)
    except (ValueError,OSError,RuntimeError) as exc:raise failure(exc)


def _waveform(path,source_id,start_ms,end_ms,points,bins):
    import numpy as np
    import soundfile as sf
    info = sf.info(path)
    if info.samplerate != SR or info.frames < 1:raise ValueError('波形来源采样时钟不匹配或音源为空')
    duration_ms = info.frames * 1000 / SR
    end_ms = duration_ms if end_ms is None else end_ms
    count = bins if bins is not None else points
    if not np.isfinite(start_ms) or not np.isfinite(end_ms) or not 0<=start_ms<end_ms<=duration_ms or not 16<=count<=4096:raise ValueError('波形范围或分辨率无效')
    data,rate = sf.read(path,start=round(start_ms*SR/1000),stop=round(end_ms*SR/1000),dtype='float32',always_2d=True)
    if rate!=SR or not len(data) or not np.isfinite(data).all():raise ValueError('波形来源采样时钟不匹配或范围没有有限采样')
    mono = data.mean(axis=1); boundaries = np.linspace(0,len(mono),min(count,len(mono))+1,dtype=int)
    peaks = [[float(mono[a:b].min()),float(mono[a:b].max())] for a,b in zip(boundaries[:-1],boundaries[1:])]
    return {'source_id':source_id,'start_ms':start_ms,'end_ms':end_ms,'peaks':peaks,'sample_rate':SR}
