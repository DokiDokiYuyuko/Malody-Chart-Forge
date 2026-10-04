"""Immutable range auditions on the original project clock, separate from full stems."""
import copy
import math
from pathlib import Path

import numpy as np
import soundfile as sf

from .advanced import SR, atomic, identifier, now, read, uid
from .paths import ROOT
from .separation import canonical_hash, ensure_stems, file_hash, pcm_hash, validated_settings

VERSION = 'separation-trial-v1'
CONTEXT_SAMPLES = 8 * SR
ROLES = ('original', 'vocals', 'accompaniment')


def ranges(project, start, end):
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (start, end)) or not 0 <= start < end <= project['samples'] or end - start < math.ceil(SR * .25):
        raise ValueError('试分离至少 250 ms，边界须为原曲内的整数采样点')
    return {'start_sample': start, 'end_sample': end}, {
        'start_sample': max(0, start - CONTEXT_SAMPLES), 'end_sample': min(project['samples'], end + CONTEXT_SAMPLES)}


def snapshot(project, start, end, settings):
    target, context = ranges(project, start, end)
    return {'task_type': 'separation_trial', 'trial_id': uid(),
            'project': {k: copy.deepcopy(project[k]) for k in ('id', 'samples', 'sample_rate', 'duration', 'source_pcm_sha256', 'revision')},
            'target_range': target, 'context_range': context, 'settings': validated_settings(settings)}


def _directory(project_directory, trial_id):
    root = Path(project_directory).resolve() / 'separation-trials'
    result = root / identifier(trial_id)
    if not result.resolve().is_relative_to(root):
        raise ValueError('试分离路径越过项目目录')
    return result


def validate_snapshot(state):
    p = state['project']; target, context = ranges(p, state['target_range']['start_sample'], state['target_range']['end_sample'])
    if state['context_range'] != context or p.get('sample_rate', SR) != SR:
        raise ValueError('试分离快照的原曲采样时钟或上下文范围不匹配')
    return {'version': VERSION, 'project_id': identifier(p['id']), 'parent_source_pcm_sha256': p['source_pcm_sha256'],
            'original_frame_count': p['samples'], 'sample_rate': SR, 'core': [target['start_sample'], target['end_sample']],
            'context': [context['start_sample'], context['end_sample']], 'settings': validated_settings(state['settings']),
            'preview_only': True, 'scope': 'preview_only'}


def validate_manifest(project_directory, manifest, project=None, expected_recipe=None):
    folder = _directory(project_directory, manifest['id'])
    if manifest.get('version') != VERSION or manifest.get('sample_rate') != SR or manifest.get('preview_only') is not True or manifest.get('scope') != 'preview_only':
        raise ValueError('试分离试听清单版本或范围无效')
    if expected_recipe is not None and manifest.get('recipe_hash') != expected_recipe:
        raise ValueError('试分离试听与提交快照不匹配')
    if project is not None and (manifest.get('parent_source_pcm_sha256') != project['source_pcm_sha256'] or manifest.get('original_frame_count') != project['samples'] or manifest.get('project_id') != project['id']):
        raise ValueError('试分离试听与项目原曲采样时钟不匹配')
    original_count = manifest['original_frame_count']
    target, context = ranges({'samples': original_count}, *manifest['core'])
    if manifest.get('context') != [context['start_sample'], context['end_sample']] or manifest.get('origin_source_sample') != target['start_sample'] or manifest.get('frame_count') != target['end_sample'] - target['start_sample']:
        raise ValueError('试分离试听边界或原曲偏移不匹配')
    rows = manifest.get('audio', [])
    if sorted(row.get('role', '') for row in rows) != sorted(ROLES):
        raise ValueError('试分离缺少原曲、人声或伴奏试听')
    for row in rows:
        path = (folder / row['file']).resolve()
        if path.parent != folder.resolve() or not path.is_file():
            raise ValueError('试分离试听文件路径无效')
        info = sf.info(path)
        if info.samplerate != SR or info.channels != 2 or info.subtype != 'FLOAT' or info.frames != manifest['frame_count']:
            raise ValueError('试分离试听帧数、声道或编码不匹配')
        data, _ = sf.read(path, dtype='float32', always_2d=True)
        if not np.isfinite(data).all() or pcm_hash(data) != row['pcm_sha'] or file_hash(path) != row['file_sha256']:
            raise ValueError('试分离试听完整性校验失败')
        row['path'] = str(path)
    return manifest


def present(manifest):
    result = copy.deepcopy(manifest)
    root = f"/api/advanced/projects/{result['project_id']}/separation-trials/{result['id']}"
    result['trial_id'] = result['id']
    result['target_range'] = {'start_sample': result['core'][0], 'end_sample': result['core'][1]}
    result['context_range'] = {'start_sample': result['context'][0], 'end_sample': result['context'][1]}
    for row in result['audio']:
        row.pop('path', None)
        row['audio_url'] = root + '/audio/' + row['role']
        row['waveform_url'] = root + '/waveform?role=' + row['role']
    return result


def load(project_directory, trial_id, project=None):
    path = _directory(project_directory, trial_id) / 'manifest.json'
    return validate_manifest(project_directory, read(path), project)


def audio_path(project_directory, trial_id, role, project=None):
    if role not in ROLES:
        raise ValueError('试听声部须为 original、vocals 或 accompaniment')
    manifest = load(project_directory, trial_id, project)
    return Path(next(row['path'] for row in manifest['audio'] if row['role'] == role))


def run(source, directory, options, progress):
    state = copy.deepcopy(options['_advanced']); p = state['project']; recipe = validate_snapshot(state)
    project = ROOT / 'outputs' / 'advanced' / p['id']; original = project / 'source.wav'
    if Path(source).resolve() != original.resolve():
        raise ValueError('试分离须从项目完整原曲读取')
    data, rate = sf.read(original, dtype='float32', always_2d=True)
    if rate != SR or data.shape[1] != 2 or len(data) != p['samples'] or not np.isfinite(data).all() or pcm_hash(data) != p['source_pcm_sha256']:
        raise ValueError('试分离原曲与提交快照不匹配')
    folder = _directory(project, state['trial_id']); folder.mkdir(parents=True, exist_ok=True)
    key = canonical_hash(recipe)
    if (folder / 'manifest.json').is_file():
        saved = validate_manifest(project, read(folder / 'manifest.json'), p, key)
        progress('试分离试听已缓存，原曲时钟校验通过', 100)
        return {'trial_id': saved['id'], 'trial_manifest': present(saved)}
    start, end = recipe['core']; context_start, context_end = recipe['context']
    cache = folder / 'cache'; cache.mkdir(exist_ok=True)
    context_path = cache / f'context-{context_start}-{context_end}.wav'
    context_audio = data[context_start:context_end]
    if context_path.is_file():
        existing, existing_rate = sf.read(context_path, dtype='float32', always_2d=True)
        if existing_rate != SR or existing.shape != context_audio.shape or pcm_hash(existing) != pcm_hash(context_audio):
            raise ValueError('试分离上下文缓存与原曲整数采样偏移不匹配')
    else:
        sf.write(context_path, context_audio, SR, subtype='FLOAT')
    scope = {'type': 'separation_trial', 'parent_source_pcm_sha256': p['source_pcm_sha256'],
             'original_frame_count': p['samples'], 'origin_source_sample': context_start,
             'core': recipe['core'], 'context': recipe['context']}
    separated = ensure_stems(context_path, cache, recipe['settings'], progress, cache_scope=scope)
    role_audio = {'original': data[start:end]}
    for role in ('vocals', 'accompaniment'):
        row = next((row for row in separated.get('stems', []) if row.get('role') == role), None)
        if row is None:
            raise ValueError('试分离上下文缺少人声或伴奏')
        output, output_rate = sf.read(row['path'], dtype='float32', always_2d=True)
        if output_rate != SR or output.shape != context_audio.shape or not np.isfinite(output).all():
            raise ValueError('试分离上下文输出采样时钟不匹配')
        role_audio[role] = output[start - context_start:end - context_start]
    rows = []
    original_rms = float(np.sqrt(np.mean(role_audio['original'].astype(np.float64) ** 2)))
    original_peak = float(np.max(np.abs(role_audio['original']), initial=0.))
    for role in ROLES:
        audio = role_audio[role]; path = folder / (role + '.wav'); temporary = folder / (role + '.' + uid() + '.wav')
        try:
            sf.write(temporary, audio, SR, subtype='FLOAT'); temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))); peak = float(np.max(np.abs(audio), initial=0.))
        rows.append({'role': role, 'file': path.name, 'frames': len(audio), 'origin_source_sample': start,
                     'pcm_sha': pcm_hash(audio), 'file_sha256': file_hash(path), 'rms': rms, 'peak': peak,
                     'rms_ratio': rms / original_rms if original_rms else None,
                     'peak_ratio': peak / original_peak if original_peak else None})
    manifest = {**recipe, 'id': state['trial_id'], 'recipe_hash': key, 'created': now(),
                'origin_source_sample': start, 'context_origin_source_sample': context_start, 'frame_count': end - start,
                'separation_recipe_hash': separated.get('recipe_hash', separated.get('id')), 'audio': rows,
                'name': '局部试分离 · ' + recipe['settings']['model'],
                'summary': f"{start / SR:.2f}–{end / SR:.2f} 秒 · 局部试听，不能用于谱面或融合"}
    validate_manifest(project, manifest, p, key)
    atomic(folder / 'manifest.json', manifest)
    progress('试分离试听已保存，原曲时钟校验通过', 100)
    return {'trial_id': manifest['id'], 'trial_manifest': present(manifest)}
