"""Frozen music evidence and one shared difficulty budget in source samples."""
from __future__ import annotations
import hashlib
import json
import math
import numpy as np
import soundfile as sf
from scipy.signal import find_peaks, resample_poly
from .difficulty import PRESETS

SR = 44100
VERSION = 'section-plan-v4'
BOOSTS = dict(zip(PRESETS, (.15, .2, .25, .25, .25, .2)))
DELTAS = dict(zip(PRESETS, (.3, .4, .5, .6, .6, .6)))


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def plan_settings_hash(settings):
    return canonical_hash({key: settings.get(key) for key in (
        'engine', 'dynamic_enabled', 'dynamic_strength', 'conditions',
        'difficulty_rules', 'profile', 'section_granularity')})


def pcm_audio(path):
    data, rate = sf.read(path, dtype='float32', always_2d=True)
    if not len(data) or not np.isfinite(data).all():
        raise ValueError('段落计划需要有效 PCM 音频')
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    elif data.shape[1] != 2:
        raise ValueError('段落计划支持单声道或双声道音频')
    if rate != SR:
        divisor = math.gcd(rate, SR)
        data = resample_poly(data, SR // divisor, rate // divisor, axis=0).astype(np.float32)
    return np.ascontiguousarray(data, dtype='<f4')


def rhythm_features(data):
    """Activity is independent of BPM; sustained loud sound is not fast rhythm."""
    import librosa
    # Channel power prevents antiphase cancellation; onset is the mean of both
    # channel positive fluxes, not a half-wave rectified artificial mono signal.
    hop = 110
    waves = resample_poly(data, 1, 2, axis=0).astype(np.float32)
    spectra = np.abs(librosa.stft(waves.T, n_fft=512, hop_length=hop))
    power = np.mean(spectra ** 2, axis=0)
    magnitude = np.sqrt(power)
    flux = np.maximum(np.diff(np.log1p(spectra * 10), axis=-1, prepend=np.log1p(spectra[..., :1] * 10)), 0).mean(axis=(0, 1))
    norm = max(float(np.percentile(flux, 95)), 1e-8)
    envelope = flux / norm
    peaks, _ = find_peaks(envelope, distance=4, prominence=.2, height=.3)
    rms = np.sqrt(np.sum(power, axis=0)) / 256
    threshold = max(float(np.max(rms)) * .008, 1e-5)
    peaks = [int(frame) for frame in peaks if rms[frame] > threshold]
    seconds = math.ceil(len(data) / SR)
    profile = []
    for second in range(seconds):
        a, b = int(second * 22050 / hop), min(len(rms), math.ceil((second + 1) * 22050 / hop))
        local = rms[a:b]
        count = sum(a <= frame < b for frame in peaks)
        span = min(1., (len(data) - second * SR) / SR)
        active = float(np.mean(local > threshold)) if len(local) else 0.
        profile.append({'start_sample': second * SR, 'end_sample': min(len(data), (second + 1) * SR),
                        'rms': round(float(np.sqrt(np.mean(local ** 2))) if len(local) else 0., 7),
                        'onset_rate': round(count / max(span, 1e-6), 4),
                        'active_fraction': round(active, 5)})
    # Dynamic tempo estimation is evidence only. No note snapping or forced BPM.
    dynamic = librosa.feature.tempo(onset_envelope=envelope, sr=22050, hop_length=hop, aggregate=None)
    if len(dynamic) != len(envelope):
        raise ValueError('局部节拍估计时间轴不一致')
    _, frames = librosa.beat.beat_track(onset_envelope=envelope, sr=22050, hop_length=hop, bpm=dynamic, trim=False)
    return profile, envelope, [float(frame * hop / 22050) for frame in frames], hop / 22050


def _tempo_evidence(start, end, tempo, envelope, beat_times, frame_seconds):
    points = tempo.get('points') or []
    reference = tempo.get('reference_source')
    manual = bool(tempo.get('manual'))
    # Trust belongs to each covered interval; a manual point cannot confirm an
    # earlier audio/chart estimate or a core crossing an unconfirmed interval.
    covered = [index for index,(t,_) in enumerate(points)
               if t < end*1000 and (points[index+1][0] if index+1<len(points) else float('inf')) > start*1000]
    metadata = tempo.get('points_metadata')
    complete_coverage = bool(covered) and points[covered[0]][0] <= start * 1000
    if metadata is not None:
        aligned = isinstance(metadata,list) and len(metadata)==len(points) and all(isinstance(row,dict) for row in metadata)
        sources = {metadata[index].get('source') or reference or 'unknown' for index in covered} if aligned else {'unknown'}
        trusted = complete_coverage and aligned and all(metadata[index].get('confirmed') is True and metadata[index].get('source') in ('manual','user_confirmed') for index in covered)
        reference = next(iter(sources)) if len(sources)==1 else 'mixed'
    else:
        # Older explicit manual edits had no per-point metadata. Imported/model
        # references still never become verified merely because manual is set.
        trusted = complete_coverage and manual and reference in (None,'manual','user_confirmed')
    active = next((float(bpm) for t, bpm in reversed(points) if t <= start * 1000), None)
    if not points:
        # Import analysis historically stored only a global scalar. Retain it as
        # context, without inventing anchors or promoting an estimate to trust.
        scalar = tempo.get('bpm')
        if isinstance(scalar, (int, float)) and not isinstance(scalar, bool) and math.isfinite(scalar) and scalar > 0:
            active = float(scalar)
            reference = reference or 'audio_analysis'
    if trusted and active:
        # A core may cross a confirmed anchor closer than the section minimum.
        # Integrate the actual intervals instead of extending its first BPM.
        beats = sum(max(0., min(end, points[index+1][0]/1000 if index+1<len(points) else end)
                              - max(start, points[index][0]/1000)) * float(points[index][1]) / 60
                    for index in covered)
        effective_bpm = beats * 60 / max(end - start, 1e-9)
        return {'bpm': effective_bpm, 'bpm_start': active, 'bpm_end': float(points[covered[-1]][1]),
                'variable_bpm': len({float(points[index][1]) for index in covered}) > 1,
                'confidence': 1., 'margin': 1., 'source': 'manual', 'reference_source': reference,
                'uncertain': False, 'eligible_boost': beats >= 8 and end - start >= 2,
                'beat_count': round(beats, 3), 'alternatives': [], 'note_shift_ms': 0}
    a, b = round(start / frame_seconds), round(end / frame_seconds)
    local = envelope[max(0, a):min(len(envelope), b)]
    local_beats = [v for v in beat_times if start <= v < end]
    intervals = np.diff(local_beats)
    if not len(intervals) or not np.any(local > .1):
        return {'bpm': None, 'reference_bpm': active, 'reference_source': reference,
                'confidence': 0., 'margin': 0., 'source': 'audio_estimate', 'uncertain': True,
                'eligible_boost': False, 'beat_count': len(local_beats), 'alternatives': [], 'note_shift_ms': 0}
    bpm = 60 / float(np.median(intervals))
    # Compare pulse train support at BPM, half and double. Ambiguity is explicit.
    scores = []
    for value in (bpm / 2, bpm, bpm * 2):
        lag = max(1, round(60 / value / frame_seconds))
        if lag >= len(local):
            score = 0.
        else:
            score = float(np.dot(local[lag:], local[:-lag]) / max(1e-9, np.linalg.norm(local[lag:]) * np.linalg.norm(local[:-lag])))
        scores.append({'bpm': round(value, 4), 'support': round(max(0., score), 5)})
    ordered = sorted(scores, key=lambda item: -item['support'])
    margin = ordered[0]['support'] - ordered[1]['support']
    regularity = 1 - min(1., float(np.std(intervals) / max(1e-9, np.mean(intervals))) * 3)
    confidence = min(1., max(0., regularity * ordered[0]['support']))
    eligible = confidence >= .75 and margin >= .2 and len(local_beats) >= 8 and end - start >= 2
    return {'bpm': ordered[0]['bpm'], 'reference_bpm': active, 'reference_source': reference,
            'confidence': round(confidence, 5), 'margin': round(margin, 5),
            'source': 'audio_estimate', 'uncertain': not eligible, 'eligible_boost': eligible,
            'beat_count': len(local_beats), 'alternatives': scores, 'note_shift_ms': 0}


SECTION_GRANULARITY = {
    # (minimum section seconds, preferred section seconds, maximum seconds)
    'fine': (4, 8, 16),
    'balanced': (8, 16, 32),
    'coarse': (16, 32, 48),
}


def _boundaries(samples, profile, tempo, granularity='balanced'):
    """Choose section size while retaining cuts at strong rhythmic changes."""
    if granularity not in SECTION_GRANULARITY:
        raise ValueError('制谱段落粒度无效')
    minimum, preferred, maximum = SECTION_GRANULARITY[granularity]
    duration = samples / SR
    special = {float(t) / 1000 for t, _ in tempo.get('points', []) if 0 < float(t) / 1000 < duration}
    # Evidence changes are candidates, not mandatory cuts on every accent.
    for i in range(8, len(profile) - 8):
        before = np.mean([p['onset_rate'] for p in profile[i-4:i]])
        after = np.mean([p['onset_rate'] for p in profile[i:i+4]])
        if abs(after - before) > max(3, min(before, after) * .8):
            special.add(float(i))
    cuts = [0.]
    while duration - cuts[-1] > maximum:
        start = cuts[-1]
        candidates = [v for v in special if start + minimum <= v <= start + maximum and duration - v >= minimum]
        fallback = min(start + preferred, duration - minimum)
        next_cut = min(candidates, key=lambda v: abs(v - (start + preferred))) if candidates else fallback
        if next_cut <= start:
            raise ValueError('制谱段落粒度无法覆盖音频')
        cuts.append(next_cut)
    remaining = duration - cuts[-1]
    if remaining < minimum and len(cuts) > 1:
        # Avoid a tiny trailing inference by moving the previous boundary back.
        room = max(0., cuts[-1] - cuts[-2] - minimum)
        cuts[-1] -= min(minimum - remaining, room)
    cuts.append(duration)
    # Preserve meaningful tempo/activity changes inside the final remainder too.
    # Otherwise a long last section could cross a known BPM anchor simply because
    # its total length was already below the selected maximum.
    for point in sorted(special):
        for index in range(len(cuts)-1):
            if cuts[index] + minimum <= point <= cuts[index+1] - minimum:
                cuts.insert(index+1, point)
                break
    return [round(v * SR) for v in cuts]


def difficulty_budget(settings, active_duration, activity, evidence):
    """Apply settings to frozen musical evidence, without analyzing or inferring."""
    enabled = bool(settings.get('dynamic_enabled', False))
    strength = float(settings.get('dynamic_strength', 1.))
    if not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError('动态难度强度须为 0–1')
    rules = settings.get('difficulty_rules', {})
    conditions = settings.get('conditions', {})
    engine = settings.get('engine', 'v32')
    result = {}
    for key, default in PRESETS.items():
        rule = {**default, **rules.get(key, {})}
        activity_boost = BOOSTS[key] * strength * activity if enabled else 0.
        tempo_boost = 0.
        # Trusted fast BPM can mildly reinforce activity, never override rests.
        if enabled and evidence['eligible_boost'] and active_duration > .1:
            tempo_boost = min(BOOSTS[key] / 3, max(0., ((evidence['bpm'] or 0) - 160) / 800)) * strength
        boost = float(np.clip(activity_boost + tempo_boost, -BOOSTS[key], BOOSTS[key]))
        base = float(conditions.get(engine, {}).get(key, default['sr']))
        condition = float(np.clip(base + DELTAS[key] * boost / max(BOOSTS[key], 1e-9), 1 if engine == 'v32' else 1.5, 10 if engine == 'v32' else 8))
        target = rule['rate'] * active_duration * (1 + boost)
        result[key] = {'target_heads_soft': round(target, 5), 'target_rate': round(rule['rate'] * (1 + boost), 5),
                       'model_condition': round(condition if enabled else base, 5), 'base_model_condition': base,
                       'activity_boost': round(activity_boost, 5), 'tempo_boost': round(tempo_boost, 5),
                       'effective_boost': round(boost, 5), 'hard_caps': {'peak_1s': rule['peak'], 'chord': rule['chord'],
                       'min_lane_gap_ms': rule['gap'], 'release_gap_ms': 35, 'hold_max_ms': rule['hold_ms']}}
    return result


def build_plan(source_wav, settings, tempo=None):
    tempo = tempo or {}
    data = pcm_audio(source_wav)
    source_hash = hashlib.sha256(data.tobytes()).hexdigest()
    enabled = bool(settings.get('dynamic_enabled', False))
    strength = float(settings.get('dynamic_strength', 1.))
    if not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError('动态难度强度须为 0–1')
    profile, envelope, beats, frame_seconds = rhythm_features(data)
    active_rates = [p['onset_rate'] for p in profile if p['active_fraction'] > .1]
    median_rate = float(np.median(active_rates)) if active_rates else 0.
    scale = max(2., float(np.percentile(active_rates, 90)) - float(np.percentile(active_rates, 10))) if active_rates else 2.
    for p in profile:
        p['activity_delta'] = round(float(np.clip((p['onset_rate'] - median_rate) / scale, -1, 1)), 5)
    granularity = settings.get('section_granularity', 'balanced')
    boundaries = _boundaries(len(data), profile, tempo, granularity)
    sections = []
    for index, (a, b) in enumerate(zip(boundaries, boundaries[1:])):
        local = [p for p in profile if p['start_sample'] < b and p['end_sample'] > a]
        evidence = _tempo_evidence(a / SR, b / SR, tempo, envelope, beats, frame_seconds)
        weights = [(min(b, p['end_sample']) - max(a, p['start_sample'])) / SR for p in local]
        active_duration = sum(w * p['active_fraction'] for w, p in zip(weights, local))
        activity = sum(w * p['activity_delta'] for w, p in zip(weights, local)) / max(1e-9, sum(weights))
        per_difficulty = difficulty_budget(settings, active_duration, activity, evidence)
        sections.append({'id': f'core-{index}-{a}-{b}', 'core': [a, b], 'context': [max(0, a-4*SR), min(len(data), b+4*SR)],
                         'active_seconds': round(active_duration, 5), 'rhythm_activity': round(activity, 5),
                         'tempo_confidence': evidence, 'profile': local, 'per_difficulty': per_difficulty})
    plan = {'schema_version': 1, 'version': VERSION, 'source_pcm_sha': source_hash, 'sample_rate': SR,
            'samples': len(data), 'dynamic_enabled': enabled, 'dynamic_strength': strength,
            'section_granularity': granularity,
            'settings_hash': plan_settings_hash(settings), 'reference_hash': canonical_hash(tempo),
            'tempo_reference': tempo, 'sections': sections, 'budget_application': 'once-at-selection-or-fusion',
            'warnings': ['局部节拍置信不足或半倍速歧义时不增加 BPM 难度权重；音符时间不吸附。']}
    digest = canonical_hash(plan)
    plan.update(id=digest, content_hash=digest)
    return plan


def validate_plan(plan, source_pcm_sha=None):
    body = {k: v for k, v in plan.items() if k not in ('id', 'content_hash')}
    if canonical_hash(body) != plan.get('content_hash') or plan.get('id') != plan.get('content_hash'):
        raise ValueError('段落计划内容校验失败')
    if source_pcm_sha and plan['source_pcm_sha'] != source_pcm_sha:
        raise ValueError('段落计划与原曲 PCM 不匹配')
    cursor = 0
    for section in plan['sections']:
        a, b = section['core']
        if a != cursor or b <= a or b > plan['samples']:
            raise ValueError('段落计划核心范围须连续且不重叠')
        cursor = b
    if cursor != plan['samples']:
        raise ValueError('段落计划未覆盖完整原曲')
    return plan
