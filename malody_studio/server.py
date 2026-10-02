from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import traceback
import uuid
import re
import math
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Request, Query
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from .paths import ROOT, PRESETS, WEIGHTS
from .difficulty import PATTERN_CHOICES, V32_PATTERN_TAGS

app = FastAPI(title='Malody Chart Forge')
pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='chart-generator')
jobs = {}
lock = threading.Lock()
for directory in (ROOT / 'outputs').iterdir():
    state = directory / 'job.json'
    if state.is_file():
        try:
            job = json.loads(state.read_text(encoding='utf-8'))
            if job['status'] not in ('completed', 'failed'):
                job.update(status='failed', message='服务已重启，请重新生成', error='任务被服务重启中断')
            jobs[job['id']] = job
        except (ValueError, KeyError):
            continue

def store(job):
    path = ROOT / 'outputs' / job['id'] / 'job.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)

def get_job(job_id):
    if job_id not in jobs:
        raise HTTPException(404, '任务不存在')
    return jobs[job_id]

def worker(job_id, source, options):
    directory = ROOT / 'outputs' / job_id
    def progress(message, percent):
        with lock:
            jobs[job_id].update(status='running', message=message, progress=round(percent, 1))
            store(jobs[job_id])
    try:
        from .pipeline import run
        progress('准备生成', 1)
        report, archive = run(source, directory, options, progress)
        with lock:
            jobs[job_id].update(status='completed', message='曲包已生成', progress=100,
                                report=report, download=f'/api/jobs/{job_id}/download')
            store(jobs[job_id])
    except Exception as exc:
        (ROOT / 'logs' / f'{job_id}.log').write_text(traceback.format_exc(), encoding='utf-8')
        with lock:
            jobs[job_id].update(status='failed', message='生成失败', error=str(exc))
            store(jobs[job_id])
        # Drop possibly corrupted GPU state so a failed request does not poison later jobs.
        try:
            from .pipeline import engine
            engine.model = None
            import gc
            import torch
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

def new_job(source, options):
    with lock:
        if sum(j['status'] in ('queued', 'running') for j in jobs.values()) >= 5:
            raise HTTPException(429, '任务队列已满，请等待当前任务完成')
        job_id = uuid.uuid4().hex
        directory = ROOT / 'outputs' / job_id
        directory.mkdir()
        job = {'id': job_id, 'title': options['title'], 'artist': options['artist'],
               'status': 'queued', 'message': '等待生成', 'progress': 0,
               'created': datetime.now(timezone.utc).isoformat(), 'options': options}
        jobs[job_id] = job
        store(job)
    pool.submit(worker, job_id, source, options)
    return {'id': job_id}

def settings(title, artist, difficulties, ln_ratio, steps, seed, bpm, engine='mug', artwork_url='',
             difficulty_rules='{}', pattern='balanced', pattern_strength=20,
             mug_difficulty=8, mug_style='ranked', mug_guidance=1.5, mug_eta=0,
             v32_difficulty=8, v32_temperature=.9, v32_top_p=.9,
             v32_column_temperature=.8, v32_cfg_scale=1, v32_year=2024,
             v32_descriptors='', v32_negative_descriptors=''):
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
    pattern_tag = V32_PATTERN_TAGS.get(pattern)
    if pattern_tag and pattern_tag not in positive_tags:
        positive_tags.append(pattern_tag)
    if len(positive_tags) > 4:
        raise HTTPException(400, '键型倾向会占用一个 V32 风格标签位置，请将风格标签减少到 3 个')
    if negative_tags and (v32_cfg_scale <= 1 or len(negative_tags) != len(positive_tags)):
        raise HTTPException(400, 'V32 排除标签需在条件强度大于 1 时使用，且数量须与风格标签一致')
    title, artist = title.strip(), artist.strip()
    if not title or len(title) > 120 or len(artist) > 120:
        raise HTTPException(400, '请填写曲名（不超过 120 字）')
    from .artwork import video_id_from_url
    try:
        artwork_id = video_id_from_url(artwork_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {'title': title, 'artist': artist or 'Unknown', 'difficulties': selected, 'artwork_video_id': artwork_id,
            'ln_ratio': ln_ratio, 'steps': steps, 'seed': seed, 'bpm': bpm, 'engine': engine,
            'difficulty_rules': normalized_rules, 'pattern': pattern, 'pattern_strength': int(pattern_strength),
            'mug_difficulty': float(mug_difficulty), 'mug_style': mug_style,
            'mug_guidance': float(mug_guidance), 'mug_eta': float(mug_eta),
            'v32_difficulty': float(v32_difficulty), 'v32_temperature': float(v32_temperature),
            'v32_top_p': float(v32_top_p), 'v32_column_temperature': float(v32_column_temperature),
            'v32_cfg_scale': float(v32_cfg_scale), 'v32_year': int(v32_year),
            'v32_descriptors': positive_tags, 'v32_negative_descriptors': negative_tags}

def ensure_engine(options):
    if options['engine'] == 'v32':
        from .mapperatorinator import ready
        available = ready()
    else:
        available = WEIGHTS.is_file() and WEIGHTS.stat().st_size == 1839231053
    if not available:
        raise HTTPException(503, '所选模型尚未完成部署，请选择其他引擎')

@app.get('/api/health')
def health():
    import torch
    from .mapperatorinator import ready as v32_ready
    mug_ready = WEIGHTS.is_file() and WEIGHTS.stat().st_size == 1839231053
    return {'ready': WEIGHTS.is_file() and WEIGHTS.stat().st_size == 1839231053,
            'engine': 'MuG Diffusion v1.0.0', 'gpu': torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU',
            'reference': (ROOT / 'uploads' / 'reference-sirius.m4a').is_file(),
            'engines': {'mug': {'label': 'MuG Diffusion', 'ready': mug_ready},
                        'v32': {'label': 'Mapperatorinator V32（实验）', 'ready': v32_ready()}},
            'presets': PRESETS}

@app.get('/api/jobs')
def list_jobs():
    with lock:
        return [{'id': j['id'], 'title': j['title'], 'status': j['status'], 'created': j['created']}
                for j in sorted(jobs.values(), key=lambda j: j['created'], reverse=True)[:20]]

@app.get('/api/history')
def paginated_history(page: int = Query(1, ge=1), page_size: int = Query(6, ge=1, le=24), q: str = Query('', max_length=200)):
    with lock:
        matches = sorted((j for j in jobs.values() if q.casefold() in j['title'].casefold()),
                         key=lambda j: j['created'], reverse=True)
        total = len(matches)
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        items = []
        for job in matches[(page-1)*page_size:page*page_size]:
            report = job.get('report', {})
            artwork = report.get('artwork', {})
            items.append({k: job[k] for k in ('id', 'title', 'artist', 'status', 'created')})
            items[-1].update(engine=job['options'].get('engine', 'mug'),
                             difficulties=[d['label'] for d in report.get('difficulties', [])],
                             cover=f"/api/jobs/{job['id']}/files/background.jpg" if artwork.get('status') == 'ready' else None)
            from .artwork import video_id_from_url
            try:
                video_id = job['options'].get('artwork_video_id') or video_id_from_url(job['options'].get('source', ''))
            except ValueError:
                video_id = None
            items[-1]['thumbnail'] = f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg' if video_id else None
        return {'items': items, 'total': total, 'total_all': len(jobs), 'page': page, 'pages': pages, 'page_size': page_size}

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
                 v32_descriptors: str = Form(''), v32_negative_descriptors: str = Form('')):
    options = settings(title, artist, difficulties, ln_ratio, steps, seed, bpm, engine, artwork_url,
                       difficulty_rules, pattern, pattern_strength, mug_difficulty, mug_style,
                       mug_guidance, mug_eta, v32_difficulty, v32_temperature, v32_top_p,
                       v32_column_temperature, v32_cfg_scale, v32_year,
                       v32_descriptors, v32_negative_descriptors)
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
              v32_descriptors: str = Form(''), v32_negative_descriptors: str = Form('')):
    source = ROOT / 'uploads' / 'reference-sirius.m4a'
    if not source.is_file():
        raise HTTPException(404, '参考音轨尚未准备完成')
    options = settings('シリウスの心臓（天狼星的心脏）', 'ヰ世界情緒', difficulties, ln_ratio, steps, seed, bpm, engine,
                       difficulty_rules=difficulty_rules, pattern=pattern, pattern_strength=pattern_strength,
                       mug_difficulty=mug_difficulty, mug_style=mug_style, mug_guidance=mug_guidance, mug_eta=mug_eta,
                       v32_difficulty=v32_difficulty, v32_temperature=v32_temperature, v32_top_p=v32_top_p,
                       v32_column_temperature=v32_column_temperature, v32_cfg_scale=v32_cfg_scale,
                       v32_year=v32_year, v32_descriptors=v32_descriptors,
                       v32_negative_descriptors=v32_negative_descriptors)
    ensure_engine(options)
    options['source'] = 'https://www.youtube.com/watch?v=UKZt1vq8bKI'
    options['artwork_video_id'] = 'UKZt1vq8bKI'
    return new_job(source, options)

@app.get('/api/jobs/{job_id}/download')
def download(job_id: str):
    job = get_job(job_id)
    if job['status'] != 'completed':
        raise HTTPException(409, '曲包尚未生成')
    return FileResponse(ROOT / 'outputs' / job_id / 'malody-4k.mcz', media_type='application/octet-stream',
                        filename=f'{job["title"]}-4K.mcz')

@app.get('/api/jobs/{job_id}/files/{filename}')
def artifact(job_id: str, filename: str, request: Request):
    job = get_job(job_id)
    if job['status'] != 'completed':
        raise HTTPException(409, '任务尚未完成')
    if filename == 'report.json':
        path = ROOT / 'outputs' / job_id / filename
    elif filename in ('audio.ogg', 'background.jpg', 'normal.mc') or filename in [key + '.mc' for key in PRESETS]:
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
    return StreamingResponse(stream(), status_code=status, media_type='audio/ogg', headers=headers)

@app.get('/')
def home():
    return FileResponse(ROOT / 'web' / 'index.html')

app.mount('/static', StaticFiles(directory=ROOT / 'web'), name='static')

@app.get('/api/music/search')
def music_search(q: str):
    from .music import search
    try:
        return {'results': search(q), 'source': 'YouTube'}
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, str(exc))

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
                   v32_descriptors: str = Form(''), v32_negative_descriptors: str = Form('')):
    from .music import ready_audio
    options = settings(title, artist, difficulties, ln_ratio, steps, seed, bpm, engine,
                       difficulty_rules=difficulty_rules, pattern=pattern, pattern_strength=pattern_strength,
                       mug_difficulty=mug_difficulty, mug_style=mug_style, mug_guidance=mug_guidance, mug_eta=mug_eta,
                       v32_difficulty=v32_difficulty, v32_temperature=v32_temperature, v32_top_p=v32_top_p,
                       v32_column_temperature=v32_column_temperature, v32_cfg_scale=v32_cfg_scale,
                       v32_year=v32_year, v32_descriptors=v32_descriptors,
                       v32_negative_descriptors=v32_negative_descriptors)
    ensure_engine(options)
    try:
        source, track = ready_audio(video_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    options['source'] = track['url']
    options['artwork_video_id'] = video_id
    return new_job(source, options)
