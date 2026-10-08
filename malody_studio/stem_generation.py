"""One queued parent: separate, independently generate, retain, then fuse."""
import copy
import time
import os
import math
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


def _raw_key(settings, descriptor, segment, variant, plan=None, direct_v32_policy=None):
    # Fusion/section tuning never invalidates the immutable model component.
    excluded=(('difficulty_rules','source_mode','separation_preset','fusion_mode','fusion_primary') if direct_v32_policy
              else ('difficulty_rules','source_mode','separation_preset','nps_ranges','fusion_mode','fusion_primary'))
    effective = {k: v for k, v in settings.items() if k not in excluded}
    inference_plan = [{'core': section.get('core'), 'conditions': {
        key: {'model_condition': item.get('model_condition'), 'effective_boost': item.get('effective_boost')}
        for key, item in (section.get('per_difficulty') or section.get('perDifficulty') or section.get('difficulties') or {}).items()}}
        for section in (plan or {}).get('sections', [])]
    adapter = Path(__file__).with_name('advanced_generation.py')
    execution_adapters={}
    if settings.get('engine')=='v32':
        for name,path in (('worker',ROOT/'tools'/'mapperatorinator_worker.py'),
                          ('request_adapter',Path(__file__).with_name('mapperatorinator.py')),
                          ('event_serialization',Path(__file__).with_name('v32_event_serialization.py')),
                          ('cfg_batch_order',Path(__file__).with_name('v32_cfg.py')),
                          ('generation_context',Path(__file__).with_name('generation_context.py'))):
            if path.is_file():execution_adapters[name]=file_hash(path)
    model_directory = ROOT / 'models' / ('mapperatorinator' if settings.get('engine') == 'v32' else 'mug-diffusion')
    registry = {str(path.relative_to(model_directory)): file_hash(path) for path in model_directory.glob('**/manifest.json')}
    recipe={'source': descriptor, 'settings': effective, 'range': [segment['start_sample'], segment.get('effective_end_sample',segment['end_sample'])],
            'variant': variant, 'inference_plan': inference_plan, 'tempo_reference_hash': (plan or {}).get('reference_hash'),
            'adapter_sha256': file_hash(adapter), 'execution_adapter_sha256':execution_adapters,
            'model_registry': registry}
    if direct_v32_policy is not None:recipe['direct_v32_policy']=direct_v32_policy
    return canonical_hash(recipe)


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
    audio = {row['role']: sf.read(row['path'], dtype='float32', always_2d=True)[0] for row in stems['stems']}
    mixture = mix
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
            v=v.ravel(); ac=ac.ravel()
            ve, ae = np.linalg.norm(v), np.linalg.norm(ac)
            ratio = min(ve, ae) / max(ve, ae, 1e-12)
            correlation = float(np.dot(v, ac) / max(1e-12, ve * ae))
            if correlation > .995 and ratio < .08 and min(ve, ae) > 1e-5:
                shared = 'leak-' + canonical_hash({'stem_set': stems['id'], 'samples': [a, b]})[:24]
                for event in (voice, instrument):
                    event['audio_evidence'].update(shared_event_id=shared, leakage_correlation=correlation, leakage_energy_ratio=ratio)


def _stem_silence_runs(path):
    """Declared silent runs of one stem's own audio file, or a record saying why they are unknown."""
    from . import stem_silence
    from .beat_analysis import mono_audio
    try:
        data, rate = sf.read(path, dtype='float32', always_2d=True)
        mono = mono_audio(data)[0]
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        return {'skipped': '声部音频无法读取：' + str(exc)}
    if not len(mono):
        return {'skipped': '声部音频为空'}
    duration_ms = len(mono) * 1000 / rate
    levels = stem_silence.frame_levels_db(mono, rate)
    return {'runs': stem_silence.silent_runs(levels, duration_ms=duration_ms), 'file': Path(path).name,
            'duration_ms': duration_ms, 'sample_rate': rate, 'file_peak': stem_silence.file_peak(mono)}


def _direct_fuse_revisions(pair, settings, policy, bounds, source_end_ms, silence=None):
    """Fuse frozen model events. With no fusion mode in the frozen settings this is a
    strict union that fails closed; otherwise the user-chosen collision handling applies.
    ``silence`` ({role: _stem_silence_runs result}) is used only on the fusion-mode path."""
    from .advanced import valid_events
    from .nps_star_calibration import chart_span_density
    from .charts import Note

    vocals=pair['vocals'];accompaniment=pair['accompaniment']
    if any(row.get('kind')!='stem_raw' for row in (vocals,accompaniment)):
        raise ValueError('直接融合只接受本次新生成的声部原谱')
    if vocals.get('variant')!=accompaniment.get('variant') or vocals.get('range')!=accompaniment.get('range') or vocals.get('range')!=bounds:
        raise ValueError('直接融合的原谱组合或范围不一致')
    vp,ap=vocals.get('provenance',{}),accompaniment.get('provenance',{})
    if (vp.get('source_role')!='vocals' or ap.get('source_role')!='accompaniment'
            or not vp.get('stem_set_id') or vp.get('stem_set_id')!=ap.get('stem_set_id')
            or not vp.get('parent_source_id') or vp.get('parent_source_id')!=ap.get('parent_source_id')
            or not vp.get('source_id') or not ap.get('source_id') or vp.get('source_id')==ap.get('source_id')):
        raise ValueError('直接融合的声部来源身份无法核实')
    events=[];origin_rows=[]
    for role,revision in (('vocals',vocals),('accompaniment',accompaniment)):
        source_id=revision['provenance']['source_id']
        note_ids=set()
        if not isinstance(revision.get('events'),list):raise ValueError('声部原谱音符列表无效')
        for index,raw in enumerate(revision['events']):
            if not isinstance(raw,dict):raise ValueError('声部原谱包含无效事件')
            note_id=raw.get('id')
            if not isinstance(note_id,str) or not note_id:note_id=f'event-{index}'
            if note_id in note_ids:raise ValueError('声部原谱音符身份重复')
            note_ids.add(note_id)
            origin={'revision_id':revision.get('id'),'note_id':note_id,'source_id':source_id,'stem_role':role,
                    'original_start_ms':raw.get('start_ms'),'original_end_ms':raw.get('end_ms'),
                    'original_lane':raw.get('lane'),'candidate_kind':'model'}
            identity={'direct_policy':policy,'origin':origin}
            event=copy.deepcopy(raw)
            event['id']='direct-fusion-'+canonical_hash(identity)[:32]
            event['origins']=[origin]
            event['source_id']=source_id
            event['candidate_provenance']={'kind':'model','source_role':role,'direct_policy_version':policy['version']}
            events.append(event);origin_rows.append(origin)
    if not events:raise ValueError('两份新生成声部原谱均没有模型音符')
    events.sort(key=lambda row:(row.get('start_ms',0),row.get('lane',0),row['id']))
    if len(events)!=len(origin_rows):raise ValueError('直接融合模型头来源对账失败')
    start_ms,end_ms=bounds[0]*1000/SR,bounds[1]*1000/SR
    pattern,difficulty=vocals['variant'].split('--',1)
    fusion_mode=settings.get('fusion_mode')
    if fusion_mode is not None:
        # User-selected collision handling replaces the strict union. Frozen
        # settings without a mode (historical retries) keep the strict path below.
        from . import stem_merge
        primary=settings.get('fusion_primary','vocals')
        stem_merge.validate_options(fusion_mode,primary)
        gap_ms=(settings.get('difficulty_rules',{}).get(difficulty) or {}).get('gap')
        if isinstance(gap_ms,bool) or not isinstance(gap_ms,(int,float)) or not math.isfinite(gap_ms) or gap_ms<=0:gap_ms=stem_merge.DEFAULT_GAP_MS
        gap_ms=float(gap_ms)
        chord_cap=(settings.get('difficulty_rules',{}).get(difficulty) or {}).get('chord')
        if isinstance(chord_cap,bool) or not isinstance(chord_cap,int) or not 1<=chord_cap<=4:chord_cap=stem_merge.DEFAULT_CHORD_CAP
        attack_window_ms=stem_merge.ATTACK_WINDOW_MS
        inputs={role:[row for row in events if row['origins'][0]['stem_role']==role] for role in stem_merge.ROLES}
        silent_spans=silence_record=None
        from . import stem_silence
        if silence is None:
            silence_record=stem_silence.policy_record(roles={role:{'skipped':'未提供声部静音分析'} for role in stem_merge.ROLES})
        else:
            rows={role:(silence.get(role) or {}) for role in stem_merge.ROLES}
            silent_spans={role:copy.deepcopy(row['runs']) for role,row in rows.items() if row.get('runs') is not None}
            silence_record=stem_silence.policy_record(roles={
                role:({'declared_runs':copy.deepcopy(row['runs']),'duration_ms':row.get('duration_ms'),'audio_file':row.get('file')}
                      if row.get('runs') is not None else {'skipped':row.get('skipped','声部音频不可用')})
                for role,row in rows.items()})
        merged=stem_merge.merge_stems(inputs['vocals'],inputs['accompaniment'],mode=fusion_mode,primary=primary,
                                      gap_ms=gap_ms,min_hold_ms=stem_merge.MIN_HOLD_MS,silent_spans=silent_spans,
                                      attack_window_ms=attack_window_ms,chord_cap=chord_cap)
        stem_merge.audit(inputs['vocals'],inputs['accompaniment'],merged)
        events=merged['events']
        if not events:raise ValueError('静音段内音符舍弃后，融合没有任何音符')
        origin_rows=[row['origins'][0] for row in events]
        valid_events(events,start_ms,end_ms,source_end_ms)
        nps_range=settings.get('nps_ranges',{}).get(difficulty)
        density=chart_span_density([Note(row['start_ms'],row['lane'],row.get('end_ms')) for row in events],nps_range) if nps_range else None
        version=stem_merge.VERSION
        recipe={'version':version,'parents':[vocals['id'],accompaniment['id']],
                'variant':vocals['variant'],'range':bounds,'policy':copy.deepcopy(policy),
                'source_ids':[vp['source_id'],ap['source_id']],'fusion_mode':fusion_mode,'fusion_primary':primary,
                'primary_role':merged['primary_role'],'gap_ms':gap_ms,'min_hold_ms':stem_merge.MIN_HOLD_MS,
                'attack_window_ms':attack_window_ms,'chord_cap':chord_cap}
        if silence_record is not None:recipe['stem_silence']=silence_record
        recipe_hash=canonical_hash(recipe)
        counts=copy.deepcopy(merged['counts'])
        empty_requests=[{'role':role,'request_key':row.get('key'),'core':row.get('core'),
                         'silent_fraction':(row.get('silence') or {}).get('silent_fraction')}
                        for role,revision in (('vocals',vocals),('accompaniment',accompaniment))
                        for row in revision.get('provenance',{}).get('core_conditions',[])
                        if row.get('status')=='empty_silent_stem']
        return {'events':events,'provenance':{
            **recipe,'recipe_hash':recipe_hash,'source_role':'fusion','source_id':'direct-fusion:'+recipe_hash,
            'stem_set_id':vp['stem_set_id'],'parent_source_id':vp['parent_source_id'],
            'fusion_policy':'priority_merge','fusion_policy_version':version,
            'raw_model_heads':sum(row['input'] for row in counts.values()),
            'fused_model_heads':sum(row['kept'] for row in counts.values()),
            'preservation_verified':False,'accounting_verified':True,'fusion_counts':counts,
            'residual_same_stem_lane_gap':merged['residual_same_stem_lane_gap'],
            'residual_same_stem_lane_gap_by_role':copy.deepcopy(merged['residual_same_stem_lane_gap_by_role']),
            'fusion_decisions':merged['decisions'],
            'fusion_summary':{'mode':fusion_mode,'primary':primary,'version':version,'counts':counts,
                              'residual_same_stem_lane_gap':merged['residual_same_stem_lane_gap'],
                              'stem_silent':{role:counts[role].get('stem_silent',0) for role in stem_merge.ROLES},
                              'stem_silence_skipped':([role for role in stem_merge.ROLES if (silence.get(role) or {}).get('runs') is None]
                                                      if silence is not None else list(stem_merge.ROLES))},
            **({'stem_silence':silence_record} if silence_record is not None else {}),
            **({'stem_empty_silent_requests':empty_requests} if empty_requests else {}),
            'structure_validation':'passed','head_origins':origin_rows,
            'nps_range_measurement':density}}
    for event in events:
        origin=event['origins'][0]
        if event.get('start_ms')!=origin['original_start_ms'] or event.get('lane')!=origin['original_lane'] or event.get('end_ms')!=origin['original_end_ms']:
            raise ValueError('直接融合改变了模型音符头、轨道或长条尾')
    valid_events(events,start_ms,end_ms,source_end_ms)
    # Verify the output is a strict event-for-event union. Sorting may reorder
    # rows, so compare canonical triples rather than positions.
    expected=sorted((row.get('start_ms'),row.get('lane'),row.get('end_ms'))
                    for revision in (vocals,accompaniment) for row in revision['events'])
    actual=sorted((row['start_ms'],row['lane'],row.get('end_ms')) for row in events)
    if actual!=expected or len(actual)!=sum(len(row['events']) for row in (vocals,accompaniment)):
        raise ValueError('直接融合没有逐个保全输入模型音符头')
    nps_range=settings.get('nps_ranges',{}).get(difficulty)
    density=chart_span_density([Note(row['start_ms'],row['lane'],row.get('end_ms')) for row in events],nps_range) if nps_range else None
    recipe={'version':'direct-model-head-union-v1','parents':[vocals['id'],accompaniment['id']],
            'variant':vocals['variant'],'range':bounds,'policy':copy.deepcopy(policy),
            'source_ids':[vp['source_id'],ap['source_id']]}
    recipe_hash=canonical_hash(recipe)
    return {'events':events,'provenance':{
        **recipe,'recipe_hash':recipe_hash,'source_role':'fusion','source_id':'direct-fusion:'+recipe_hash,
        'stem_set_id':vp['stem_set_id'],'parent_source_id':vp['parent_source_id'],
        'fusion_policy':'preserve_all_model_heads','fusion_policy_version':'direct-model-head-union-v1',
        'raw_model_heads':len(expected),'fused_model_heads':len(actual),'preservation_verified':True,
        'structure_validation':'passed','head_origins':origin_rows,
        'nps_range_measurement':density}}


def run(source, directory, options, progress):
    from .workflow_log import stage as log_stage, event as log_event, file_identity
    snapshot_hint=options.get('_advanced',{})
    with log_stage('stem_generation.validate_frozen_snapshot', source=file_identity(source),
                   project=snapshot_hint.get('project'),segment=snapshot_hint.get('segment'),
                   variants=snapshot_hint.get('variants'),settings=snapshot_hint.get('settings'),
                   direct_v32=bool(snapshot_hint.get('direct_v32_policy'))):
        pass
    if options['_advanced'].get('silence_verified'):
        from .advanced_generation import run as silence_candidate
        return silence_candidate(source,directory,options,progress)
    snapshot = copy.deepcopy(options['_advanced']); p = snapshot['project']; segment = snapshot['segment']; settings = snapshot['settings']
    direct_mode='direct_v32_policy' in snapshot
    direct_policy=snapshot.get('direct_v32_policy')
    if direct_mode:
        if settings.get('engine')!='v32' or settings.get('strategy')!='independent' or not isinstance(direct_policy,dict):
            raise ValueError('直接声部生成策略快照无效')
        from .nps_star_calibration import load_mapping
        load_mapping(direct_policy)
    from .advanced_generation import validate_frozen_policies, frozen_candidate_options
    validate_frozen_policies(snapshot)
    candidate_options=frozen_candidate_options(snapshot)
    # _stem_raw_only suppresses per-voice density spending in every child stage.
    # The parent's two rounds remain available after evaluating shared fusion.
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    frozen_original = snapshot.get('original_source', {}).get('path')
    project_directory = Path(frozen_original).parent if frozen_original else ROOT / 'outputs' / 'advanced' / identifier(p['id'])
    # Always separate the immutable complete original PCM, not selected/export PCM.
    original = project_directory / 'source.wav'
    from .audio_bounds import content_end
    p.setdefault('samples',sf.info(original).frames)
    segment['effective_end_sample']=min(segment['end_sample'],content_end(p))
    if Path(source).resolve() != original.resolve(): raise ValueError('声部生成须使用项目原曲 PCM')
    # A chart re-roll must not re-run full-track separation. Its independent
    # fixed seed belongs to the separation recipe, not the generation seed.
    separation_settings = {'model': settings.get('separation_preset', 'htdemucs')}
    explicit = snapshot.get('input_sources')
    if explicit:
        with log_stage('stem_generation.load_and_verify_selected_stem_set',stem_set_id=snapshot.get('stem_set_id'),
                       input_sources=explicit):
            manifest = read(project_directory / 'stems' / snapshot['stem_set_id'] / 'manifest.json')
            validate_manifest(project_directory / 'stems' / manifest['id'], manifest)
        if manifest['frame_count'] != p['samples'] or manifest['source_pcm_sha'] != p.get('source_pcm_sha256'):
            raise ValueError('所选分离版本与原曲时钟不匹配')
        for frozen in explicit:
            row = next((r for r in manifest['stems'] if r['role'] == frozen['source_role']), None)
            if row is None or row['source_id'] != frozen['source_id'] or row['pcm_sha'] != frozen['pcm_sha']:
                raise ValueError('声部版本与冻结生成来源不匹配')
    else:
        with log_stage('stem_generation.separate_and_materialize_stems',model=separation_settings['model'],
                       original=file_identity(original),project_directory=project_directory):
            manifest = materialize_stems(project_directory, ensure_stems(original, directory, separation_settings, progress))
        log_event('stem_set_result','stem_generation.separate_and_materialize_stems',manifest=manifest)
    from .section_plan import build_plan, validate_plan
    plan = snapshot.get('section_plan') or build_plan(original, settings, p.get('tempo', {}))
    validate_plan(plan, manifest['source_pcm_sha'])
    atomic(project_directory / 'section-plans' / (plan['id'] + '.json'), plan)
    atomic(directory / 'section-plan.json', plan)
    from .advanced_generation import run as generate
    rows = []; components = {}; errors = [];warnings = [];fusion_seconds=0.;silence_cache = {}
    bounds = [segment['start_sample'], segment['end_sample']]
    roles = [row['source_role'] for row in explicit] if explicit else ['vocals', 'accompaniment']
    descriptors = {}; role_settings = {}; timing_recoveries = []
    previous_failure = None
    if snapshot.get('retry_of') and len(roles) == 2 and not direct_mode:
        try:
            old_directory = ROOT/'outputs'/identifier(snapshot['retry_of'])
            old_snapshot = read(old_directory/'queue-worker.json')['options']['_advanced']
            old_result = read(old_directory/'worker-result.json')
            if (old_snapshot['project']['id'] == p['id'] and old_snapshot['settings'] == settings
                and old_snapshot['variants'] == snapshot['variants']
                and [old_snapshot['segment'][k] for k in ('start_sample','end_sample')] == bounds):
                previous_failure = old_result
        except (OSError, ValueError, KeyError): pass
    def save_raw(generated, role, descriptor, raw_settings, prefix):
        generated.update(id=uid(), kind='stem_raw', activate_initial=False)
        generated['provenance'].update(descriptor, raw_recipe_hash=_raw_key(raw_settings, descriptor, segment, generated['variant'], plan,direct_policy if direct_mode else None),
                                      generation_role_seed=raw_settings['seed'], original_base_seed=settings['seed'], section_plan_id=plan['id'])
        if direct_mode:
            generated['provenance'].update(direct_v32_policy=copy.deepcopy(direct_policy),
                direct_v32_policy_hash=canonical_hash(direct_policy),
                quality_policy=copy.deepcopy(snapshot.get('quality_policy')),
                candidate_policy=copy.deepcopy(snapshot.get('candidate_policy',settings.get('candidate_policy'))))
        generated['provenance'].pop('path', None)
        if generated['provenance'].get('cache_file'):
            generated['provenance']['cache_job'] = directory.name
            generated['provenance']['cache_file'] = prefix + '/' + generated['provenance']['cache_file']
        generated['range'] = bounds; rows.append(generated)
        if not direct_mode:
            atomic(ROOT / 'cache' / 'stem-generation' / (generated['provenance']['raw_recipe_hash'] + '.json'),
                   {'schema': 1, 'result_hash': canonical_hash(generated), 'result': generated})
        return generated
    for index, role in enumerate(roles):
        stem = next(row for row in manifest['stems'] if row['role'] == role)
        descriptor = {'source_id': stem['source_id'], 'source_role': role, 'path': stem['path'],
                      'parent_source_id': manifest['source_pcm_sha'], 'stem_set_id': manifest['id'],
                      'pcm_sha': stem['pcm_sha'], 'sample_rate': SR, 'origin_sample': 0, 'frame_count': manifest['frame_count']}
        if not direct_mode and snapshot.get('density_policy'):
            descriptor['density_policy_hash']=snapshot['density_policy'].get('policy_hash')
        raw_settings = {**settings, 'seed': _seed(settings['seed'], role, stem['pcm_sha'])}
        descriptors[role] = descriptor; role_settings[role] = raw_settings
        pending = []; reused = {}
        for variant in snapshot['variants']:
            key = _raw_key(raw_settings, descriptor, segment, variant['key'], plan,direct_policy if direct_mode else None)
            previous=None
            if not direct_mode:
                previous=next((row for row in snapshot.get('reusable_raw_revisions',[]) if row['variant']==variant['key'] and row['range']==bounds
                               and row.get('provenance',{}).get('source_id')==descriptor['source_id']
                               and row.get('provenance',{}).get('pcm_sha')==descriptor['pcm_sha']),None)
                previous = previous or _find_existing(project_directory, segment, variant['key'], key)
            if previous:
                reused[variant['key']] = previous
            else:
                checkpoint = _checkpoint(key) if not direct_mode else None
                if checkpoint:
                    reused[variant['key']] = checkpoint
                    # A recovered model stage may not have reached server commit.
                    if not (project_directory / 'revisions' / (checkpoint['id'] + '.json')).is_file(): rows.append(checkpoint)
                else:
                    pending.append(variant)
        if pending:
            local_snapshot = {**snapshot, 'source': descriptor, 'settings': raw_settings, 'variants': pending,
                              '_stem_raw_only': True, '_density_raw_supply':len(roles)==1, 'section_plan': plan}
            progress('独立生成人声原谱' if role == 'vocals' else '独立生成伴奏原谱', 20 + index * 30)
            stage = directory / role
            stage.mkdir(exist_ok=True)
            try:
                label = '人声' if role == 'vocals' else '伴奏'
                previous_errors = [e for e in (previous_failure or {}).get('errors',[]) if e.get('stage') == role]
                paired_output = any(r.get('kind') == 'stem_raw' and r.get('provenance',{}).get('source_role') != role
                                    and r.get('provenance',{}).get('stem_set_id') == manifest['id']
                                    for r in (previous_failure or {}).get('advanced_result',[]))
                known_timing_failure = False
                if not direct_mode and settings.get('engine') == 'v32' and paired_output:
                    from .paired_timing import missing_model_timing
                    known_timing_failure = missing_model_timing(previous_errors)
                if known_timing_failure:
                    # A frozen retry already proves this ordinary attempt failed;
                    # proceed directly to the paired recovery after source reuse.
                    result = {'advanced_result':[], 'errors':previous_errors}
                else:
                    with log_stage('stem_generation.generate_role',role=role,variant_keys=[row['key'] for row in pending],
                                   source=file_identity(stem['path']),source_descriptor=descriptor,
                                   seed=raw_settings.get('seed'),engine=settings.get('engine'),strategy=settings.get('strategy')):
                        result = generate(Path(stem['path']), stage, {**options, '_advanced': local_snapshot},
                                          lambda message, percent: progress(label + ' · ' + message, 20 + index * 30 + min(100, max(0, percent)) * .3))
                    log_event('role_generation_result','stem_generation.generate_role',role=role,
                          generated_variants=[row.get('variant') for row in result.get('advanced_result',[])],
                          errors=result.get('errors'),returned_metadata=result.get('execution'))
            except Exception as exc:
                errors.append({'stage': role, 'error': str(exc)}); result = {'advanced_result': []}
            for generated in result.get('advanced_result', []):
                reused[generated['variant']] = save_raw(generated, role, descriptor, raw_settings, role)
            errors.extend({**error, 'stage':role} for error in result.get('errors', []))
            warnings.extend({**warning, 'stage':role} for warning in result.get('warnings', []))
        components[role] = reused
    if not direct_mode and settings.get('engine') == 'v32' and len(roles) == 2 and all(not components[role] for role in roles):
        from .paired_timing import missing_model_timing, original_recovery_project
        if all(missing_model_timing([e for e in errors if e.get('stage') == role]) for role in roles):
            initial_errors = copy.deepcopy(errors)
            try:
                progress('两路节拍缺失，使用完整原曲独立生成模型节拍', 76)
                project, reference = original_recovery_project(original, project_directory, p, segment,
                    lambda message, percent: progress('原曲模型节拍 · '+message, 76+min(100,max(0,percent))*.02), authorized_missing_timing=True)
                for role in roles:
                    for variant in snapshot['variants']:
                        key=variant['key'];record={**copy.deepcopy(reference),'failed_role':role,'variant':key,
                                                  'initial_errors':initial_errors}
                        try:
                            prefix=role+'/original-timing-recovery/'+key
                            stage=directory/prefix;stage.mkdir(parents=True,exist_ok=True)
                            local={**snapshot,'project':project,'source':descriptors[role],'settings':role_settings[role],
                                   'variants':[variant],'_stem_raw_only':True,'section_plan':plan}
                            result=generate(Path(descriptors[role]['path']),stage,{**options,'_advanced':local},
                                lambda message,percent:progress('声部节拍恢复 · '+message,78+min(100,max(0,percent))*.06))
                            for generated in result.get('advanced_result',[]):
                                if generated['variant']!=key:raise ValueError('声部恢复返回其他组合')
                                generated['provenance']['original_timing_recovery']=copy.deepcopy(reference)
                                components[role][key]=save_raw(generated,role,descriptors[role],role_settings[role],prefix)
                            if key not in components[role]:raise ValueError('原曲节拍恢复没有有效声部输出：'+str(result.get('errors',[])))
                            record['status']='completed'
                        except Exception as exc:
                            record.update(status='failed',error=str(exc))
                            errors.append({'stage':role,'variant':key,'error':'原曲节拍恢复失败：'+str(exc)})
                        timing_recoveries.append(record)
                    if all(v['key'] in components[role] for v in snapshot['variants']):
                        errors=[e for e in errors if e.get('stage')!=role]
            except Exception as exc:
                timing_recoveries.append({'source':'original_native_timing_model','attempts':1,'status':'failed','error':str(exc)})
                errors.append({'stage':'original_timing_recovery','error':str(exc)})
    # The original detector may deliberately reject an uncertain timing map.
    # A missing model timing context can still recover once from the paired
    # model's actual, validated absolute clock. Never synthesize an empty stem.
    if not direct_mode and settings.get('engine') == 'v32' and len(roles) == 2:
        from .paired_timing import missing_model_timing, recovery_project
        for role in roles:
            initial_errors = [e for e in errors if e.get('stage') == role]
            if not missing_model_timing(initial_errors): continue
            other = 'accompaniment' if role == 'vocals' else 'vocals'
            for variant in snapshot['variants']:
                key = variant['key']
                if key in components[role] or key not in components.get(other, {}): continue
                if any(r.get('failed_role')==role and r.get('variant')==key for r in timing_recoveries):continue
                record = {'failed_role':role, 'reference_role':other, 'variant':key,
                          'attempts':0, 'initial_errors':copy.deepcopy(initial_errors)}
                try:
                    project, reference = recovery_project(p, segment, role, components[other][key], manifest, key, authorized_missing_timing=True)
                    record.update(reference, attempts=1)
                    prefix = role + '/timing-recovery/' + key
                    stage = directory / prefix; stage.mkdir(parents=True, exist_ok=True)
                    local = {**snapshot, 'project':project, 'source':descriptors[role], 'settings':role_settings[role],
                             'variants':[variant], '_stem_raw_only':True, 'section_plan':plan}
                    progress('复用另一声部模型节拍，恢复'+('人声' if role == 'vocals' else '伴奏'), 78)
                    result = generate(Path(descriptors[role]['path']), stage, {**options, '_advanced':local},
                                      lambda message, percent: progress('声部节拍恢复 · '+message, 78+min(100,max(0,percent))*.06))
                    for generated in result.get('advanced_result', []):
                        if generated['variant'] != key: raise ValueError('声部恢复返回其他组合')
                        generated['provenance']['paired_timing_recovery'] = copy.deepcopy(reference)
                        components[role][key] = save_raw(generated, role, descriptors[role], role_settings[role], prefix)
                    if key not in components[role]: raise ValueError('声部节拍恢复没有有效输出：'+str(result.get('errors',[])))
                    record['status'] = 'completed'
                except Exception as exc:
                    record.update(status='failed', error=str(exc))
                    errors.append({'stage':role, 'variant':key, 'error':'声部节拍恢复失败：'+str(exc)})
                timing_recoveries.append(record)
            if all(v['key'] in components[role] for v in snapshot['variants']):
                errors = [e for e in errors if e.get('stage') != role]
    can_direct_fuse=not direct_mode or set(roles)=={'vocals','accompaniment'}
    for variant in snapshot['variants'] if snapshot.get('auto_fuse', not bool(explicit)) and can_direct_fuse else []:
        key = variant['key']
        if direct_mode:
            if key not in components.get('vocals', {}) or key not in components.get('accompaniment', {}):
                errors.append({'variant':key,'stage':'direct_fusion','error':'缺少一方本次新生成的声部原谱，保留已有原谱并阻止融合','raw_preserved':True});continue
            pair={role:copy.deepcopy(components[role][key]) for role in ('vocals','accompaniment')}
            for revision in pair.values():
                revision.setdefault('settings',settings);revision.setdefault('range',bounds)
            try:
                fusion_started=time.monotonic()
                silence=None
                if settings.get('fusion_mode') is not None:
                    for role in ('vocals','accompaniment'):
                        if role not in silence_cache:silence_cache[role]=_stem_silence_runs(descriptors[role]['path'])
                    silence={role:silence_cache[role] for role in ('vocals','accompaniment')}
                result=_direct_fuse_revisions(pair,settings,direct_policy,bounds,p.get('duration',p['samples']/SR)*1000,silence)
                from .direct_v32 import quality_events
                checked=quality_events(result['events'],original,directory,snapshot,
                    variant['difficulty'],variant['pattern'])
                candidate=checked['events']
                expected={event['id']:event.get('origins') for event in result['events']}
                if len(candidate)!=len(result['events']) or {event['id']:event.get('origins') for event in candidate}!=expected:
                    raise ValueError('直接融合质量复核未完整保留每个声部模型头来源')
                from .advanced import valid_events
                valid_events(candidate,bounds[0]*1000/SR,bounds[1]*1000/SR,p.get('duration',p['samples']/SR)*1000)
                from .advanced import as_notes
                from .nps_star_calibration import chart_span_density
                from .chart_quality import summarize_region
                difficulty=variant['difficulty']
                density=chart_span_density(as_notes(candidate),settings.get('nps_ranges',{})[difficulty])
                quality_summary=summarize_region(candidate,checked.get('decisions',[]),
                    before_events=result['events'],ln_target=settings.get('ln_ratio',.15))
                result['events']=candidate
                result['provenance'].update(direct_v32_policy=copy.deepcopy(direct_policy),
                    quality_policy=copy.deepcopy(snapshot.get('quality_policy')),
                    candidate_policy=copy.deepcopy(snapshot.get('candidate_policy',settings.get('candidate_policy'))),
                    quality_summary=copy.deepcopy(quality_summary),
                    quality_decisions=copy.deepcopy(checked.get('decisions',[])),
                    density_validation=density,nps_range_measurement=copy.deepcopy(density))
                fusion_seconds+=time.monotonic()-fusion_started
                result['provenance']['section_plan_id']=plan['id']
                rows.append({'id':uid(),'variant':key,'kind':'fusion','events':result['events'],'settings':settings,
                             'provenance':result['provenance'],'activate_initial':True})
            except (ValueError,TypeError,KeyError) as exc:
                errors.append({'variant':key,'stage':'direct_fusion','error':str(exc),
                               'fusion_failed':True,'raw_preserved':True,
                               'raw_revision_ids':[pair[role].get('id') for role in ('vocals','accompaniment')]})
            continue
        if key not in components['vocals'] or key not in components['accompaniment']:
            errors.append({'variant': key, 'error': '缺少一个声部原谱，保留已完成原谱并阻止融合'}); continue
        pair = {role: copy.deepcopy(components[role][key]) for role in ('vocals', 'accompaniment')}
        for revision in pair.values():
            revision.setdefault('settings', settings); revision.setdefault('range', bounds)
        progress('共享四轨融合 · ' + key, 85)
        _audio_evidence(pair, manifest, original)
        quality_evidence=None
        if snapshot.get('quality_policy'):
            from .quality_workflow import evidence_for
            quality_evidence=evidence_for(project_directory,original,descriptors.values())
        fusion_started=time.monotonic()
        fusion_acoustic=snapshot.get('evidence',{}).get('acoustic') or quality_evidence
        if candidate_options.get('selection_policy')=='model_skeleton_then_audio' and quality_evidence:
            fusion_acoustic=quality_evidence
        with log_stage('stem_generation.fuse_roles',variant=key,source_revisions={role:pair[role].get('id') for role in pair},
                       input_event_counts={role:len(pair[role].get('events',[])) for role in pair},
                       plan_id=plan.get('id'),settings=settings,candidate_policy=candidate_options):
            result = fuse_revisions(pair['vocals'], pair['accompaniment'], plan, settings,quality_evidence,
                acoustic=fusion_acoustic,timing=snapshot.get('timing_map'),**candidate_options)
        log_event('fusion_result','stem_generation.fuse_roles',variant=key,event_count=len(result.get('events',[])),
              provenance=result.get('provenance'),report=result.get('report'))
        fusion_seconds+=time.monotonic()-fusion_started
        if snapshot.get('density_policy'):
            from .density_validation import candidate_rank
            from .density_calibration import condition_next
            from .quality_workflow import finish, evidence_for
            evidence=quality_evidence or evidence_for(project_directory,original,descriptors.values())
            def finish_fusion(value, rounds):
                model_heads=sum(len(r['events']) for r in pair.values())
                supply_stats=value.get('provenance',{}).get('section_stats',[])
                candidate_heads=sum(row.get('candidate_heads',0) for row in supply_stats) if supply_stats else None
                acoustic_heads=sum(row.get('candidate_acoustic_heads',0) for row in supply_stats)
                shortage=bool(supply_stats) and any(row.get('candidate_heads',0)<row.get('target_heads',0)*.85
                                                  for row in supply_stats)
                checked=finish(value['events'],settings,snapshot,original,project_directory,
                    pattern=variant['pattern'],difficulty=variant['difficulty'],bounds=bounds,
                    evidence=evidence,candidates={'model_heads':model_heads,'candidate_heads':candidate_heads,
                        'acoustic_candidate_heads':acoustic_heads,'model_supply_failure':shortage},attempts=rounds)
                return {**value,'events':checked['events'],'provenance':{**value['provenance'],
                    'quality_policy':snapshot.get('quality_policy'),'quality_summary':checked['summary'],
                    'candidate_policy':snapshot.get('candidate_policy'),
                    'quality_decisions':checked['decisions'],'quality_evidence_id':evidence.get('id',evidence.get('cache_key')),
                    'density_policy':snapshot['density_policy'],'density_validation':checked['density_validation']}}
            result=finish_fusion(result,0);best=result;best_pair=copy.deepcopy(pair);attempts=[]
            used={role:[float(settings.get('conditions',{}).get(settings['engine'],{}).get(variant['difficulty'],5.9))] for role in pair}
            for round_index in range(2):
                if snapshot.get('disable_density_retry'):break
                if not best['provenance']['density_validation'].get('retry_allowed'):break
                trial_pair=copy.deepcopy(best_pair)
                total_raw=max(1,sum(len(r['events']) for r in best_pair.values()))
                for role in ('vocals','accompaniment'):
                    share=max(.2,min(.8,len(best_pair[role]['events'])/total_raw))
                    target=best['provenance']['density_validation']['target_nps']*share*1.15
                    condition=condition_next(snapshot['density_policy'],settings['engine'],variant['difficulty'],target_rate=target,
                                             attempt=round_index,used_conditions=used[role],source_role=role,pattern=variant['pattern'])
                    if condition is None:continue
                    used[role].append(condition)
                    prefix=role+'/density-round-'+str(round_index+1)+'/'+key
                    stage=directory/prefix;stage.mkdir(parents=True,exist_ok=True)
                    rs=copy.deepcopy(role_settings[role]);rs['conditions'].setdefault(settings['engine'],{})[variant['difficulty']]=condition
                    rs['seed']=int(canonical_hash({'base':settings['seed'],'role':role,'round':round_index+1,'variant':key})[:8],16)%2147483640
                    rp=copy.deepcopy(plan)
                    for section in rp['sections']:section['per_difficulty'][variant['difficulty']]['model_condition']=condition
                    rp.pop('id',None);rp.pop('content_hash',None);rp['id']=rp['content_hash']=canonical_hash(rp)
                    project=copy.deepcopy(p)
                    local={**snapshot,'project':project,'source':descriptors[role],'settings':rs,'variants':[variant],
                           '_stem_raw_only':True,'section_plan':rp,'disable_density_retry':True,'density_condition_override':True}
                    record={'round':round_index+1,'role':role,'condition':condition,'target_rate':target}
                    atomic(stage/'density-round-snapshot.json',local)
                    try:
                        progress('密度补生成 '+str(round_index+1)+'/2 · '+role+' · '+key,88)
                        generated=generate(Path(descriptors[role]['path']),stage,{**options,'_advanced':local},
                            lambda message,percent:progress('密度补生成 · '+message,88+min(100,max(0,percent))*.05))
                        candidate=next((r for r in generated.get('advanced_result',[]) if r['variant']==key),None)
                        if candidate is None:raise ValueError('补生成未返回有效原谱：'+str(generated.get('errors',[])))
                        candidate['provenance'].update(density_generation_round=round_index+1,inference_plan_id=rp['id'])
                        actual=[float(row.get('selected_condition',row.get('inference_condition',row['sr'])))
                                for row in candidate['provenance'].get('core_conditions',[]) if 'sr' in row]
                        saved=save_raw(candidate,role,descriptors[role],rs,prefix)
                        trial_pair[role]=saved;record.update(status='completed',heads=len(saved['events']),revision_id=saved['id'])
                        record.update(actual_conditions=actual,condition_applied=bool(actual) and all(abs(value-condition)<1e-6 for value in actual))
                    except Exception as exc:record.update(status='failed',error=str(exc))
                    attempts.append(record)
                pair=trial_pair;_audio_evidence(pair,manifest,original)
                trial=fuse_revisions(pair['vocals'],pair['accompaniment'],plan,settings,evidence,
                    acoustic=fusion_acoustic or evidence,timing=snapshot.get('timing_map'),**candidate_options)
                trial=finish_fusion(trial,round_index+1)
                rule=settings.get('difficulty_rules',{}).get(variant['difficulty'],{})
                if candidate_rank(trial['events'],trial['provenance']['density_validation'],rule)<candidate_rank(best['events'],best['provenance']['density_validation'],rule):
                    best=trial;best_pair=copy.deepcopy(pair)
                best['provenance']['density_validation']['attempts']=round_index+1
                best['provenance']['density_validation']['retry_allowed']=not best['provenance']['density_validation']['passed'] and best['provenance']['density_validation']['cause']=='model_supply' and round_index+1<2
            result=best
            result['provenance']['density_generation_attempts']=attempts
            for role in best_pair:components[role][key]=best_pair[role]
        rows.append({'id': uid(), 'variant': key, 'kind': 'fusion', 'events': result['events'], 'settings': settings,
                     'provenance': result['provenance'], 'activate_initial': True})
    if timing_recoveries:atomic(directory/'timing-recovery-report.json',{'recoveries':timing_recoveries,'errors':errors})
    if not rows and not any(components.values()):
        raise ValueError('所选声部生成均失败：' + str(errors))
    progress('声部原谱与融合候选已保存' if any(row['kind'] == 'fusion' for row in rows) else '已保留完成的声部原谱；融合等待补齐', 98)
    result={'fusion_seconds':round(fusion_seconds,4),'advanced_result': rows, 'errors': errors, 'warnings': warnings, 'timing_recoveries':timing_recoveries, 'bounds': bounds, 'stem_set_id': manifest['id'], 'section_plan_id': plan['id'],
            'reused_revisions': list(dict.fromkeys(r['id'] for entries in components.values() for r in entries.values() if r['id'] not in {row.get('id') for row in rows})),
            **({'generation_context_policy':snapshot['generation_context_policy'],
                'member_segment_ids':[part['id'] for part in snapshot.get('member_segments',[segment])]}
               if snapshot.get('generation_context_policy') else {})}
    if direct_mode:result['direct_v32_policy']=copy.deepcopy(direct_policy)
    return result


def _effective_fusion_plan(plan, settings):
    """Retune one fusion budget while retaining the selected music evidence."""
    from .section_plan import difficulty_budget, canonical_hash
    effective = copy.deepcopy(plan)
    for section in effective['sections']:
        if not all(key in section for key in ('active_seconds', 'rhythm_activity', 'tempo_confidence')):
            raise ValueError('共享段落计划缺少冻结节奏证据，请重新分析段落')
        section['per_difficulty'] = difficulty_budget(settings, section['active_seconds'],
                                                     0 if plan.get('arrangement_enabled') else section['rhythm_activity'], section['tempo_confidence'])
        if plan.get('arrangement_enabled'):
            factor=section.get('normalized_factor',1.)
            for detail in section['per_difficulty'].values():
                detail['target_heads_soft']*=factor
                detail['target_rate']*=factor
                detail['effective_boost']=factor-1
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
        if any(r.get('provenance',{}).get('direct_v32_policy') for r in (vocals,accompaniment)):
            raise ValueError('直接 V32 原谱不能进入旧预算融合；请新建生成任务以使用完整保全融合')
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
        request_settings=payload.get('settings',{})
        segment_overrides=segment.get('overrides',{})
        merged_settings=merge(merge(p['settings'],segment_overrides),request_settings)
        # Manual re-fusion is the legacy scalar-rate path. A stored default
        # range must not mask a rate explicitly changed in a segment/request
        # override. Explicit range objects still take precedence.
        if 'nps_ranges' not in segment_overrides and 'nps_ranges' not in request_settings:
            rate_overrides={}
            for source in (segment_overrides.get('difficulty_rules',{}),request_settings.get('difficulty_rules',{})):
                for difficulty,rule in source.items():
                    if isinstance(rule,dict) and 'rate' in rule:
                        rate_overrides[difficulty]=rule['rate']
            if rate_overrides:
                ranges=copy.deepcopy(merged_settings.get('nps_ranges',{}))
                for difficulty,rate in rate_overrides.items():
                    if isinstance(rate,bool) or not isinstance(rate,(int,float)) or not math.isfinite(rate) or not .5<=rate<=50:
                        raise ValueError('谱面规则超出范围')
                    lower=max(.5,rate*.8);upper=min(50.,rate*1.2)
                    if rate*.8<.5:upper=2*rate-lower
                    if rate*1.2>50:lower=2*rate-upper
                    ranges[difficulty]={'min':round(lower,5),'max':round(upper,5)}
                merged_settings['nps_ranges']=ranges
        settings = validate_settings(merged_settings)
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
