"""One queued parent: separate, independently generate, retain, then fuse."""
import copy
import time
import os
from pathlib import Path
import shutil

import numpy as np
import soundfile as sf

from .advanced import SR, atomic, identifier, read, uid, merge, validate_settings
from .fusion import fuse_revisions
from .paths import ROOT
from .separation import canonical_hash, ensure_stems, validate_manifest, file_hash


def materialize_stems(project_directory, manifest):
    destination = Path(project_directory) / 'stems' / manifest['id']
    if not destination.resolve().is_relative_to((Path(project_directory) / 'stems').resolve()):raise ValueError('分离版本路径越过项目目录')
    for row in manifest['stems']:
        if (destination / row['file']).resolve().parent != destination.resolve():raise ValueError('分离声部文件路径无效')
    destination.mkdir(parents=True, exist_ok=True)
    local = copy.deepcopy(manifest)
    if (destination / 'manifest.json').is_file():
        previous = read(destination / 'manifest.json')
        for key in ('created','name'):
            if key in previous:local[key] = previous[key]
    for row in local['stems']:
        source = Path(row['path']); target = destination / row['file']
        if not target.is_file():
            temporary = destination / (row['file'] + '.partial')
            try:
                os.link(source, temporary)
            except OSError:
                shutil.copyfile(source, temporary)
            temporary.replace(target)
        row['path'] = str(target.resolve())
    validate_manifest(destination, local, local['recipe_hash'])
    atomic(destination / 'manifest.json', local)
    return local


def _seed(base, role, pcm_sha):
    return (int(base) + int(canonical_hash({'role': role, 'pcm_sha': pcm_sha})[:8], 16)) % 2147483640


def _raw_key(settings, descriptor, segment, variant, plan=None):
    # Fusion/section tuning never invalidates the immutable model component.
    effective = {k: v for k, v in settings.items() if k not in ('difficulty_rules', 'source_mode', 'separation_preset')}
    inference_plan = [{'core': section.get('core'), 'conditions': {
        key: {'model_condition': item.get('model_condition'), 'effective_boost': item.get('effective_boost')}
        for key, item in (section.get('per_difficulty') or section.get('perDifficulty') or section.get('difficulties') or {}).items()}}
        for section in (plan or {}).get('sections', [])]
    adapter = Path(__file__).with_name('advanced_generation.py')
    model_directory = ROOT / 'models' / ('mapperatorinator' if settings.get('engine') == 'v32' else 'mug-diffusion')
    registry = {str(path.relative_to(model_directory)): file_hash(path) for path in model_directory.glob('**/manifest.json')}
    return canonical_hash({'source': descriptor, 'settings': effective, 'range': [segment['start_sample'], segment['end_sample']],
                           'variant': variant, 'inference_plan': inference_plan, 'tempo_reference_hash': (plan or {}).get('reference_hash'), 'adapter_sha256': file_hash(adapter), 'model_registry': registry})


def _find_existing(project_directory, segment, variant, recipe_hash):
    for version in reversed(segment.get('versions', {}).get(variant, [])):
        path = Path(project_directory) / 'revisions' / (identifier(version['id']) + '.json')
        if path.is_file():
            revision = read(path)
            if revision.get('kind') == 'stem_raw' and revision.get('provenance', {}).get('raw_recipe_hash') == recipe_hash:
                return revision
    return None


def _checkpoint(recipe_hash):
    path = ROOT / 'cache' / 'stem-generation' / (recipe_hash + '.json')
    try:
        checkpoint = read(path)
        result = checkpoint['result']
        if checkpoint.get('result_hash') != canonical_hash(result): return None
        if result['provenance']['raw_recipe_hash'] != recipe_hash or result.get('kind') != 'stem_raw': return None
        return result
    except (OSError, ValueError, KeyError):
        return None


def _audio_evidence(revisions, stems, source):
    """Absolute energy supports selection; very quiet coherent leakage shares identity."""
    mix, rate = sf.read(source, dtype='float32', always_2d=True)
    if rate != SR: raise ValueError('采音证据采样率错误')
    audio = {row['role']: sf.read(row['path'], dtype='float32', always_2d=True)[0].mean(axis=1) for row in stems['stems']}
    mixture = mix.mean(axis=1)
    for role, revision in revisions.items():
        for event in revision['events']:
            center = round(event['start_ms'] * SR / 1000); a = max(0, center - 1323); b = min(len(mixture), center + 1323)
            rms = float(np.sqrt(np.mean(audio[role][a:b].astype(np.float64) ** 2))) if b > a else 0.
            reference = float(np.sqrt(np.mean(mixture[a:b].astype(np.float64) ** 2))) if b > a else 0.
            event['audio_evidence'] = {'source_id': revision['provenance']['source_id'], 'sample_range': [a, b],
                                       'rms': rms, 'mixture_rms': reference, 'audible': rms > max(1e-5, reference * .015),
                                       'salience': min(3., rms / max(1e-5, reference) + .2)}
    vocals = revisions['vocals']['events']; accompaniment = sorted(revisions['accompaniment']['events'], key=lambda e: e['start_ms']); j = 0
    for voice in sorted(vocals, key=lambda e: e['start_ms']):
        while j < len(accompaniment) and accompaniment[j]['start_ms'] < voice['start_ms'] - 12: j += 1
        for instrument in accompaniment[j:j + 8]:
            if instrument['start_ms'] > voice['start_ms'] + 12: break
            center = round((voice['start_ms'] + instrument['start_ms']) * .5 * SR / 1000)
            a, b = max(0, center - 1323), min(len(mixture), center + 1323)
            v = audio['vocals'][a:b].astype(np.float64); ac = audio['accompaniment'][a:b].astype(np.float64)
            ve, ae = np.linalg.norm(v), np.linalg.norm(ac)
            ratio = min(ve, ae) / max(ve, ae, 1e-12)
            correlation = float(np.dot(v, ac) / max(1e-12, ve * ae))
            if correlation > .995 and ratio < .08 and min(ve, ae) > 1e-5:
                shared = 'leak-' + canonical_hash({'stem_set': stems['id'], 'samples': [a, b]})[:24]
                for event in (voice, instrument):
                    event['audio_evidence'].update(shared_event_id=shared, leakage_correlation=correlation, leakage_energy_ratio=ratio)


def run(source, directory, options, progress):
    snapshot = copy.deepcopy(options['_advanced']); p = snapshot['project']; segment = snapshot['segment']; settings = snapshot['settings']
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    project_directory = ROOT / 'outputs' / 'advanced' / identifier(p['id'])
    # Always separate the immutable complete original PCM, not selected/export PCM.
    original = project_directory / 'source.wav'
    if Path(source).resolve() != original.resolve(): raise ValueError('声部生成须使用项目原曲 PCM')
    # A chart re-roll must not re-run full-track separation. Its independent
    # fixed seed belongs to the separation recipe, not the generation seed.
    separation_settings = {'model': settings.get('separation_preset', 'htdemucs')}
    explicit = snapshot.get('input_sources')
    if explicit:
        manifest = read(project_directory / 'stems' / snapshot['stem_set_id'] / 'manifest.json')
        validate_manifest(project_directory / 'stems' / manifest['id'], manifest)
        if manifest['frame_count'] != p['samples'] or manifest['source_pcm_sha'] != p.get('source_pcm_sha256'):
            raise ValueError('所选分离版本与原曲时钟不匹配')
        for frozen in explicit:
            row = next((r for r in manifest['stems'] if r['role'] == frozen['source_role']), None)
            if row is None or row['source_id'] != frozen['source_id'] or row['pcm_sha'] != frozen['pcm_sha']:
                raise ValueError('声部版本与冻结生成来源不匹配')
    else:
        manifest = materialize_stems(project_directory, ensure_stems(original, directory, separation_settings, progress))
    from .section_plan import build_plan, validate_plan
    plan = snapshot.get('section_plan') or build_plan(original, settings, p.get('tempo', {}))
    validate_plan(plan, manifest['source_pcm_sha'])
    atomic(project_directory / 'section-plans' / (plan['id'] + '.json'), plan)
    atomic(directory / 'section-plan.json', plan)
    from .advanced_generation import run as generate
    rows = []; components = {}; errors = [];fusion_seconds=0.
    bounds = [segment['start_sample'], segment['end_sample']]
    roles = [row['source_role'] for row in explicit] if explicit else ['vocals', 'accompaniment']
    for index, role in enumerate(roles):
        stem = next(row for row in manifest['stems'] if row['role'] == role)
        descriptor = {'source_id': stem['source_id'], 'source_role': role, 'path': stem['path'],
                      'parent_source_id': manifest['source_pcm_sha'], 'stem_set_id': manifest['id'],
                      'pcm_sha': stem['pcm_sha'], 'sample_rate': SR, 'origin_sample': 0, 'frame_count': manifest['frame_count']}
        raw_settings = {**settings, 'seed': _seed(settings['seed'], role, stem['pcm_sha'])}
        pending = []; reused = {}
        for variant in snapshot['variants']:
            key = _raw_key(raw_settings, descriptor, segment, variant['key'], plan)
            previous = _find_existing(project_directory, segment, variant['key'], key)
            if previous:
                reused[variant['key']] = previous
            else:
                checkpoint = _checkpoint(key)
                if checkpoint:
                    reused[variant['key']] = checkpoint
                    # A recovered model stage may not have reached server commit.
                    if not (project_directory / 'revisions' / (checkpoint['id'] + '.json')).is_file(): rows.append(checkpoint)
                else:
                    pending.append(variant)
        if pending:
            local_snapshot = {**snapshot, 'source': descriptor, 'settings': raw_settings, 'variants': pending,
                              '_stem_raw_only': True, 'section_plan': plan}
            progress('独立生成人声原谱' if role == 'vocals' else '独立生成伴奏原谱', 20 + index * 30)
            stage = directory / role
            stage.mkdir(exist_ok=True)
            try:
                label = '人声' if role == 'vocals' else '伴奏'
                result = generate(Path(stem['path']), stage, {**options, '_advanced': local_snapshot},
                                  lambda message, percent: progress(label + ' · ' + message, 20 + index * 30 + min(100, max(0, percent)) * .3))
            except Exception as exc:
                errors.append({'stage': role, 'error': str(exc)}); result = {'advanced_result': []}
            for generated in result.get('advanced_result', []):
                rid = uid()
                generated.update(id=rid, kind='stem_raw', activate_initial=False)
                generated['provenance'].update(descriptor, raw_recipe_hash=_raw_key(raw_settings, descriptor, segment, generated['variant'], plan),
                                               generation_role_seed=raw_settings['seed'], original_base_seed=settings['seed'], section_plan_id=plan['id'])
                generated['provenance'].pop('path', None)
                if generated['provenance'].get('cache_file'):
                    generated['provenance']['cache_job'] = directory.name
                    generated['provenance']['cache_file'] = role + '/' + generated['provenance']['cache_file']
                generated['range'] = bounds; rows.append(generated); reused[generated['variant']] = generated
                atomic(ROOT / 'cache' / 'stem-generation' / (generated['provenance']['raw_recipe_hash'] + '.json'),
                       {'schema': 1, 'result_hash': canonical_hash(generated), 'result': generated})
            errors.extend(result.get('errors', []))
        components[role] = reused
    for variant in snapshot['variants'] if snapshot.get('auto_fuse', not bool(explicit)) else []:
        key = variant['key']
        if key not in components['vocals'] or key not in components['accompaniment']:
            errors.append({'variant': key, 'error': '缺少一个声部原谱，保留已完成原谱并阻止融合'}); continue
        pair = {role: copy.deepcopy(components[role][key]) for role in ('vocals', 'accompaniment')}
        for revision in pair.values():
            revision.setdefault('settings', settings); revision.setdefault('range', bounds)
        progress('共享四轨融合 · ' + key, 85)
        _audio_evidence(pair, manifest, original)
        fusion_started=time.monotonic()
        result = fuse_revisions(pair['vocals'], pair['accompaniment'], plan, settings)
        fusion_seconds+=time.monotonic()-fusion_started
        rows.append({'id': uid(), 'variant': key, 'kind': 'fusion', 'events': result['events'], 'settings': settings,
                     'provenance': result['provenance'], 'activate_initial': True})
    if not rows and not any(components.values()):
        raise ValueError('所选声部生成均失败：' + str(errors))
    progress('声部原谱与融合候选已保存' if any(row['kind'] == 'fusion' for row in rows) else '已保留完成的声部原谱；融合等待补齐', 98)
    return {'fusion_seconds':round(fusion_seconds,4),'advanced_result': rows, 'errors': errors, 'bounds': bounds, 'stem_set_id': manifest['id'], 'section_plan_id': plan['id'],
            'reused_revisions': list(dict.fromkeys(r['id'] for entries in components.values() for r in entries.values() if r['id'] not in {row.get('id') for row in rows}))}


def _effective_fusion_plan(plan, settings):
    """Retune one fusion budget while retaining the selected music evidence."""
    from .section_plan import difficulty_budget, canonical_hash
    effective = copy.deepcopy(plan)
    for section in effective['sections']:
        if not all(key in section for key in ('active_seconds', 'rhythm_activity', 'tempo_confidence')):
            raise ValueError('共享段落计划缺少冻结节奏证据，请重新分析段落')
        section['per_difficulty'] = difficulty_budget(settings, section['active_seconds'],
                                                     section['rhythm_activity'], section['tempo_confidence'])
    effective.update(dynamic_enabled=settings['dynamic_enabled'], dynamic_strength=settings['dynamic_strength'],
                     fusion_parent_plan_id=plan['id'], fusion_only=True,
                     fusion_settings_hash=canonical_hash(settings))
    body = {key: value for key, value in effective.items() if key not in ('id', 'content_hash')}
    digest = canonical_hash(body)
    effective.update(id=digest, content_hash=digest)
    return effective


def fuse_selected(store, pid, sid, payload):
    with store.lock:
        p = store.load(pid); store.check(p, payload.get('expected_revision')); segment = store.segment(p, sid)
        vocals = store.revision(pid, payload['vocal_revision']); accompaniment = store.revision(pid, payload['accompaniment_revision'])
        bounds = [segment['start_sample'], segment['end_sample']]
        if any(r['segment_id'] != sid or r['range'] != bounds or r.get('kind') != 'stem_raw' for r in (vocals, accompaniment)):
            raise ValueError('请选择当前片段范围内的两份声部原谱')
        if vocals['variant'] != accompaniment['variant'] or vocals['provenance'].get('source_role') != 'vocals' or accompaniment['provenance'].get('source_role') != 'accompaniment':
            raise ValueError('请选择同一组合的人声与伴奏原谱')
        if vocals['provenance'].get('stem_set_id') != accompaniment['provenance'].get('stem_set_id'):
            raise ValueError('两路原谱必须来自同一分离版本')
        plan = payload.get('section_plan')
        if plan is None:
            plan_id = payload.get('plan_id')
            if not isinstance(plan_id, str) or len(plan_id) not in (32, 64) or any(c not in 'abcdef0123456789' for c in plan_id):
                raise ValueError('请明确选择有效共享段落难度计划')
            plan = read(store.directory(pid) / 'section-plans' / (plan_id + '.json'))
        from .section_plan import validate_plan
        validate_plan(plan, p['source_pcm_sha256'])
        if plan['samples'] != p['samples'] or plan['sample_rate'] != SR:
            raise ValueError('共享段落计划与原曲时钟不匹配')
        if any(r['provenance'].get('parent_source_id') != p['source_pcm_sha256'] for r in (vocals, accompaniment)):
            raise ValueError('声部原谱与当前原曲 PCM 不匹配')
        settings = validate_settings(merge(merge(p['settings'], segment.get('overrides', {})),payload.get('settings', {})))
        stems = read(store.directory(pid) / 'stems' / vocals['provenance']['stem_set_id'] / 'manifest.json')
        validate_manifest(store.directory(pid) / 'stems' / stems['id'], stems)
        if stems['frame_count'] != p['samples'] or stems['source_pcm_sha'] != p['source_pcm_sha256']:
            raise ValueError('分离版本与当前原曲时钟不匹配')
        effective_plan = _effective_fusion_plan(plan, settings)
        validate_plan(effective_plan, p['source_pcm_sha256'])
        pair = {'vocals': copy.deepcopy(vocals), 'accompaniment': copy.deepcopy(accompaniment)}
        _audio_evidence(pair, stems, store.directory(pid) / 'source.wav')
        vocals, accompaniment = pair['vocals'], pair['accompaniment']
        result = fuse_revisions(vocals, accompaniment, effective_plan, settings)
        atomic(store.directory(pid) / 'section-plans' / (effective_plan['id'] + '.json'), effective_plan)
        result['provenance'].update(section_plan_id=effective_plan['id'], parent_section_plan_id=plan['id'],
                                    fusion_settings_applied=True)
        return store.add_revision(pid, sid, vocals['variant'], result['events'], settings, 'fusion', result['provenance'], bounds=bounds, activate_initial=False)
