from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import threading
import traceback
import subprocess
import os
import sys
import time
import uuid
import re
import math
import shutil
import copy
from io import BytesIO
import zipfile
from urllib.parse import quote
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Request, Query, Body
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from .paths import ROOT, PRESETS, WEIGHTS
from .difficulty import PATTERN_CHOICES, V32_PATTERN_TAGS
from .naming import chart_id as make_chart_id
from .inference_policy import V32_INFERENCE_POLICY

app = FastAPI(title='Malody Chart Forge')
pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='chart-generator')
jobs = {}
lock = threading.Lock()
scheduler_active = set()
queue_sequence = 0
MUG_WEIGHT_BYTES = 1839231053
MUG_WEIGHT_SHA256 = 'af6ab91337d0ef6b518367082ac3f849448c6daaa01fd987678fb25ea44ca184'
_weight_verify_lock = threading.Lock()
_weight_verify_cache = {'signature': None, 'valid': False}

def mug_weights_ready():
    try:
        stat = WEIGHTS.stat()
    except OSError:
        return False
    signature = (str(WEIGHTS.resolve()), stat.st_size, stat.st_mtime_ns)
    with _weight_verify_lock:
        if _weight_verify_cache['signature'] == signature:
            return _weight_verify_cache['valid']
        valid = stat.st_size == MUG_WEIGHT_BYTES
        if valid:
            with WEIGHTS.open('rb') as stream:
                valid = hashlib.file_digest(stream, 'sha256').hexdigest() == MUG_WEIGHT_SHA256
        _weight_verify_cache.update(signature=signature, valid=valid)
        return valid

def store(job):
    path = ROOT / 'outputs' / job['id'] / 'job.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)

def parallel_benchmark_gate():
    path=ROOT/'benchmarks'/'latest.json'
    try:
        result=json.loads(path.read_text(encoding='utf-8'))
        if result.get('v32_inference_policy') != V32_INFERENCE_POLICY:
            return False,'V32 推理策略已更新，请用当前版本重新运行并行基准。'
        import torch
        if not torch.cuda.is_available() or result.get('device_name')!=torch.cuda.get_device_name(0): return False,'当前显卡与基准记录不一致。'
        try:
            detected=subprocess.run(['nvidia-smi','--query-gpu=driver_version','--format=csv,noheader'],
                capture_output=True,text=True,timeout=5).stdout.strip().splitlines()[0]
            if result.get('driver_version') and result['driver_version']!=detected:return False,'NVIDIA 驱动已变化，请重新运行硬件基准。'
        except (OSError,subprocess.SubprocessError,IndexError):return False,'无法确认 NVIDIA 驱动版本，请重新运行硬件基准。'
        singles=result.get('single_tests',{})
        if not all(singles.get(f'{engine}:{length}',{}).get('success') and singles[f'{engine}:{length}'].get('artifact_valid')
                   for engine in ('mug','v32') for length in ('short','medium','long')):
            return False,'MuG 与 V32 的短、中、长音频基准没有全部通过。'
        parallel=result.get('parallel_tests',{})
        required=('mug+mug','mug+v32','v32+v32')
        if not all(parallel.get(key,{}).get('success') and parallel[key].get('artifacts_valid') and
                   parallel[key].get('faster_than_serial') for key in required):
            return False,'本机双任务组合需要全部通过曲包校验并比串行更快。'
        reserve=min(parallel[key].get('min_free_vram_bytes',0) for key in required)
        if reserve<2*1024**3: return False,'双任务基准没有保留至少 2 GB 显存余量。'
        return True,'双任务基准已通过；本机至少保留 2 GB 显存余量。'
    except (OSError,ValueError,KeyError,ImportError):
        return False,'并行生成尚未通过本机双任务硬件基准，队列以串行方式运行。'

def configured_concurrency():
    ready,_=parallel_benchmark_gate()
    if not ready:return 1
    try:
        value=json.loads((ROOT/'benchmarks'/'concurrency.json').read_text(encoding='utf-8')).get('value',1)
        return 2 if value==2 else 1
    except (OSError,ValueError,TypeError):return 1

def legacy_source_ref(options):
    name=options.get('_source_upload')
    if isinstance(name,str) and re.fullmatch(r'[0-9a-f]{32}\.[a-z0-9]{1,8}',name):return {'type':'upload','name':name}
    parent=options.get('_reuse_source_job_id')
    if isinstance(parent,str) and re.fullmatch(r'[0-9a-f]{32}',parent):return {'type':'job_audio','job_id':parent}
    if options.get('source')=='https://www.youtube.com/watch?v=UKZt1vq8bKI':return {'type':'project_file','path':'uploads/reference-sirius.m4a'}
    video_id=options.get('artwork_video_id')
    if isinstance(video_id,str) and re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id):return {'type':'youtube','video_id':video_id}
    return None

for directory in (ROOT / 'outputs').iterdir():
    state = directory / 'job.json'
    if state.is_file():
        try:
            job = json.loads(state.read_text(encoding='utf-8'))
            if job['status'] == 'queued':
                job.update(status='paused', message='服务重启后已暂停，请在队列页继续')
            elif job['status'] == 'running':
                job.update(status='interrupted', message='服务重启时生成中断，可重试', error='任务被服务重启中断')
            if job.get('status') == 'paused' and not job.get('source_ref'):
                job['source_ref']=legacy_source_ref(job.get('options',{}))
            if job.get('status') == 'paused' and not job.get('source_ref'):
                job.update(status='needs_source', message='旧排队任务来源无法自动恢复，请重新导入歌曲')
            jobs[job['id']] = job
            if isinstance(job.get('queue_order'), int):
                queue_sequence = max(queue_sequence, job['queue_order'])
            elif job.get('status') in ('queued', 'paused'):
                queue_sequence += 1
                job['queue_order'] = queue_sequence
            if job.get('status') in ('paused', 'interrupted', 'needs_source'):
                store(job)
        except (ValueError, KeyError):
            continue

def get_job(job_id):
    if job_id not in jobs:
        raise HTTPException(404, '任务不存在')
    return jobs[job_id]

def worker(job_id, source, options):
    directory=ROOT/'outputs'/job_id
    progress_path=directory/'worker-progress.json'
    result_path=directory/'worker-result.json'
    request_path=directory/'queue-worker.json'
    log_path=ROOT/'logs'/f'{job_id}.log'
    payload={'source':str(Path(source).resolve()),'directory':str(directory.resolve()),'options':options}
    request_path.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
    environment=os.environ.copy();environment['PYTHONUTF8']='1'
    creation_flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    try:
        with log_path.open('w',encoding='utf-8') as log:
            process=subprocess.Popen([sys.executable,str(ROOT/'tools'/'queue_worker.py'),str(request_path)],
                cwd=ROOT,env=environment,stdout=log,stderr=subprocess.STDOUT,creationflags=creation_flags)
            previous=None
            while process.poll() is None:
                try:
                    progress_state=json.loads(progress_path.read_text(encoding='utf-8'))
                    state=(progress_state.get('message'),progress_state.get('progress'))
                    if state!=previous:
                        with lock:
                            jobs[job_id].update(status='running',message=state[0],progress=state[1])
                            store(jobs[job_id])
                        previous=state
                except (OSError,ValueError,TypeError):pass
                time.sleep(.35)
        if process.returncode!=0:
            detail=log_path.read_text(encoding='utf-8',errors='replace')[-5000:]
            try:
                failure=json.loads((directory/'worker-failure.json').read_text(encoding='utf-8'))
                quality=failure.get('quality_failure') or {}
                failure_reason=failure.get('error')
            except (OSError,ValueError):quality={};failure_reason=None
            if quality:
                with lock:
                    jobs[job_id].update(quality_alerts=quality.get('alerts',[]),
                        quality_duration=quality.get('duration_seconds'),
                        quality_preview=quality.get('preview',[]),quality_difficulty=quality.get('difficulty'))
                    store(jobs[job_id])
            raise RuntimeError(failure_reason or detail or f'生成工作进程退出，代码 {process.returncode}')
        if not result_path.is_file():raise RuntimeError('生成工作进程没有返回曲包报告')
        result=json.loads(result_path.read_text(encoding='utf-8'))
        if options.get('_advanced'):
            if options['_advanced'].get('task_type') == 'separation_trial':
                with lock:
                    jobs[job_id].update(status='completed', message='局部试分离已完成，可在原曲位置对比试听',
                        progress=100, trial_id=result['trial_id'], trial_manifest=result['trial_manifest'],
                        task_type='separation_trial')
                    store(jobs[job_id])
                return
            if options['_advanced'].get('task_type') == 'separation':
                with lock:
                    jobs[job_id].update(status='completed', message='音频分离已完成，可以试听并选择声部', progress=100, stem_set_id=result['stem_set_id'], stem_set=result['stem_set'], task_type='separation')
                    store(jobs[job_id])
                return
            from .advanced_api import commit_generated
            revisions = commit_generated(options, result,job_id=job_id)
            with lock:
                jobs[job_id].update(status='completed',message='分段候选已生成，请在高级台选择版本',progress=100,
                    advanced_revisions=list(dict.fromkeys(revisions + result.get('reused_revisions', []))),advanced_errors=result.get('errors',[]),
                    advanced_reused_revisions=result.get('reused_revisions', []),
                    stem_set_id=result.get('stem_set_id'),section_plan_id=result.get('section_plan_id'),
                    execution=result.get('execution'),elapsed_seconds=result.get('elapsed_seconds'))
                store(jobs[job_id])
            return
        if not (directory/'malody-4k.mcz').is_file():raise RuntimeError('生成工作进程没有生成 MCZ 曲包')
        with lock:
            jobs[job_id].update(status='completed',message='部分谱面已生成' if result['report'].get('partial') else '曲包已生成',progress=100,
                report=result['report'],download=f'/api/jobs/{job_id}/download')
            store(jobs[job_id])
    except Exception as exc:
        with lock:
            jobs[job_id].update(status='failed',message='生成失败',error=str(exc))
            store(jobs[job_id])


def infer_source_ref(source, options):
    if isinstance(options.get('_source_upload'), str):
        name = options['_source_upload']
        if re.fullmatch(r'[0-9a-f]{32}\.[a-z0-9]{1,8}', name):
            return {'type': 'upload', 'name': name}
    if isinstance(options.get('_reuse_source_job_id'), str) and re.fullmatch(r'[0-9a-f]{32}', options['_reuse_source_job_id']):
        return {'type': 'job_audio', 'job_id': options['_reuse_source_job_id']}
    if options.get('source') == 'https://www.youtube.com/watch?v=UKZt1vq8bKI':
        return {'type': 'project_file', 'path': 'uploads/reference-sirius.m4a'}
    video_id = options.get('artwork_video_id')
    if isinstance(video_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        return {'type': 'youtube', 'video_id': video_id}
    source = Path(source).resolve()
    try:
        relative = source.relative_to(ROOT.resolve())
        return {'type': 'project_file', 'path': relative.as_posix()}
    except ValueError:
        return None

def resolve_source_ref(job):
    ref = job.get('source_ref') or infer_source_ref('', job.get('options', {}))
    if not isinstance(ref, dict):
        raise ValueError('这条旧排队记录没有可恢复的音乐来源，请重新导入歌曲')
    kind = ref.get('type')
    if kind == 'upload' and re.fullmatch(r'[0-9a-f]{32}\.[a-z0-9]{1,8}', str(ref.get('name', ''))):
        source = ROOT / 'uploads' / ref['name']
    elif kind == 'job_audio' and re.fullmatch(r'[0-9a-f]{32}', str(ref.get('job_id', ''))):
        source = ROOT / 'outputs' / ref['job_id'] / '0' / 'audio.ogg'
    elif kind == 'youtube' and re.fullmatch(r'[A-Za-z0-9_-]{11}', str(ref.get('video_id', ''))):
        from .music import begin, get, ready_audio
        video_id = ref['video_id']
        try:
            track = get(video_id)
        except ValueError:
            track = begin(video_id)
        if track['status'] == 'failed':
            track = begin(video_id)
        import time
        deadline = time.monotonic() + 360
        while track['status'] not in ('ready', 'failed') and time.monotonic() < deadline:
            time.sleep(.75)
            track = get(video_id)
        if track['status'] != 'ready':
            raise ValueError(track.get('message') or 'YouTube 音乐下载失败，请重新导入音源')
        source, _ = ready_audio(video_id)
    elif kind == 'project_file' and isinstance(ref.get('path'), str):
        relative = Path(ref['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('音乐来源路径无效，请重新导入')
        source = (ROOT / relative).resolve()
        if ROOT.resolve() not in source.parents:
            raise ValueError('音乐来源路径无效，请重新导入')
    else:
        raise ValueError('这条任务的音乐来源无法恢复，请重新导入歌曲')
    if not source.is_file():
        raise ValueError('原始音乐文件已不存在，请重新导入歌曲')
    return source

def dispatch_next():
    submissions=[]
    with lock:
        slots=max(0,configured_concurrency()-len(scheduler_active))
        if not slots:return
        queued = [j for j in jobs.values() if j.get('status') == 'queued']
        queued.sort(key=lambda item:item.get('queue_order',0))
        active_exclusive=any(jobs.get(j,{}).get('options',{}).get('_advanced') for j in scheduler_active)
        if active_exclusive:return
        for job in queued:
            if not slots:break
            if job.get('options',{}).get('_advanced') and scheduler_active:break
            slots-=1
            scheduler_active.add(job['id'])
            job.update(status='running',message='准备读取音乐',progress=0)
            store(job)
            submissions.append(job['id'])
            if job.get('options',{}).get('_advanced'):break
    for job_id in submissions:pool.submit(run_queued_job,job_id)

def run_queued_job(job_id):
    try:
        with lock:
            job = jobs.get(job_id)
            if not job or job.get('status') != 'running':
                return
            options = dict(job.get('options', {}))
        source = resolve_source_ref(job)
        worker(job_id, source, options)
    except Exception as exc:
        with lock:
            if job_id in jobs and jobs[job_id].get('status') not in ('cancelled', 'completed', 'failed'):
                jobs[job_id].update(status='failed', message='任务准备失败', error=str(exc))
                store(jobs[job_id])
    finally:
        with lock:
            scheduler_active.discard(job_id)
        dispatch_next()

def enqueue_job(options, source_ref):
    return enqueue_jobs([(options, source_ref)])[0]

def enqueue_jobs(entries):
    """Reserve an entire submission before dispatching any independent leaf job."""
    global queue_sequence
    with lock:
        if sum(j.get('status') in ('queued', 'running', 'paused') for j in jobs.values()) + len(entries) > 100:
            raise HTTPException(429, '队列已满（最多 100 项），请等待或取消部分任务')
        prepared=[]
        try:
            for options, source_ref in entries:
                job_id = uuid.uuid4().hex
                directory = ROOT / 'outputs' / job_id
                directory.mkdir()
                queue_sequence += 1
                job = {'id': job_id, 'title': options['title'], 'artist': options['artist'],
                       'status': 'queued', 'message': '等待生成', 'progress': 0,
                       'created': datetime.now(timezone.utc).isoformat(), 'queue_order': queue_sequence,
                       'source_ref': source_ref, 'options': options}
                prepared.append(job)
                store(job)
        except Exception:
            # No job has been dispatched. Mark written records cancelled so
            # a restart cannot accidentally run half an accepted batch.
            for job in prepared:
                job.update(status='cancelled', message='批次提交未完成，未执行')
                store(job)
            raise
        for job in prepared: jobs[job['id']] = job
    dispatch_next()
    return [{'id': job['id']} for job in prepared]

def new_job(source, options):
    return enqueue_job(options, infer_source_ref(source, options))

def settings(title, artist, difficulties, ln_ratio, steps, seed, bpm, engine='mug', artwork_url='',
             difficulty_rules='{}', pattern='balanced', pattern_strength=20,
             mug_difficulty=8, mug_style='ranked', mug_guidance=1.5, mug_eta=0,
             v32_difficulty=8, v32_temperature=.9, v32_top_p=.9,
             v32_column_temperature=.8, v32_cfg_scale=1, v32_year=2024,
             v32_descriptors='', v32_negative_descriptors='', patterns=None, dynamic_enabled=False):
    if engine not in ('mug', 'v32'):
        raise HTTPException(400, '请选择有效的生成引擎')
    try:
        selected = json.loads(difficulties)
    except ValueError:
        raise HTTPException(400, '难度设置格式错误')
    if isinstance(selected, list):
        selected = ['medium' if key == 'normal' else key for key in selected]
    if not isinstance(selected, list) or not selected or len(selected) > 6 or any(not isinstance(key, str) or key not in PRESETS for key in selected):
        raise HTTPException(400, '请至少选择一个有效难度')
    if len(set(selected)) != len(selected):
        raise HTTPException(400, '难度不可重复')
    if not math.isfinite(ln_ratio) or not 0 <= ln_ratio <= 0.8 or steps not in (20, 50, 100) or not 0 <= seed <= 2147483640:
        raise HTTPException(400, '生成参数超出允许范围')
    if bpm is not None and not 20 <= bpm <= 600:
        raise HTTPException(400, 'BPM 必须介于 20–600')
    try:
        submitted_rules = json.loads(difficulty_rules)
    except (TypeError, ValueError):
        raise HTTPException(400, '各档谱面规则格式错误')
    if not isinstance(submitted_rules, dict) or set(submitted_rules) - set(PRESETS):
        raise HTTPException(400, '各档谱面规则包含未知难度')
    rule_ranges = {'rate': (0.5, 50), 'chord': (1, 4), 'gap': (20, 500),
                   'peak': (1, 56), 'hold_ms': (100, 5000)}
    normalized_rules = {}
    for key, values in submitted_rules.items():
        if not isinstance(values, dict) or set(values) - set(rule_ranges):
            raise HTTPException(400, f'{key} 谱面规则包含无效参数')
        normalized = {}
        for name, value in values.items():
            low, high = rule_ranges[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise HTTPException(400, f'{key} 的 {name} 超出允许范围')
            if name != 'rate' and not float(value).is_integer():
                raise HTTPException(400, f'{key} 的 {name} 必须是整数')
            normalized[name] = float(value) if name == 'rate' else int(value)
        normalized_rules[key] = normalized
    if patterns in (None, ''):
        selected_patterns = [pattern]
    else:
        try:
            selected_patterns = json.loads(patterns) if isinstance(patterns, str) else patterns
        except (TypeError, ValueError):
            raise HTTPException(400, '排键选择格式错误')
    if not isinstance(selected_patterns, list) or not selected_patterns or len(selected_patterns) > len(PATTERN_CHOICES) or any(not isinstance(item, str) or item not in PATTERN_CHOICES for item in selected_patterns):
        raise HTTPException(400, '请至少选择一种有效排键')
    if len(set(selected_patterns)) != len(selected_patterns):
        raise HTTPException(400, '排键不可重复')
    if pattern not in PATTERN_CHOICES or not 5 <= pattern_strength <= 35:
        raise HTTPException(400, '键型倾向参数无效')
    if mug_style not in ('ranked', 'loved', 'graveyard'):
        raise HTTPException(400, 'MuG 谱面风格无效')
    if not 1 <= mug_difficulty <= 8 or not 1 <= mug_guidance <= 30 or not 0 <= mug_eta <= 1:
        raise HTTPException(400, 'MuG 参数超出允许范围')
    if not 1 <= v32_difficulty <= 10 or not .1 <= v32_temperature <= 2 or not .1 <= v32_top_p <= 1:
        raise HTTPException(400, 'V32 采样参数超出允许范围')
    if not .1 <= v32_column_temperature <= 2 or not .5 <= v32_cfg_scale <= 5:
        raise HTTPException(400, 'V32 轨道或条件强度参数超出允许范围')
    if not 2007 <= v32_year <= 2024:
        raise HTTPException(400, 'V32 风格年份须介于 2007–2024')
    def descriptors(raw, field):
        values = [value.strip() for value in raw.split(',') if value.strip()]
        if len(values) > 4 or len(set(values)) != len(values) or any(len(value) > 64 for value in values):
            raise HTTPException(400, f'{field} 最多填写 4 个不重复的标签')
        known_path = ROOT / 'vendor' / 'Mapperatorinator' / 'datasets' / 'tags_2026.json'
        known = {item['name'] for item in json.loads(known_path.read_text(encoding='utf-8'))['tags']}
        if any(value not in known for value in values):
            raise HTTPException(400, f'{field} 含未知标签，请使用提示中的标准标签名')
        return values
    positive_tags = descriptors(v32_descriptors, 'V32 风格标签')
    negative_tags = descriptors(v32_negative_descriptors, 'V32 排除标签')
    if len(positive_tags) > 4:
        raise HTTPException(400, 'V32 风格标签最多 4 个')
    for selected_pattern in selected_patterns:
        pattern_tag = V32_PATTERN_TAGS.get(selected_pattern)
        pattern_tags = list(dict.fromkeys([*positive_tags, *([pattern_tag] if pattern_tag else [])]))
        if len(pattern_tags) > 4:
            raise HTTPException(400, f'{selected_pattern} 会占用一个 V32 风格标签位置，请将自定义标签减少到 3 个')
        if negative_tags and (v32_cfg_scale <= 1 or len(negative_tags) != len(pattern_tags)):
            raise HTTPException(400, f'{selected_pattern} 的 V32 排除标签数量须与正向条件一致，且条件强度需大于 1')
    title, artist = title.strip(), artist.strip()
    if not title or len(title) > 120 or len(artist) > 120:
        raise HTTPException(400, '请填写曲名（不超过 120 字）')
    from .artwork import video_id_from_url
    try:
        artwork_id = video_id_from_url(artwork_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not isinstance(dynamic_enabled,bool):raise HTTPException(400,'段落适配开关无效')
    return {'dynamic_enabled':dynamic_enabled,'dynamic_strength':1.,'title': title, 'artist': artist or 'Unknown', 'difficulties': selected,
            'patterns': selected_patterns, 'artwork_video_id': artwork_id,
            'ln_ratio': ln_ratio, 'steps': steps, 'seed': seed, 'bpm': bpm, 'engine': engine,
            'difficulty_rules': normalized_rules, 'pattern': pattern, 'pattern_strength': int(pattern_strength),
            'mug_difficulty': float(mug_difficulty), 'mug_style': mug_style,
            'mug_guidance': float(mug_guidance), 'mug_eta': float(mug_eta),
            'v32_difficulty': float(v32_difficulty), 'v32_temperature': float(v32_temperature),
            'v32_top_p': float(v32_top_p), 'v32_column_temperature': float(v32_column_temperature),
            'v32_cfg_scale': float(v32_cfg_scale), 'v32_year': int(v32_year),
            # Preserve the historical single-pattern settings contract while
            # retaining a clean base list for independent per-pattern calls.
            'v32_descriptors': list(dict.fromkeys([*positive_tags, *([V32_PATTERN_TAGS[selected_patterns[0]]] if selected_patterns[0] in V32_PATTERN_TAGS else [])])),
            'v32_descriptors_base': positive_tags, 'v32_negative_descriptors': negative_tags}

def ensure_engine(options):
    if options['engine'] == 'v32':
        from .mapperatorinator import ready
        available = ready()
    else:
        available = mug_weights_ready()
    if not available:
        raise HTTPException(503, '所选模型尚未完成部署，请选择其他引擎')

@app.get('/api/gpu/resident')
def resident_status():
    from .resident import status
    return status()


@app.post('/api/gpu/resident/release')
def resident_release():
    from .resident import release
    try:return release()
    except RuntimeError as exc:raise HTTPException(409,str(exc))


@app.get('/api/health')
def health():
    import torch
    from .mapperatorinator import ready as v32_ready, PYTHON as V32_PYTHON, MODEL_ROOT as V32_MODEL_ROOT

    cuda_available = torch.cuda.is_available()
    gpu_name = torch.cuda.get_device_name(0) if cuda_available else None
    mug_exists = WEIGHTS.is_file()
    mug_size_ok = mug_exists and WEIGHTS.stat().st_size == MUG_WEIGHT_BYTES
    mug_ready = mug_weights_ready()
    v32_python_ready = V32_PYTHON.is_file()
    v32_weights_ready = all(
        (V32_MODEL_ROOT / folder / 'model.safetensors').is_file() and
        (V32_MODEL_ROOT / folder / 'model.safetensors').stat().st_size == 865900700
        for folder in ('v32-mania', 'v32-timing'))
    v32_ready_state = v32_ready() and cuda_available
    mug_reason = None if mug_ready else ('MuG 权重 SHA-256 不匹配，请重新运行一键配置环境.bat 下载校验。'
                                         if mug_size_ok else 'MuG 权重大小不正确，请重新运行一键配置环境.bat 校验。'
                                         if mug_exists else '缺少 MuG 模型权重，请运行“一键配置环境.bat”。')
    v32_reason = None if v32_ready_state else (
        'V32 需要可用的 NVIDIA CUDA 显卡，请检查驱动和 V32 环境。' if not cuda_available else
        '缺少 V32 独立 Python 环境，请运行“一键配置环境.bat”并选择 V32。' if not v32_python_ready else
        '缺少 V32 模型权重或部署校验文件，请运行“一键配置环境.bat”并选择 V32。')
    return {
        'api_version': 2,
        'advanced_workflow_version': 7,
        'task_history_version': 1,
        'gpu_resident_version': 1,
        'ready': mug_ready or v32_ready_state,
        'gpu': gpu_name or 'CPU（生成速度可能较慢）',
        'cuda_available': cuda_available,
        'reference': (ROOT / 'uploads' / 'reference-sirius.m4a').is_file(),
        'engines': {
            'mug': {'label': 'MuG Diffusion', 'ready': mug_ready, 'reason': mug_reason,
                    'weights_ready': mug_ready, 'python_ready': (ROOT / '.venv' / 'Scripts' / 'python.exe').is_file(),
                    'device': gpu_name or 'CPU'},
            'v32': {'label': 'Mapperatorinator V32', 'ready': v32_ready_state,
                    'reason': v32_reason, 'weights_ready': v32_weights_ready,
                    'python_ready': v32_python_ready, 'cuda_available': cuda_available,
                    'device': gpu_name or '不可用'}},
        'presets': PRESETS}

@app.get('/api/jobs')
def list_jobs():
    with lock:
        return [{'id': j['id'], 'title': j['title'], 'status': j['status'], 'created': j['created'],
                 'queue_order': j.get('queue_order')}
                for j in sorted(jobs.values(), key=lambda j: j['created'], reverse=True)[:20]]

@app.get('/api/queue')
def queue_status():
    states = {'queued', 'running', 'paused', 'interrupted', 'needs_source', 'failed'}
    with lock:
        items = [j for j in jobs.values() if j.get('status') in states]
        items.sort(key=lambda item: (item.get('queue_order', 0), item.get('created', '')))
        parallel_enabled,parallel_reason=parallel_benchmark_gate()
        return {'items': [{k: item.get(k) for k in ('id', 'title', 'artist', 'status', 'message',
                                                     'progress', 'created', 'queue_order', 'error')}
                          for item in items],
                'running': sum(item.get('status') == 'running' for item in items),
                'waiting': sum(item.get('status') in ('queued', 'paused') for item in items),
                'paused': sum(item.get('status') == 'paused' for item in items),
                'parallel_enabled': parallel_enabled, 'max_concurrency': 2 if parallel_enabled else 1,
                'concurrency': configured_concurrency(), 'parallel_reason': parallel_reason}

@app.post('/api/queue/concurrency')
def set_queue_concurrency(payload: dict = Body(...)):
    value=payload.get('value')
    if isinstance(value,bool) or value not in (1,2):raise HTTPException(400,'并发数只能设为 1 或 2')
    if value==2:
        ready,reason=parallel_benchmark_gate()
        if not ready:raise HTTPException(409,reason)
    path=ROOT/'benchmarks'/'concurrency.json';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'value':value},ensure_ascii=False),encoding='utf-8')
    dispatch_next()
    return {'concurrency':value}

@app.post('/api/queue/resume')
def resume_queue():
    resumed, needs_source = [], []
    with lock:
        queued = sorted((j for j in jobs.values() if j.get('status') == 'paused'),
                        key=lambda item: item.get('queue_order', 0))
        for job in queued:
            if not job.get('source_ref'):
                job['source_ref'] = legacy_source_ref(job.get('options', {}))
            if not job.get('source_ref'):
                job.update(status='needs_source', message='音乐来源无法恢复，请重新导入歌曲')
                needs_source.append(job['id'])
            else:
                job.update(status='queued', message='等待生成', progress=0)
                resumed.append(job['id'])
            store(job)
    dispatch_next()
    return {'resumed': resumed, 'needs_source': needs_source}

@app.get('/api/history')
def paginated_history(page: int = Query(1, ge=1), page_size: int = Query(8, ge=1, le=8),
                     q: str = Query('', max_length=200), engine: str = Query(''),
                     status: str = Query(''), difficulty: str = Query(''),
                     sort: str = Query('newest')):
    allowed_status = {'', 'queued', 'running', 'paused', 'interrupted', 'needs_source', 'cancelled', 'completed', 'failed'}
    allowed_engine = {'', 'mug', 'v32', 'advanced'}
    allowed_sort = {'newest', 'oldest', 'title'}
    if status not in allowed_status or engine not in allowed_engine or sort not in allowed_sort:
        raise HTTPException(400, '曲包筛选条件无效')
    needle = q.casefold()
    advanced_items=[]
    advanced_total_all=0
    # Advanced projects store final MCZ assemblies outside the ordinary job history.
    # Expose each actual export in the package library with an identity-safe preview target.
    from .advanced_api import store as advanced_store
    from .advanced import read as read_advanced
    for project_path in advanced_store.root.glob('*/project.json'):
        pid=project_path.parent.name
        if not re.fullmatch(r'[0-9a-f]{32}',pid):continue
        try:
            project=read_advanced(project_path)
        except (OSError,ValueError,KeyError):
            continue
        if not isinstance(project,dict) or project.get('id')!=pid:continue
        project_matches=not needle or needle in (project.get('title','')+' '+project.get('artist','')).casefold()
        assemblies=project.get('assemblies',[])
        if not isinstance(assemblies,list):continue
        for assembly in assemblies:
            if not isinstance(assembly,dict):continue
            aid=assembly.get('id','')
            if not isinstance(aid,str) or not re.fullmatch(r'[0-9a-f]{32}',aid):continue
            folder=advanced_store.directory(pid)/'assemblies'/aid
            try:report=read_advanced(folder/'report.json')
            except (OSError,ValueError):continue
            if not isinstance(report,dict):continue
            charts=report.get('charts',[])
            if not isinstance(charts,list) or not charts or any(not isinstance(row,dict) for row in charts):continue
            if not (folder/'audio.ogg').is_file() or not (folder/'0').is_dir():continue
            if not any((folder/'0').glob('*.mc')):continue
            advanced_total_all+=1
            if not project_matches:continue
            engines={row.get('engine') for row in charts}
            if engine and engine!='advanced' and engine not in engines:continue
            if status and status!='completed':continue
            difficulties=list(dict.fromkeys(row.get('difficulty') for row in charts if row.get('difficulty')))
            if difficulty and difficulty not in difficulties:continue
            level_names={'easy':'Easy','medium':'Medium','hard':'Hard','expert':'Expert','master':'Master','lunatic':'Lunatic'}
            advanced_items.append({'id':'advanced-'+aid,'type':'advanced','project_id':pid,
                'assembly_id':aid,'title':project.get('title','高级制谱成品'),
                'artist':project.get('artist',''),'status':'completed',
                'created':assembly.get('created') or report.get('created',''),
                'engine':'advanced','difficulties':difficulties,
                'difficulty_labels':[level_names.get(value,value) for value in difficulties],'download':assembly.get('download') or f"/api/advanced/projects/{pid}/assemblies/{aid}/download",
                'cover':f"/api/advanced/projects/{pid}/background" if project.get('background') else None,
                'thumbnail':None,'segment_count':len(assembly.get('mapping',[])),
                'chart_count':len(charts),'duration':assembly.get('duration')})
    with lock:
        matches = []
        for job in jobs.values():
            if job.get('options', {}).get('_advanced'):
                continue
            if needle and needle not in (job.get('title', '') + ' ' + job.get('artist', '')).casefold():
                continue
            options = job.get('options', {})
            if engine and options.get('engine', 'mug') != engine:
                continue
            if status and job.get('status') != status:
                continue
            if difficulty and difficulty not in options.get('difficulties', []):
                continue
            matches.append(job)
        if sort == 'oldest':
            matches.sort(key=lambda j: j.get('created', ''))
        elif sort == 'title':
            matches.sort(key=lambda j: (j.get('title', '').casefold(), j.get('created', '')), reverse=False)
        else:
            matches.sort(key=lambda j: j.get('created', ''), reverse=True)
        ordinary_items=[]
        for job in matches:
            report=job.get('report',{});artwork=report.get('artwork',{})
            item={k:job[k] for k in ('id','title','artist','status','created') if k in job}
            item.update(type='song',engine=job.get('options',{}).get('engine','mug'),
                        difficulties=[d['key'] for d in report.get('difficulties',[])] or job.get('options',{}).get('difficulties',[]),
                        difficulty_labels=[d['label'] for d in report.get('difficulties',[])],
                        download=f"/api/jobs/{job['id']}/download" if job.get('status')=='completed' else None,
                        cover=f"/api/jobs/{job['id']}/files/background.jpg" if artwork.get('status')=='ready' else None)
            from .artwork import video_id_from_url
            try:video_id=job.get('options',{}).get('artwork_video_id') or video_id_from_url(job.get('options',{}).get('source',''))
            except ValueError:video_id=None
            item['thumbnail']=f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg' if video_id else None
            ordinary_items.append(item)
        items_all=ordinary_items+advanced_items
        if sort=='oldest':items_all.sort(key=lambda row:row.get('created',''))
        elif sort=='title':items_all.sort(key=lambda row:(row.get('title','').casefold(),row.get('created','')))
        else:items_all.sort(key=lambda row:row.get('created',''),reverse=True)
        total = len(items_all)
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        items=items_all[(page-1)*page_size:page*page_size]
        ordinary_total_all=sum(not j.get('options', {}).get('_advanced') for j in jobs.values())
        return {'items': items, 'total': total, 'total_all': ordinary_total_all+advanced_total_all, 'page': page,
                'pages': pages, 'page_size': page_size, 'filters': {'engine': engine, 'status': status,
                                                                    'difficulty': difficulty, 'sort': sort}}

@app.get('/api/task-history')
def task_history(page:int=Query(1,ge=1),page_size:int=Query(10,ge=1,le=100),
                 q:str=Query('',max_length=200),type:str=Query(''),project_id:str=Query('')):
    from .advanced_api import store as project_store
    from .task_history import history_page
    import copy
    with lock:records=copy.deepcopy(list(jobs.values()))
    try:return history_page(project_store,records,page,page_size,q,type,project_id)
    except (ValueError,TypeError,KeyError,OSError) as exc:raise HTTPException(400,str(exc))


@app.post('/api/task-history/open-folder')
def task_history_open_folder(payload:dict=Body(...)):
    from .advanced_api import store as project_store
    from .task_history import open_folder
    import copy
    with lock:records=copy.deepcopy(list(jobs.values()))
    try:return open_folder(project_store,ROOT,records,payload)
    except (ValueError,TypeError,KeyError,OSError) as exc:raise HTTPException(400,str(exc))
    except RuntimeError as exc:raise HTTPException(409,str(exc))


@app.get('/api/storage')
def storage_usage():
    def total_bytes(directory):
        return sum(path.stat().st_size for path in directory.rglob('*') if path.is_file())
    outputs = ROOT / 'outputs'
    uploads = ROOT / 'uploads'
    return {'outputs_bytes': total_bytes(outputs), 'uploads_bytes': total_bytes(uploads),
            'total_bytes': total_bytes(outputs) + total_bytes(uploads),
            'free_bytes': shutil.disk_usage(ROOT).free}

@app.get('/api/jobs/{job_id}')
def job_status(job_id: str):
    with lock:
        return dict(get_job(job_id))

@app.post('/api/jobs')
async def upload(file: UploadFile = File(...), title: str = Form(...), artist: str = Form(''),
                 difficulties: str = Form('["easy","normal","hard"]'), ln_ratio: float = Form(0.15),
                 steps: int = Form(50), seed: int = Form(20261001), bpm: float | None = Form(None),
                 engine: str = Form('mug'), artwork_url: str = Form(''), difficulty_rules: str = Form('{}'),
                 pattern: str = Form('balanced'), pattern_strength: int = Form(20),
                 mug_difficulty: float = Form(8), mug_style: str = Form('ranked'),
                 mug_guidance: float = Form(1.5), mug_eta: float = Form(0),
                 v32_difficulty: float = Form(8), v32_temperature: float = Form(.9),
                 v32_top_p: float = Form(.9), v32_column_temperature: float = Form(.8),
                 v32_cfg_scale: float = Form(1), v32_year: int = Form(2024),
                 v32_descriptors: str = Form(''), v32_negative_descriptors: str = Form(''),
                 patterns: str = Form(''), dynamic_enabled: bool = Form(True)):
    options = settings(title, artist, difficulties, ln_ratio, steps, seed, bpm, engine, artwork_url,
                       difficulty_rules, pattern, pattern_strength, mug_difficulty, mug_style,
                       mug_guidance, mug_eta, v32_difficulty, v32_temperature, v32_top_p,
                       v32_column_temperature, v32_cfg_scale, v32_year,
                       v32_descriptors, v32_negative_descriptors, patterns, dynamic_enabled)
    extension = Path(file.filename or '').suffix.lower()
    if extension not in ('.mp3', '.wav', '.flac', '.m4a', '.ogg', '.aac', '.opus'):
        raise HTTPException(400, '请选择 MP3、WAV、FLAC、M4A、OGG、AAC 或 OPUS 音频')
    ensure_engine(options)
    source = ROOT / 'uploads' / (uuid.uuid4().hex + extension)
    size = 0
    try:
        with source.open('wb') as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 200 * 1024 * 1024:
                    raise HTTPException(413, '音频文件不能超过 200 MB')
                target.write(chunk)
        if not size:
            raise HTTPException(400, '音频文件为空')
        # Retain only this job's upload basename so deletion can safely clean up.
        options['_source_upload'] = source.name
        return new_job(source, options)
    except Exception:
        source.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

@app.post('/api/reference')
def reference(difficulties: str = Form('["easy","normal","hard"]'), ln_ratio: float = Form(0.15),
              steps: int = Form(50), seed: int = Form(20261001), bpm: float | None = Form(None),
              engine: str = Form('mug'), difficulty_rules: str = Form('{}'),
              pattern: str = Form('balanced'), pattern_strength: int = Form(20),
              mug_difficulty: float = Form(8), mug_style: str = Form('ranked'),
              mug_guidance: float = Form(1.5), mug_eta: float = Form(0),
              v32_difficulty: float = Form(8), v32_temperature: float = Form(.9),
              v32_top_p: float = Form(.9), v32_column_temperature: float = Form(.8),
              v32_cfg_scale: float = Form(1), v32_year: int = Form(2024),
              v32_descriptors: str = Form(''), v32_negative_descriptors: str = Form(''),
              patterns: str = Form(''), dynamic_enabled: bool = Form(True)):
    source = ROOT / 'uploads' / 'reference-sirius.m4a'
    if not source.is_file():
        raise HTTPException(404, '参考音轨尚未准备完成')
    options = settings('シリウスの心臓（天狼星的心脏）', 'ヰ世界情緒', difficulties, ln_ratio, steps, seed, bpm, engine,
                       difficulty_rules=difficulty_rules, pattern=pattern, pattern_strength=pattern_strength,
                       mug_difficulty=mug_difficulty, mug_style=mug_style, mug_guidance=mug_guidance, mug_eta=mug_eta,
                       v32_difficulty=v32_difficulty, v32_temperature=v32_temperature, v32_top_p=v32_top_p,
                       v32_column_temperature=v32_column_temperature, v32_cfg_scale=v32_cfg_scale,
                       v32_year=v32_year, v32_descriptors=v32_descriptors,
                       v32_negative_descriptors=v32_negative_descriptors, patterns=patterns, dynamic_enabled=dynamic_enabled)
    ensure_engine(options)
    options['source'] = 'https://www.youtube.com/watch?v=UKZt1vq8bKI'
    options['artwork_video_id'] = 'UKZt1vq8bKI'
    return new_job(source, options)

@app.post('/api/jobs/{job_id}/regenerate')
def regenerate_job(job_id: str):
    if not re.fullmatch(r'[0-9a-f]{32}', job_id):
        raise HTTPException(404, '曲包记录不存在')
    with lock:
        original = dict(get_job(job_id))
    state = original.get('status')
    if state not in ('completed', 'failed', 'interrupted'):
        raise HTTPException(409, '只有已完成、失败或中断的任务可以重试')
    options = dict(original.get('options', {}))
    if options.get('_advanced'):
        from .advanced_api import store
        snapshot=options['_advanced'];pid=snapshot['project']['id']
        try:
            if snapshot.get('task_type') == 'separation_trial':
                from .separation_trials import validate_snapshot
                validate_snapshot(snapshot)
                current_project=store.load(pid)
                if (current_project['samples'] != snapshot['project']['samples'] or
                        current_project['source_pcm_sha256'] != snapshot['project']['source_pcm_sha256']):
                    raise ValueError('试分离原曲已变化，请重新选择范围提交')
            if snapshot.get('task_type') not in ('separation', 'separation_trial'):store.segment(store.load(pid),snapshot['segment']['id'])
            source=store.directory(pid)/'source.wav'
            if not source.is_file():raise ValueError('高级项目原音频缺失')
        except ValueError as exc:raise HTTPException(409,str(exc))
        if snapshot.get('task_type') not in ('separation', 'separation_trial'):
            ensure_engine(options)
        else:
            from .separation import deployment, validated_settings
            try:
                separation_settings=validated_settings(snapshot.get('settings',{}))
                deployment(separation_settings['model'])
            except (OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:
                raise HTTPException(409,str(exc))
        return enqueue_job(options,original.get('source_ref') or {'type':'project_file','path':f'outputs/advanced/{pid}/source.wav'})
    source_job_id = None
    if state == 'completed':
        source = ROOT / 'outputs' / job_id / '0' / 'audio.ogg'
        source_job_id = job_id
    else:
        parent_id = options.get('_reuse_source_job_id')
        if isinstance(parent_id, str) and re.fullmatch(r'[0-9a-f]{32}', parent_id):
            source = ROOT / 'outputs' / parent_id / '0' / 'audio.ogg'
            source_job_id = parent_id
        else:
            upload_name = options.get('_source_upload')
            if isinstance(upload_name, str) and re.fullmatch(r'[0-9a-f]{32}\.[a-z0-9]{1,8}', upload_name):
                source = ROOT / 'uploads' / upload_name
                source_job_id = job_id
            elif options.get('source') == 'https://www.youtube.com/watch?v=UKZt1vq8bKI' and (ROOT / 'uploads' / 'reference-sirius.m4a').is_file():
                source = ROOT / 'uploads' / 'reference-sirius.m4a'
            elif options.get('artwork_video_id'):
                try:
                    from .music import ready_audio
                    source, _ = ready_audio(options['artwork_video_id'])
                except (ValueError, KeyError):
                    raise HTTPException(409, '原音乐缓存已失效，请重新导入音乐')
            else:
                raise HTTPException(409, '原始音乐不可用，请重新上传或导入音乐')
    if not source.is_file():
        raise HTTPException(409, '原始音乐缺失，请重新上传或导入音乐')
    options.pop('_source_upload', None)
    options['title'] = original.get('title', options.get('title', ''))
    options['artist'] = original.get('artist', options.get('artist', ''))
    if source_job_id:
        options['_reuse_source_job_id'] = source_job_id
    else:
        options.pop('_reuse_source_job_id', None)
    ensure_engine(options)
    return new_job(source, options)

def _version_directory(job_id, key):
    if not re.fullmatch(r'[0-9a-f]{32}', job_id) or not re.fullmatch(r'(?:[a-z]+--)?[a-z]+', key):
        raise HTTPException(404, '难度或曲包记录无效')
    return ROOT / 'outputs' / job_id / 'versions' / key

def _read_version_manifest(directory):
    manifest_path=directory/'manifest.json'
    if not manifest_path.is_file():
        return {'active':1,'versions':[]}
    try:
        return json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError,ValueError):
        raise HTTPException(500,'谱面版本记录损坏')

def _chart_rows(job):
    report=job.get('report') or {}
    rows=report.get('difficulties') or report.get('charts') or []
    return rows

def _sync_chart_rows(report):
    rows=report.get('difficulties') or report.get('charts') or []
    report['difficulties']=rows
    report['charts']=copy.deepcopy(rows)

def _chart_row(job, identifier):
    rows=_chart_rows(job)
    row=next((item for item in rows if item.get('chart_id')==identifier or item.get('key')==identifier),None)
    if row is None:
        patterns=job.get('options',{}).get('patterns') or [job.get('options',{}).get('pattern','balanced')]
        row=next((item for item in rows if item.get('difficulty',item.get('key'))==identifier and
                  item.get('pattern',patterns[0])==patterns[0]),None)
    return row

def _chart_disk_path(job_id, row):
    directory=ROOT/'outputs'/job_id/'0'
    filename=row.get('filename')
    if isinstance(filename,str) and Path(filename).name==filename and filename.endswith('.mc'):
        return directory/filename
    return directory/f'{row.get("key")}.mc'

def _charts_from_disk(job_id, identifiers=None):
    job=jobs.get(job_id)
    if not job: raise HTTPException(404,'曲包记录不存在')
    rows=_chart_rows(job)
    selected=rows if identifiers is None else [_chart_row(job,key) for key in identifiers]
    if not selected or any(row is None for row in selected): raise HTTPException(409,'曲包谱面清单无效')
    charts={}
    for row in selected:
        identifier=row.get('chart_id') or row['key']
        path=_chart_disk_path(job_id,row)
        if not path.is_file(): raise HTTPException(409,f'{identifier} 谱面文件缺失，无法更新曲包')
        try: charts[identifier]=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError): raise HTTPException(409,f'{identifier} 谱面文件损坏')
    return charts

def _save_report_and_package(job_id, job, charts):
    from .charts import package
    directory=ROOT/'outputs'/job_id
    report=job['report']
    background=directory/'0'/'background.jpg'
    filenames={row.get('chart_id') or row['key']:row['filename'] for row in _chart_rows(job)
               if row.get('filename')}
    package(directory,charts,directory/'0'/'audio.ogg',report,
            background if background.is_file() else None,filenames=filenames)
    job['download']=f'/api/jobs/{job_id}/download'
    with lock:
        jobs[job_id]=job
        store(job)

@app.post('/api/jobs/{job_id}/difficulties/{key}/regenerate')
def regenerate_difficulty(job_id: str, key: str, payload: dict = Body(default={} )):
    from .charts import Note, serialize, validate_chart, chart_stats
    from .difficulty import calibrate, PRESETS as DIFFICULTY_PRESETS, PATTERN_LABELS
    from .naming import chart_stem, model_display
    with lock:
        job=dict(get_job(job_id))
    if job.get('status')!='completed': raise HTTPException(409,'只有已完成的曲包可以单档迭代')
    row=_chart_row(job,key)
    if row is None: raise HTTPException(409,'该排键与难度组合不在当前曲包中')
    identifier=row.get('chart_id') or row['key']
    difficulty_key=row.get('difficulty',row['key'])
    pattern=row.get('pattern',job.get('options',{}).get('pattern','balanced'))
    directory=ROOT/'outputs'/job_id
    cache_path=directory/'chart-cache'/pattern/'generation-cache.json'
    if not cache_path.is_file() and len(job.get('options',{}).get('patterns',[]))<=1:
        cache_path=directory/'generation-cache.json'
    if not cache_path.is_file(): raise HTTPException(409,'该曲包没有共享母谱缓存，请重新生成后再使用单档迭代')
    try: cache=json.loads(cache_path.read_text(encoding='utf-8'))
    except (OSError,ValueError): raise HTTPException(409,'单档生成缓存损坏，请重新生成曲包')
    try:
        seed=payload.get('seed',job['options'].get('seed',20261001))
        if isinstance(seed,bool) or not isinstance(seed,int) or not 0<=seed<=2147483640: raise ValueError()
        from .pipeline import _variant_seed
        seed=_variant_seed(seed,pattern)
        supplied=payload.get('rule',{})
        if not isinstance(supplied,dict) or set(supplied)-{'rate','chord','gap','peak','hold_ms'}: raise ValueError()
        existing=job['options'].get('difficulty_rules',{}).get(difficulty_key,{})
        rule={**existing,**supplied}
        bounds={'rate':(.5,50),'chord':(1,4),'gap':(20,500),'peak':(1,56),'hold_ms':(100,5000)}
        for name,value in rule.items():
            low,high=bounds[name]
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high or (name!='rate' and not float(value).is_integer()): raise ValueError()
        candidates=[]
        for timestamp,strength,originals in cache['candidates']:
            candidates.append((float(timestamp),float(strength),[Note(float(n[0]),int(n[1]),float(n[2]) if n[2] is not None else None) for n in originals]))
        if cache.get('section_plan'):
            from .adaptive_difficulty import calibrate_adaptive
            notes,adjustment=calibrate_adaptive(candidates,float(cache['duration_ms']),difficulty_key,float(job['options'].get('ln_ratio',.15)),seed,rule,pattern,cache['section_plan'])
        else:
            notes,adjustment=calibrate(candidates,float(cache['duration_ms']),difficulty_key,
                float(job['options'].get('ln_ratio',.15)),seed,rule,pattern=pattern)
    except (KeyError,TypeError,ValueError,OverflowError):
        raise HTTPException(400,'单档种子或生成规则无效')
    label=DIFFICULTY_PRESETS[difficulty_key]['label']
    from .naming import chart_stem
    version_name=chart_stem(job['title'],job['options'].get('engine','mug'),pattern,difficulty_key)
    if cache.get('engine')=='v32':
        from .mapperatorinator import serialize_with_timing
        timing=cache.get('timings',{}).get(difficulty_key)
        if not timing: raise HTTPException(409,'V32 分段 BPM 缓存缺失，无法单独重建此难度')
        chart=serialize_with_timing(notes,job['title'],job['artist'],version_name,timing)
        chart['meta']['creator']='Malody Chart Forge / Mapperatorinator V32 (AI)'
    else:
        chart=serialize(notes,job['title'],job['artist'],version_name,float(cache['bpm']))
        chart['meta']['creator']='Malody Chart Forge / MuG Diffusion v1.0.0'
    validation=validate_chart(chart,float(cache['duration_ms']))
    stats=chart_stats(notes,float(cache['duration_ms'])/1000)
    waveform=sample_rate=None
    analysis_path=directory/'analysis.wav'
    if analysis_path.is_file():
        try:
            import soundfile as sf
            waveform,sample_rate=sf.read(analysis_path,dtype='float32')
        except Exception: waveform=sample_rate=None
    from .quality import assess
    alerts=assess(notes,float(cache['duration_ms'])/1000,adjustment['target_active_nps'],waveform,sample_rate,chart,difficulty_key)
    for alert in alerts: alert.update(pattern=pattern,chart_id=identifier)
    report=copy.deepcopy(job.get('report',{})); row=next((item for item in _chart_rows(job) if (item.get('chart_id') or item['key'])==identifier),None)
    if row is None: raise HTTPException(409,'曲包报告中找不到目标难度')
    original_row=copy.deepcopy(row)
    row.update(stats,validation=validation,difficulty_adjustment=adjustment,quality_alerts=alerts,
               model_raw_notes=cache.get('raw_count',cache.get('raw_counts',{}).get(difficulty_key,row.get('model_raw_notes',0))))
    report.setdefault('quality_alerts',[])
    report['quality_alerts']=[a for a in report['quality_alerts'] if a.get('chart_id')!=identifier]+alerts
    version_dir=_version_directory(job_id,identifier);version_dir.mkdir(parents=True,exist_ok=True)
    manifest=_read_version_manifest(version_dir)
    if not manifest.get('versions'):
        original_chart=_chart_disk_path(job_id,original_row); original_archive=directory/'malody-4k.mcz'
        if not original_chart.is_file() or not original_archive.is_file(): raise HTTPException(409,'当前难度或曲包文件缺失')
        initial_stats={field:original_row.get(field) for field in ('notes','holds','ln_ratio','average_nps','peak_nps','lanes','density','quality_alerts','difficulty_adjustment','validation')}
        (version_dir/'v1.mc').write_bytes(original_chart.read_bytes())
        shutil.copy2(original_archive,version_dir/'v1.mcz')
        (version_dir/'v1.json').write_text(json.dumps({'version':1,'seed':job['options'].get('seed'),
            'model_version':cache.get('model_version'),'parameters':job['options'].get('difficulty_rules',{}).get(difficulty_key,{}),
            'stats':initial_stats,'preview':report.get('previews',{}).get(identifier,[])},ensure_ascii=False),encoding='utf-8')
        manifest={'active':1,'versions':[{'version':1,**initial_stats,'seed':job['options'].get('seed')} ]}
    new_version=max((item['version'] for item in manifest['versions']),default=0)+1
    chart_path=_chart_disk_path(job_id,original_row)
    (version_dir/f'v{new_version}.mc').write_text(json.dumps(chart,ensure_ascii=False,indent=2),encoding='utf-8')
    preview=[[round(n.start,2),n.lane,round(n.end,2) if n.end else None] for n in notes]
    version_data={'version':new_version,'seed':seed,'model_version':cache.get('model_version'),
        'parameters':rule,'stats':{field:row.get(field) for field in ('notes','holds','ln_ratio','average_nps','peak_nps','lanes','density','quality_alerts','difficulty_adjustment','validation')},
        'preview':preview,'created':datetime.now(timezone.utc).isoformat()}
    (version_dir/f'v{new_version}.json').write_text(json.dumps(version_data,ensure_ascii=False),encoding='utf-8')
    chart_path.write_text(json.dumps(chart,ensure_ascii=False,indent=2),encoding='utf-8')
    report.setdefault('previews',{})[identifier]=preview
    manifest['active']=new_version
    manifest['versions'].append({'version':new_version,**version_data['stats'],'seed':seed})
    (version_dir/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    report.setdefault('active_versions',{})[identifier]=new_version
    job['report']=report;job['options']['difficulty_rules'][difficulty_key]=rule
    _sync_chart_rows(report)
    charts=_charts_from_disk(job_id)
    _save_report_and_package(job_id,job,charts)
    shutil.copy2(directory/'malody-4k.mcz',version_dir/f'v{new_version}.mcz')
    return {'id':job_id,'key':key,'active':new_version,'versions':len(manifest['versions']),'report':report}

@app.get('/api/jobs/{job_id}/difficulties/{key}/versions')
def list_difficulty_versions(job_id: str,key: str):
    job=jobs.get(job_id)
    if not job: raise HTTPException(404,'曲包记录不存在')
    row=_chart_row(job,key)
    if row is None: raise HTTPException(404,'当前曲包没有此谱面')
    identifier=row.get('chart_id') or row['key']
    directory=_version_directory(job_id,identifier)
    manifest=_read_version_manifest(directory)
    if not manifest.get('versions'):
        fields=('notes','holds','ln_ratio','average_nps','peak_nps','lanes','density','quality_alerts','difficulty_adjustment','validation')
        return {'key':identifier,'active':1,'versions':[{'version':1,**{field:row.get(field) for field in fields},
            'seed':job.get('options',{}).get('seed')}]}
    return {'key':identifier,'active':manifest.get('active',1),'versions':manifest.get('versions',[])}

@app.post('/api/jobs/{job_id}/difficulties/{key}/restore')
def restore_difficulty_version(job_id: str,key: str,payload: dict = Body(...)):
    from .charts import validate_chart
    with lock:
        job=jobs.get(job_id)
        if not job or job.get('status')!='completed': raise HTTPException(404,'曲包记录不存在或尚未完成')
        job=copy.deepcopy(job)
    version=payload.get('version')
    if isinstance(version,bool) or not isinstance(version,int) or version<1: raise HTTPException(400,'版本号无效')
    row=_chart_row(job,key)
    if row is None: raise HTTPException(404,'目标谱面不在曲包中')
    identifier=row.get('chart_id') or row['key']
    directory=_version_directory(job_id,identifier);manifest=_read_version_manifest(directory)
    if not manifest.get('versions') and version==1:
        return {'id':job_id,'key':identifier,'active':1,'report':job['report']}
    if version not in [item.get('version') for item in manifest.get('versions',[])]: raise HTTPException(404,'谱面版本不存在')
    chart_path=directory/f'v{version}.mc';data_path=directory/f'v{version}.json'
    try:
        chart=json.loads(chart_path.read_text(encoding='utf-8'));version_data=json.loads(data_path.read_text(encoding='utf-8'))
    except (OSError,ValueError): raise HTTPException(409,'保存的谱面版本文件缺失或损坏')
    validate_chart(chart,float(job['report']['duration'])*1000)
    report=job['report']
    row.update(version_data['stats']);report.setdefault('previews',{})[identifier]=version_data['preview']
    report.setdefault('active_versions',{})[identifier]=version
    report.setdefault('quality_alerts',[])
    report['quality_alerts']=[a for a in report['quality_alerts'] if a.get('chart_id')!=identifier]+(version_data['stats'].get('quality_alerts') or [])
    _chart_disk_path(job_id,row).write_text(json.dumps(chart,ensure_ascii=False,indent=2),encoding='utf-8')
    manifest['active']=version;(directory/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    job['report']=report
    _sync_chart_rows(report)
    job['report']=report
    _save_report_and_package(job_id,job,_charts_from_disk(job_id))
    return {'id':job_id,'key':identifier,'active':version,'report':report}

@app.get('/api/jobs/{job_id}/difficulties/{key}/versions/{filename}')
def download_difficulty_version(job_id: str,key: str,filename: str):
    if not re.fullmatch(r'v[1-9][0-9]*\.(mc|mcz)',filename): raise HTTPException(404,'版本文件不存在')
    job=jobs.get(job_id)
    if not job: raise HTTPException(404,'曲包记录不存在')
    row=_chart_row(job,key)
    if row is None: raise HTTPException(404,'谱面组合不存在')
    version_dir=_version_directory(job_id,row.get('chart_id') or row['key'])
    if filename.endswith('.mcz'):
        version=int(filename[1:-4])
        chart_file=version_dir/f'v{version}.mc'
        if not chart_file.is_file() and version==1:
            chart_file=_chart_disk_path(job_id,row)
        try: chart=json.loads(chart_file.read_text(encoding='utf-8'))
        except (OSError,ValueError): raise HTTPException(404,'版本谱面文件不存在或损坏')
        data,chart_filename=_single_chart_mcz(job_id,row,chart)
        return Response(content=data,media_type='application/octet-stream',
            headers={'Content-Disposition':"attachment; filename*=UTF-8''"+
                quote(Path(row.get('filename',chart_filename)).stem+f'_v{version}.mcz')})
    path=version_dir/filename
    if not path.is_file() and filename=='v1.mc': path=_chart_disk_path(job_id,row)
    if not path.is_file(): raise HTTPException(404,'版本文件不存在')
    return FileResponse(path,media_type='application/octet-stream',filename=f'{key}-{filename}')

@app.post('/api/jobs/{job_id}/charts/{chart_id}/regenerate')
def regenerate_chart_variant(job_id: str,chart_id: str,payload: dict = Body(default={})):
    return regenerate_difficulty(job_id,chart_id,payload)

@app.get('/api/jobs/{job_id}/charts/{chart_id}/versions')
def list_chart_versions(job_id: str,chart_id: str):
    return list_difficulty_versions(job_id,chart_id)

@app.post('/api/jobs/{job_id}/charts/{chart_id}/restore')
def restore_chart_version(job_id: str,chart_id: str,payload: dict = Body(...)):
    return restore_difficulty_version(job_id,chart_id,payload)

@app.get('/api/jobs/{job_id}/charts/{chart_id}/versions/{filename}')
def download_chart_version(job_id: str,chart_id: str,filename: str):
    return download_difficulty_version(job_id,chart_id,filename)

@app.delete('/api/jobs/{job_id}')
def delete_job(job_id: str):
    if not re.fullmatch(r'[0-9a-f]{32}', job_id):
        raise HTTPException(404, '曲包记录不存在')
    with lock:
        job = get_job(job_id)
        if job.get('status') in ('queued', 'paused'):
            job.update(status='cancelled', message='已取消等待任务')
            store(job)
            return {'cancelled': job_id}
        if job.get('status') == 'running':
            raise HTTPException(409, '任务已经开始生成，暂时不能中断')
        if any(item.get('status') in ('queued', 'running', 'paused') and
               item.get('options', {}).get('_reuse_source_job_id') == job_id
               for item in jobs.values()):
            raise HTTPException(409, '其他任务正在使用这份音频，暂时不能删除')
        directory = (ROOT / 'outputs' / job_id).resolve()
        output_root = (ROOT / 'outputs').resolve()
        if directory.parent != output_root or not directory.is_dir():
            raise HTTPException(404, '曲包文件夹不存在')
        upload_name = job.get('options', {}).get('_source_upload')
        upload_path = None
        if isinstance(upload_name, str) and re.fullmatch(r'[0-9a-f]{32}\.[a-z0-9]{1,8}', upload_name):
            candidate = (ROOT / 'uploads' / upload_name).resolve()
            if candidate.parent == (ROOT / 'uploads').resolve():
                upload_path = candidate
        shutil.rmtree(directory)
        if upload_path:
            upload_path.unlink(missing_ok=True)
        del jobs[job_id]
    return {'deleted': job_id}

@app.get('/api/jobs/{job_id}/download')
def download(job_id: str):
    job = get_job(job_id)
    if job['status'] != 'completed':
        raise HTTPException(409, '曲包尚未生成')
    return FileResponse(ROOT / 'outputs' / job_id / 'malody-4k.mcz', media_type='application/octet-stream',
                        filename=job.get('report',{}).get('download_name') or f'{job["title"]}-4K.mcz')

def _single_chart_mcz(job_id, row, chart=None):
    directory=ROOT/'outputs'/job_id
    chart_path=_chart_disk_path(job_id,row)
    if chart is None:
        try: chart=json.loads(chart_path.read_text(encoding='utf-8'))
        except (OSError,ValueError): raise HTTPException(404,'谱面文件不存在或损坏')
    audio=directory/'0'/'audio.ogg'
    if not audio.is_file(): raise HTTPException(404,'曲包音频不存在')
    filename=row.get('filename')
    if not isinstance(filename,str) or Path(filename).name!=filename or not filename.endswith('.mc'):
        filename=f'{row.get("chart_id") or row["key"]}.mc'
    content=BytesIO()
    with zipfile.ZipFile(content,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('0/',b'')
        archive.writestr(f'0/{filename}',json.dumps(chart,ensure_ascii=False,indent=2).encode('utf-8'))
        archive.write(audio,'0/audio.ogg')
        background=directory/'0'/'background.jpg'
        if background.is_file() and chart.get('meta',{}).get('background')=='background.jpg':
            archive.write(background,'0/background.jpg')
    return content.getvalue(),filename

@app.get('/api/jobs/{job_id}/charts/{chart_id}/download')
def download_chart_pack(job_id: str, chart_id: str):
    job=get_job(job_id)
    if job.get('status')!='completed': raise HTTPException(409,'谱面尚未生成')
    row=_chart_row(job,chart_id)
    if row is None: raise HTTPException(404,'谱面组合不存在')
    data,filename=_single_chart_mcz(job_id,row)
    from .naming import chart_stem
    name=chart_stem(job['title'],job.get('options',{}).get('engine','mug'),
                    row.get('pattern','balanced'),row.get('difficulty',row['key']))+'.mcz'
    return Response(content=data,media_type='application/octet-stream',
        headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(name)})

@app.get('/api/jobs/{job_id}/charts/{chart_id}')
def chart_for_arcade(job_id: str, chart_id: str):
    job=get_job(job_id)
    if job.get('status')!='completed': raise HTTPException(409,'谱面尚未生成')
    row=_chart_row(job,chart_id)
    if row is None: raise HTTPException(404,'谱面组合不存在')
    path=_chart_disk_path(job_id,row)
    try: chart=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError): raise HTTPException(404,'谱面文件不存在或损坏')
    return {'chart_id':row.get('chart_id') or row['key'],'pattern':row.get('pattern','balanced'),
        'difficulty':row.get('difficulty',row['key']),'filename':row.get('filename',path.name),
        'title':job['title'],'artist':job['artist'],'engine':job.get('report',{}).get('engine'),
        'audio_url':f'/api/jobs/{job_id}/files/audio.ogg','chart':chart}

@app.get('/api/jobs/{job_id}/files/{filename}')
def artifact(job_id: str, filename: str, request: Request):
    job = get_job(job_id)
    if job['status'] != 'completed':
        if job['status'] == 'failed' and filename == 'audio.ogg':
            path = ROOT / 'outputs' / job_id / '0' / 'audio.ogg'
            if not path.is_file():
                raise HTTPException(404, '转换后的音频不存在')
            return audio_response(path, request.headers.get('range'))
        raise HTTPException(409, '任务尚未完成')
    if filename == 'report.json':
        path = ROOT / 'outputs' / job_id / filename
    elif filename in ('audio.ogg', 'background.jpg') or any(
            row.get('filename')==filename for row in _chart_rows(job)) or filename in [key + '.mc' for key in PRESETS]:
        path = ROOT / 'outputs' / job_id / '0' / filename
    else:
        raise HTTPException(404, '文件不存在')
    if not path.is_file():
        raise HTTPException(404, '文件不存在')
    if filename == 'audio.ogg':
        return audio_response(path, request.headers.get('range'))
    return FileResponse(path, media_type='image/jpeg' if filename.endswith('.jpg') else 'application/json',
                        filename=filename)

def audio_response(path, range_header=None):
    # OGG players read the tail to determine duration and seek to a page boundary.
    size = path.stat().st_size
    start, end, status = 0, size - 1, 200
    headers = {'Accept-Ranges': 'bytes'}
    if range_header:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header)
        if not match or not any(match.groups()):
            return Response(status_code=416, headers={'Content-Range': f'bytes */{size}'})
        first, last = match.groups()
        if first:
            start, end = int(first), min(int(last), size - 1) if last else size - 1
        else:
            start = max(0, size - int(last))
        if start >= size or end < start:
            return Response(status_code=416, headers={'Content-Range': f'bytes */{size}'})
        status = 206
        headers['Content-Range'] = f'bytes {start}-{end}/{size}'
    headers['Content-Length'] = str(end - start + 1)
    def stream():
        with path.open('rb') as audio:
            audio.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = audio.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
    return StreamingResponse(stream(), status_code=status, media_type='audio/wav' if path.suffix.lower()=='.wav' else 'audio/ogg', headers=headers)

@app.get('/')
def home():
    return FileResponse(ROOT / 'web' / 'index.html')

@app.get('/docs/separation-upgrade-guide')
def separation_setup_guide():
    import html
    text=(ROOT/'docs'/'separation-upgrade-guide.md').read_text(encoding='utf-8')
    content='<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>分离模型 · 使用与安装</title><style>body{background:#eff7f7;color:#153847;font:15px/1.8 system-ui;margin:32px auto;padding:0 20px;max-width:860px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}a{color:#167c8c}</style><a href="/">← 返回星轨制谱台</a><pre>'+html.escape(text)+'</pre></html>'
    return Response(content,media_type='text/html')

app.mount('/static', StaticFiles(directory=ROOT / 'web'), name='static')

from .advanced_api import router as advanced_router
app.include_router(advanced_router)

@app.get('/api/music/search')
def music_search(q: str, cursor: int = Query(0, ge=0, le=500), limit: int = Query(20, ge=1, le=20),
                 min_duration: int = Query(5, ge=5, le=600), max_duration: int = Query(600, ge=5, le=600)):
    from .music import search_page
    try:
        return search_page(q, cursor, limit, min_duration, max_duration)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, str(exc))

@app.post('/api/batches')
def create_batch(payload: dict = Body(...)):
    from . import music
    ids = payload.get('video_ids')
    snapshot = payload.get('settings')
    if not isinstance(ids, list) or not 1 <= len(ids) <= 20:
        raise HTTPException(400, '每批请选择 1 至 20 首 YouTube 音乐')
    if not isinstance(snapshot, dict):
        raise HTTPException(400, '缺少本批生成设置快照')
    unique_ids = []
    for video_id in ids:
        try:
            video_id = music.valid_id(video_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if video_id not in unique_ids:
            unique_ids.append(video_id)
    try:
        difficulties = snapshot.get('difficulties', ['easy', 'medium', 'hard'])
        if isinstance(difficulties, str):
            difficulties = json.loads(difficulties)
        rules = snapshot.get('difficulty_rules', {})
        if isinstance(rules, str):
            rules = json.loads(rules)
        def number(key, default, convert=float):
            value = snapshot.get(key, default)
            return None if key == 'bpm' and value in (None, '') else convert(value)
        common = settings('批量任务', 'YouTube', json.dumps(difficulties),
            number('ln_ratio', .15), number('steps', 50, int), number('seed', 20261001, int),
            number('bpm', None), str(snapshot.get('engine', 'mug')),
            difficulty_rules=json.dumps(rules), pattern=str(snapshot.get('pattern', 'balanced')),
            patterns=snapshot.get('patterns'), dynamic_enabled=snapshot.get('dynamic_enabled',True),
            pattern_strength=number('pattern_strength', 20, int),
            mug_difficulty=number('mug_difficulty', 8), mug_style=str(snapshot.get('mug_style', 'ranked')),
            mug_guidance=number('mug_guidance', 1.5), mug_eta=number('mug_eta', 0),
            v32_difficulty=number('v32_difficulty', 8), v32_temperature=number('v32_temperature', .9),
            v32_top_p=number('v32_top_p', .9),
            v32_column_temperature=number('v32_column_temperature', .8),
            v32_cfg_scale=number('v32_cfg_scale', 1), v32_year=number('v32_year', 2024, int),
            v32_descriptors=str(snapshot.get('v32_descriptors', '')),
            v32_negative_descriptors=str(snapshot.get('v32_negative_descriptors', '')))
        ensure_engine(common)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(400, '批量生成参数无效，请检查难度和模型设置')
    with music.lock:
        tracks = {video_id: dict(music.candidates.get(video_id) or music.tracks.get(video_id) or {})
                  for video_id in unique_ids}
    missing = [video_id for video_id, item in tracks.items() if not item]
    if missing:
        raise HTTPException(409, '部分搜索结果已过期，请重新搜索后再加入批次')
    result = []
    for video_id in unique_ids:
        item = tracks[video_id]
        options = dict(common, title=str(item.get('title') or 'YouTube 音乐')[:120],
                       artist=str(item.get('artist') or 'Unknown')[:120],
                       source=music.url_for(video_id), artwork_video_id=video_id,
                       batch_size=len(unique_ids))
        result.append(enqueue_job(options, {'type': 'youtube', 'video_id': video_id})['id'])
    return {'ids': result, 'count': len(result), 'status': 'queued'}

@app.get('/api/music')
def music_library():
    from .music import tracks, lock as music_lock
    with music_lock:
        return [dict(track) for track in tracks.values()][::-1]

@app.post('/api/music/{video_id}/import')
def music_import(video_id: str):
    from .music import begin
    try:
        return begin(video_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

@app.get('/api/music/{video_id}')
def music_status(video_id: str):
    from .music import get
    try:
        return get(video_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))

@app.get('/api/music/{video_id}/audio')
def music_audio(video_id: str, request: Request):
    from .music import ready_audio
    try:
        path, _ = ready_audio(video_id)
        return audio_response(path, request.headers.get('range'))
    except ValueError as exc:
        raise HTTPException(409, str(exc))

@app.get('/api/music/{video_id}/download')
def music_download(video_id: str):
    from .music import ready_audio
    try:
        path, track = ready_audio(video_id)
        return FileResponse(path, media_type='audio/ogg', filename=track['title'] + '.ogg')
    except ValueError as exc:
        raise HTTPException(409, str(exc))

@app.post('/api/music/{video_id}/generate')
def music_generate(video_id: str, title: str = Form(...), artist: str = Form(''),
                   difficulties: str = Form('["easy","normal","hard"]'), ln_ratio: float = Form(.15),
                   steps: int = Form(50), seed: int = Form(20261001), bpm: float | None = Form(None),
                   engine: str = Form('mug'), difficulty_rules: str = Form('{}'),
                   pattern: str = Form('balanced'), pattern_strength: int = Form(20),
                   mug_difficulty: float = Form(8), mug_style: str = Form('ranked'),
                   mug_guidance: float = Form(1.5), mug_eta: float = Form(0),
                   v32_difficulty: float = Form(8), v32_temperature: float = Form(.9),
                   v32_top_p: float = Form(.9), v32_column_temperature: float = Form(.8),
                   v32_cfg_scale: float = Form(1), v32_year: int = Form(2024),
                   v32_descriptors: str = Form(''), v32_negative_descriptors: str = Form(''),
                   patterns: str = Form(''), dynamic_enabled: bool = Form(True)):
    from .music import ready_audio
    options = settings(title, artist, difficulties, ln_ratio, steps, seed, bpm, engine,
                       difficulty_rules=difficulty_rules, pattern=pattern, pattern_strength=pattern_strength,
                       mug_difficulty=mug_difficulty, mug_style=mug_style, mug_guidance=mug_guidance, mug_eta=mug_eta,
                       v32_difficulty=v32_difficulty, v32_temperature=v32_temperature, v32_top_p=v32_top_p,
                       v32_column_temperature=v32_column_temperature, v32_cfg_scale=v32_cfg_scale,
                       v32_year=v32_year, v32_descriptors=v32_descriptors,
                       v32_negative_descriptors=v32_negative_descriptors, patterns=patterns, dynamic_enabled=dynamic_enabled)
    ensure_engine(options)
    try:
        source, track = ready_audio(video_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    options['source'] = track['url']
    options['artwork_video_id'] = video_id
    return new_job(source, options)
