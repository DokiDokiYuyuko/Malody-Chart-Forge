"""Ordinary jobs use the original whole-song call, independent of advanced plans."""
import copy
import hashlib

VERSION = 'simple-whole-song-v1'


def waveform_only(y):
    """Preserve the preview envelope without detecting tempo or beat positions."""
    import numpy as np
    return {'bpm': None, 'beat_times': [], 'warnings': [],
            'waveform': [round(float(np.max(np.abs(chunk))), 4) if len(chunk) else 0.
                         for chunk in np.array_split(y, 420)]}


def contract():
    return {'version': VERSION, 'baseline': '5006506',
            'inference': 'one_whole_song_mother_per_pattern',
            'application_segments': False, 'automatic_density_retries': 0,
            'timing': 'model_output_without_external_reference',
            'note_heads': 'model_only', 'quality': 'single_post_generation_pass'}


def direct_contract():
    return {**contract(), 'version': 'simple-direct-v32-v1',
            'inference': 'independent_difficulty_and_bpm_condition_spans',
            'application_segments': True, 'bpm_analysis': 'frozen_beat_this'}


def freeze(options):
    """Only new ordinary submissions are migrated; never mutate an old snapshot."""
    if options.get('_advanced'):
        raise ValueError('高级任务不能使用简易整曲策略')
    policy = options.get('simple_generation_policy')
    if policy is not None and policy not in (contract(), direct_contract()):
        raise ValueError('简易整曲生成策略版本不受支持')
    from .chart_quality import contract as quality_contract
    from .quality_workflow import candidate_contract
    result = copy.deepcopy(options)
    for field in ('density_policy', 'generation_context_policy', 'section_plan',
                  'timing_reference', 'timing_fallback_reference', '_advanced_presets',
                  'start_time', 'end_time', 'retry_condition', 'density_rounds'):
        result.pop(field, None)
    result.update(simple_generation_policy=contract(), dynamic_enabled=False,
                  candidate_policy=candidate_contract(), quality_policy=quality_contract())
    if options.get('engine') == 'v32' and policy != contract():
        from .nps_star_calibration import freeze_policy, normalize_ranges, load_mapping
        frozen = options.get('direct_v32_policy') or freeze_policy()
        load_mapping(frozen)
        result.update(simple_generation_policy=direct_contract(), direct_v32_policy=frozen,
                      nps_ranges=normalize_ranges(options.get('nps_ranges'),options.get('difficulty_rules')),
                      dynamic_enabled=options.get('dynamic_enabled',True), strategy='independent')
    return result


def density_target(source, settings):
    """Freeze one whole-song measurement target; it never drives model requests.

    PCM channel power is measured before inference. No tempo detector or regional
    model conditions are needed, and opposite channels cannot cancel activity.
    """
    import numpy as np
    import soundfile as sf
    from .paths import PRESETS
    from .section_plan import canonical_hash
    data, sr = sf.read(source, dtype='float32', always_2d=True)
    if sr != 44100 or not len(data) or not np.isfinite(data).all():
        raise ValueError('简易整曲目标需要有效的 44100 Hz 原曲 PCM')
    hop = round(sr * .005)
    windows = []
    for a in range(0, len(data), sr):
        b = min(a + sr, len(data))
        energy = np.mean(data[a:b] ** 2, axis=1)
        starts = np.arange(0, b-a, hop)
        lengths = np.minimum(hop, b-a-starts)
        rms = np.sqrt(np.add.reduceat(energy, starts) / lengths)
        windows.append((a, b, rms, lengths))
    threshold = max(max(float(rms.max()) for _, _, rms, _ in windows) * .008, 1e-5)
    profile = [{'start_sample': a, 'end_sample': b,
                'active_fraction': float(lengths[rms > threshold].sum()) / (b-a)}
               for a, b, rms, lengths in windows]
    active = sum((p['end_sample']-p['start_sample'])/sr*p['active_fraction'] for p in profile)
    rules = settings.get('difficulty_rules', {})
    targets = {}
    for key, preset in PRESETS.items():
        rule = {**preset, **rules.get(key, {})}
        targets[key] = {'target_rate': rule['rate'], 'target_heads_soft': active*rule['rate'],
                        'hard_caps': {'peak_1s': rule['peak'], 'chord': rule['chord'],
                                      'min_lane_gap_ms': rule['gap'], 'hold_max_ms': rule['hold_ms']}}
    result = {'version': VERSION, 'sample_rate': sr, 'samples': len(data),
              'source_pcm_sha': hashlib.sha256(data.astype('<f4', copy=False).tobytes()).hexdigest(),
              'measurement': {'version': 'pcm-channel-power-activity-v1',
                              'hop_samples': hop, 'rms_threshold': threshold},
              'sections': [{'id': 'whole-song', 'core': [0, len(data)],
                            'active_seconds': active, 'profile': profile, 'per_difficulty': targets}]}
    digest = canonical_hash(result)
    result.update(id=digest, content_hash=digest)
    return result


def validate(options):
    if options.get('_advanced') or options.get('simple_generation_policy') not in (contract(),direct_contract()):
        raise ValueError('简易整曲生成策略无效')
    if options.get('simple_generation_policy') == direct_contract():
        from .nps_star_calibration import load_mapping
        load_mapping(options.get('direct_v32_policy'))
        if options.get('engine') != 'v32':
            raise ValueError('直接星级通路仅支持 V32')
    elif options.get('direct_v32_policy'):
        raise ValueError('旧生成快照不能混入直接生成策略')
    if any(options.get(field) is not None for field in ('density_policy', '_advanced_presets',
            'timing_reference', 'timing_fallback_reference', 'start_time', 'end_time', 'density_rounds')):
        raise ValueError('简易整曲任务不能带入高级分段或密度补生成策略')


def hydrate_rhythm(notes, metadata):
    """Reuse the repaired native-event reader without changing the shared adapter."""
    from pathlib import Path
    from .v32_rhythm import read_rhythm, attach_notes
    path = metadata.get('charts', {}).get('master')
    if path:
        rhythm, missing = read_rhythm(Path(path).parent / 'model-events.json')
        attach_notes(notes, rhythm, missing)


def finish_rule_revision(notes, candidates, cache, settings, directory, difficulty, pattern, adjustment):
    """Derive only from cached model supply; keep the original activity denominator."""
    from .advanced import as_notes
    from .chart_quality import apply
    from .density_validation import evaluate
    from .paths import PRESETS
    from .quality_workflow import candidate_events, evidence_for
    from .section_plan import canonical_hash
    from pathlib import Path
    if cache.get('simple_generation_policy') != contract():
        raise ValueError('简易整曲缓存策略无效')
    target = copy.deepcopy(cache['density_plan'])
    body = {k: v for k, v in target.items() if k not in ('id', 'content_hash')}
    if target.get('id') != canonical_hash(body) or target.get('content_hash') != target['id']:
        raise ValueError('简易整曲密度目标缓存无效')
    rule = {**PRESETS[difficulty], **settings.get('difficulty_rules', {}).get(difficulty, {})}
    detail = target['sections'][0]['per_difficulty'][difficulty]
    detail.update(target_rate=rule['rate'],
                  target_heads_soft=target['sections'][0]['active_seconds']*rule['rate'])
    detail['hard_caps'].update(peak_1s=rule['peak'], chord=rule['chord'],
                              min_lane_gap_ms=rule['gap'], hold_max_ms=rule['hold_ms'])
    events = candidate_events(notes, candidates, cache['raw_events'],
                              source_role='mix', source_id=target['source_pcm_sha'])
    evidence = evidence_for(directory, Path(directory)/'source.wav')
    checked = apply(events, settings, evidence, difficulty, pattern, policy=settings['quality_policy'])
    density = evaluate(checked['events'], target, difficulty, [0, target['samples']],
                       candidates={'model_heads': cache['raw_count'],
                                   'candidate_heads': adjustment.get('candidate_model_heads',cache['raw_count']),
                                   'acoustic_candidate_heads': 0,
                                   'constraint_removed': adjustment.get('phone_filtered_notes',0)}, attempts=0)
    density.update(retry_allowed=False, automatic_retry_enabled=False,
                   denominator=target['measurement']['version'])
    return as_notes(checked['events']), {'density_validation': density, 'quality_summary': checked['summary']}
