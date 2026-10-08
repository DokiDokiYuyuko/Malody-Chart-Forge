"""Project-local, immutable vocal/accompaniment separation on the source PCM clock."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

import numpy as np
import soundfile as sf

from .advanced import SR, atomic, read
from .paths import ROOT
from . import separation_models as registry
from .deployment_integrity import registry_inference_hash

VERSION = 'demucs-4.0.1-adapter-v1'
PYTHON = ROOT / 'runtime' / 'separation-venv' / 'Scripts' / 'python.exe'
MODEL_ROOT = ROOT / 'models' / 'separation' / 'demucs-4.0.1'
ROLES = ('vocals', 'accompaniment')
DEFAULTS = registry.DEMUC_DEFAULTS
PARAMETER_LIMITS = registry.DEMUC_LIMITS
ROFORMER_VERSION = registry.ROFORMER_VERSION
ROFORMER_MODEL_ROOT = ROOT / 'models' / 'separation' / 'melband-roformer-kim'
ROFORMER_PYTHON = ROOT / 'runtime' / 'roformer-venv' / 'Scripts' / 'python.exe'
_TQDM_PROGRESS = re.compile(r'(?<!\d)(\d{1,3})%\|')


def model_progress_from_log(text):
    for line in reversed((text or '').splitlines()):
        try:
            event = json.loads(line)
            fraction = event.get('fraction') if event.get('event') == 'progress' else None
            if isinstance(fraction, (int, float)) and np.isfinite(fraction) and 0 <= fraction <= 1:
                return int(fraction * 100)
        except (ValueError, TypeError, AttributeError):
            pass
    values=[int(value) for value in _TQDM_PROGRESS.findall(text or '') if 0<=int(value)<=100]
    return values[-1] if values else None


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def pcm_hash(data):
    data = np.asarray(data, dtype='<f4', order='C')
    return hashlib.sha256(data.tobytes(order='C')).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def validated_settings(value=None):
    return registry.validated_settings(value)


def get_separation_models(include_status=False):
    models = registry.catalog()
    if include_status:
        for model, definition in models.items():
            definition.update(deployment_status(model))
    return models


model_catalog = get_separation_models


def _model_paths(model):
    registry.adapter_version(model)
    if model == registry.ROFORMER_MODEL:
        return ROFORMER_MODEL_ROOT, ROFORMER_PYTHON, ROOT / 'tools' / 'roformer_worker.py'
    return MODEL_ROOT, PYTHON, ROOT / 'tools' / 'separation_worker.py'


def deployment_status(model='htdemucs'):
    try:
        manifest = deployment(model)
        verified = model_inference_verified(manifest, model)
        return {'assets_installed': True, 'inference_verified': verified, 'ready': verified, 'error': None}
    except (OSError, ValueError, KeyError, RuntimeError, TypeError) as error:
        return {'assets_installed': False, 'inference_verified': False, 'ready': False, 'error': str(error)}


def deployment(model='htdemucs'):
    model_root, python, worker = _model_paths(model)
    manifest = read(model_root / 'manifest.json')
    if manifest.get('adapter_version') != registry.adapter_version(model):
        raise RuntimeError('分离环境清单版本不匹配，请运行项目分离安装工具')
    required = manifest['models'].get(model)
    if not required:
        raise RuntimeError('分离模型尚未部署：' + model)
    for name in required:
        record = manifest['files'][name]
        path = (model_root / name).resolve()
        if path.parent != model_root.resolve():
            raise RuntimeError('分离模型权重路径无效：' + name)
        if not path.is_file() or path.stat().st_size != record['bytes'] or file_hash(path) != record['sha256']:
            raise RuntimeError('分离模型权重缺失或校验失败：' + name)
    if not python.is_file() or not worker.is_file():
        raise RuntimeError('独立分离运行环境尚未部署')
    if model == registry.ROFORMER_MODEL:
        lock = ROOT / 'runtime' / 'roformer-requirements-lock.txt'
        if not lock.is_file() or file_hash(lock) != manifest.get('dependency_lock_sha256'):
            raise RuntimeError('RoFormer 独立依赖清单校验失败')
        if not manifest.get('code_files'):
            raise RuntimeError('RoFormer 固定代码清单缺失')
        for name, record in manifest['code_files'].items():
            path = (ROOT / name).resolve()
            if not path.is_relative_to(ROOT.resolve()) or not path.is_file():
                raise RuntimeError('RoFormer 固定代码校验失败：' + name)
            if name == 'malody_studio/separation_models.py' and manifest.get('registry_inference_sha256'):
                valid = registry_inference_hash(path) == manifest['registry_inference_sha256']
            else:
                valid = file_hash(path) == record['sha256']
            if not valid:
                raise RuntimeError('RoFormer 固定代码校验失败：' + name)
    return manifest


def ready(model='htdemucs'):
    try:
        manifest = deployment(model)
        return model_inference_verified(manifest, model)
    except (OSError, ValueError, KeyError, RuntimeError):
        return False


def model_inference_verified(manifest, model):
    """Use per-model verification while remaining compatible with old manifests."""
    records=manifest.get('verified_models',{})
    if isinstance(records,dict) and isinstance(records.get(model),dict):
        return records[model].get('status')=='passed'
    # Earlier manifests could record only one model. Preserve that proof for
    # the model actually tested; downloading FT must not imply it was tested.
    return bool(manifest.get('inference_verified') and manifest.get('verification',{}).get('model')==model)


def _record_model_inference(model, stems_manifest):
    """Persist a successful, integrity-checked inference as model-specific evidence."""
    path=_model_paths(model)[0]/'manifest.json'
    try:
        manifest=read(path)
        records=manifest.get('verified_models',{})
        if not isinstance(records,dict):records={}
        records=dict(records)
        evidence={'status':'passed','model':model,'source_pcm_sha':stems_manifest.get('source_pcm_sha'),
                  'frames':stems_manifest.get('frame_count'),'device':stems_manifest.get('device'),
                  'report':'successful integrity-checked separation cache'}
        performance=stems_manifest.get('performance',{})
        for key in ('wall_seconds','cuda_peak_allocated_bytes','cuda_peak_reserved_bytes','cuda_free_min_bytes'):
            if key in performance:evidence[key]=performance[key]
        records[model]=evidence
        manifest['verified_models']=records
        manifest['inference_verified']=True
        if model=='htdemucs':manifest['verification']=evidence
        atomic(path,manifest)
    except (OSError,ValueError,KeyError,TypeError):
        # The already-validated stem cache is the product. Registry telemetry
        # must never turn a successful separation into a failed user task.
        return False
    return True


def validate_manifest(directory, manifest, expected=None):
    directory = Path(directory).resolve()
    version = manifest.get('adapter_version')
    settings = manifest.get('settings')
    valid_version = version in (VERSION, ROFORMER_VERSION)
    if settings is not None:
        valid_version = valid_version and version == registry.adapter_version(validated_settings(settings)['model'])
    if not valid_version or manifest.get('sample_rate') != SR or manifest.get('origin_sample') != 0:
        raise ValueError('分离缓存采样时钟或版本不一致')
    if expected and manifest.get('recipe_hash') != expected:
        raise ValueError('分离缓存配方不一致')
    rows = manifest.get('stems', [])
    if sorted(x.get('role', '') for x in rows) != sorted(ROLES):
        raise ValueError('分离缓存缺少人声或伴奏')
    for row in rows:
        path = (directory / row['file']).resolve()
        if path.parent != directory or not path.is_file():
            raise ValueError('分离缓存音频路径无效')
        info = sf.info(path)
        if info.frames != manifest['frame_count'] or info.samplerate != SR or info.channels != 2 or info.subtype != 'FLOAT':
            raise ValueError('分离结果帧数、声道或编码不匹配')
        data, _ = sf.read(path, dtype='float32', always_2d=True)
        if not np.isfinite(data).all() or pcm_hash(data) != row['pcm_sha'] or file_hash(path) != row['file_sha256']:
            raise ValueError('分离缓存音频完整性校验失败')
        row['path'] = str(path)
    return manifest


def ensure_stems(source, directory=None, settings=None, progress=lambda *_: None, *, cache_scope=None):
    """Synchronous stage; no nested queue job and no generated playback replacement."""
    from .workflow_log import stage, event, file_identity, current_context
    options = validated_settings(settings)
    with stage('separation.read_and_verify_original_pcm', source=file_identity(source,hash_file=True),
               expected_model=options['model']):
        data, rate = sf.read(source, dtype='float32', always_2d=True)
    if rate != SR or data.shape[1] != 2 or not len(data) or not np.isfinite(data).all():
        raise ValueError('分离输入须为有限双声道 44100 Hz 源 PCM')
    model = deployment(options['model'])
    model_root, python, worker = _model_paths(options['model'])
    source_sha = pcm_hash(data)
    deployed = {k: model[k] for k in ('adapter_version', 'code_version', 'models', 'files', 'environment', 'dependency_lock_sha256')}
    # Installing the optional ft model does not invalidate default model caches.
    deployed['models'] = {options['model']: model['models'][options['model']]}
    deployed['files'] = {name: model['files'][name] for name in model['models'][options['model']]}
    if options['model'] == registry.ROFORMER_MODEL:
        deployed['code_files'] = model.get('code_files', {})
    recipe = {'adapter_version': registry.adapter_version(options['model']), 'source_pcm_sha': source_sha, 'sample_rate': SR,
              'frame_count': len(data), 'origin_sample': 0, 'settings': options,
              'deployment_hash': canonical_hash(deployed)}
    if cache_scope is not None:
        if directory is None or not isinstance(cache_scope, dict) or cache_scope.get('type') != 'separation_trial':
            raise ValueError('试听试跑须提供独立目录和试跑范围')
        # Freeze caller-owned state before hashing; the integer parent offset is
        # provenance and cannot be confused with the local context PCM origin.
        recipe['cache_scope'] = json.loads(json.dumps(cache_scope, allow_nan=False))
    key = canonical_hash(recipe)
    cache = ((Path(directory).resolve() / 'separation') if cache_scope is not None else (ROOT / 'cache' / 'separation')) / key
    cache.mkdir(parents=True, exist_ok=True)
    def cached_result():
        with stage('separation.validate_cached_stems', cache=cache, recipe_hash=key):
            manifest = validate_manifest(cache, read(cache / 'manifest.json'), key)
        if manifest.get('id') != key or any(manifest.get(field) != value for field,value in recipe.items()):
            raise ValueError('分离缓存完整原曲、配置或部署来源与配方不匹配')
        _record_model_inference(options['model'],manifest)
        event('separation_result','separation.cache_hit',manifest_id=manifest.get('id'),
              stems=manifest.get('stems'),performance=manifest.get('performance'),device=manifest.get('device'))
        return manifest
    # Atomic mkdir protects a shared source even outside the parent GPU lease.
    lease = cache / 'writer.lock'
    deadline = time.monotonic() + 7200
    while True:
        if (cache / 'manifest.json').is_file():
            try:
                return cached_result()
            except (OSError, ValueError, KeyError):
                pass
        try:
            lease.mkdir()
            break
        except FileExistsError:
            owner = lease / 'owner.json'
            try:
                state = read(owner)
                owners = [state['pid']] + ([state['worker_pid']] if state.get('worker_pid') else [])
                alive = any(('"' + str(pid) + '"') in subprocess.run(
                    ['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'], capture_output=True, text=True).stdout for pid in owners)
                if not alive:
                    owner.unlink(missing_ok=True)
                    lease.rmdir()
                    continue
            except (OSError, KeyError, ValueError):
                if time.time() - lease.stat().st_mtime > 30 and not owner.exists():
                    try:
                        lease.rmdir()
                        continue
                    except OSError:
                        pass
            if time.monotonic() > deadline:
                raise RuntimeError('等待共享分离缓存超时')
            time.sleep(.5)
    atomic(lease / 'owner.json', {'pid': os.getpid()})
    try:
        if (cache / 'manifest.json').is_file():
            try:
                return cached_result()
            except (OSError, ValueError, KeyError):
                pass
        request = {**recipe, 'recipe_hash': key, 'source': str(Path(source).resolve()),
                   'directory': str(cache), 'model_root': str(model_root),
                   'workflow_trace': current_context()}
        atomic(cache / 'request.json', request)
        env = os.environ.copy()
        env.update(PYTHONUTF8='1', TEMP=str(ROOT / 'cache'), TMP=str(ROOT / 'cache'),
                   TORCH_HOME=str(ROOT / 'cache' / 'torch'), HF_HUB_OFFLINE='1')
        log_path = ROOT / 'logs' / ('separation-' + key[:16] + '.log')
        progress('分离人声与伴奏', 5)
        from .resident import external_gpu
        with stage('separation.gpu_model_inference', model=options['model'], python=python, worker=worker,
                   recipe_hash=key, cache=cache, source_pcm_sha=source_sha):
            with external_gpu(progress), log_path.open('w', encoding='utf-8') as log:
                with subprocess.Popen([str(python), '-u', str(worker), str(cache / 'request.json')],
                                      cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                      creationflags=getattr(subprocess,'CREATE_NO_WINDOW', 0)) as process:
                    atomic(lease / 'owner.json', {'pid': os.getpid(), 'worker_pid': process.pid})
                    deadline=time.monotonic()+7200;last_reported=5
                    while process.poll() is None:
                        try:
                            with log_path.open('rb') as stream:
                                stream.seek(max(0,log_path.stat().st_size-16384))
                                tail=stream.read().decode('utf-8','replace')
                            percent=model_progress_from_log(tail)
                            if percent is not None:
                                overall=5+percent*.87
                                if overall>=last_reported+1:
                                    progress(f'分离模型处理中 · {percent}%',overall);last_reported=overall
                        except OSError:
                            pass
                        if time.monotonic()>deadline:
                            process.kill();process.wait()
                            raise RuntimeError('人声分离超过两小时；已释放工作进程，请缩短或重试')
                        time.sleep(.5)
                    returncode=process.returncode
        if returncode:
            tail=log_path.read_text(encoding='utf-8')[-2000:]
            event('child_process_failure','separation.gpu_model_inference',returncode=returncode,
                  worker_log=file_identity(log_path),log_tail=tail)
            raise RuntimeError('人声分离失败；原曲与已有谱面已保留。' + tail)
        result=cached_result()
        event('separation_result','separation.gpu_model_inference',manifest_id=result.get('id'),
              performance=result.get('performance'),device=result.get('device'),stems=result.get('stems'))
        return result
    finally:
        (lease / 'owner.json').unlink(missing_ok=True)
        lease.rmdir()


def resolve_source(store, pid, source_id='mix'):
    """Return a validated source descriptor for local spectrum and playback tools."""
    p = store.load(pid)
    if source_id in ('mix', 'original', p.get('source_sha256')):
        path = store.directory(pid) / 'source.wav'
        if not path.resolve().is_relative_to(store.directory(pid).resolve()) or not path.is_file():raise ValueError('项目原曲不存在或音源路径无效')
        data, rate = sf.read(path, dtype='float32', always_2d=True)
        if rate != SR or len(data) != p['samples'] or not np.isfinite(data).all() or pcm_hash(data) != p.get('source_pcm_sha256'):
            raise ValueError('原曲采样时钟或完整性与项目不匹配')
        return {'source_id': 'original', 'source_role': 'mix', 'path': str(store.directory(pid) / 'source.wav'),
                'parent_source_id': p.get('source_pcm_sha256'), 'pcm_sha': p.get('source_pcm_sha256'), 'origin_sample': 0, 'frame_count': p['samples'], 'sample_rate': SR}
    for entry in (store.directory(pid) / 'stems').glob('*/manifest.json'):
        if not entry.parent.resolve().is_relative_to(store.directory(pid).resolve()):raise ValueError('声部路径越过项目目录')
        manifest = read(entry)
        if manifest.get('scope') == 'preview_only' or manifest.get('cache_scope', {}).get('type') == 'separation_trial':
            continue
        if not any(row.get('source_id') == source_id for row in manifest.get('stems', [])):continue
        cached = validate_manifest(entry.parent, manifest)
        if cached['frame_count'] != p['samples'] or cached['source_pcm_sha'] != p.get('source_pcm_sha256'):
            raise ValueError('分离版本与项目完整原曲时钟不匹配')
        for row in cached['stems']:
            if row['source_id'] == source_id:
                return {**row, 'source_role': row['role'], 'parent_source_id': cached['source_pcm_sha'],
                        'stem_set_id': cached['id'], 'origin_sample': 0, 'frame_count': cached['frame_count'], 'sample_rate': SR}
    raise ValueError('项目中没有这份已校验音源')
