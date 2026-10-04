"""Evidence-aware two-source selection and four-lane planning under one budget."""
import copy
from collections import Counter
import math

from .advanced import SR, valid_events
from .difficulty import PRESETS
from .separation import canonical_hash

VERSION = 'stem-fusion-v3'


def _origin(revision, event, role):
    prov = revision.get('provenance', {})
    return {'revision_id': revision['id'], 'note_id': event['id'], 'source_id': prov.get('source_id'),
            'stem_role': role, 'anchor_id': event.get('anchor_id'),
            'original_start_ms': event['start_ms'], 'original_end_ms': event.get('end_ms'), 'original_lane': event['lane']}


def _caps(section, key, settings):
    detail = (section.get('per_difficulty') or section.get('perDifficulty') or section.get('difficulties') or {}).get(key, {})
    preset = {**PRESETS[key], **settings.get('difficulty_rules', {}).get(key, {})}
    hard = detail.get('hard_caps', {})
    core = section.get('core') or section.get('range') or section.get('source_range')
    span = (core[1] - core[0]) / SR
    return {'target': max(0, float(detail.get('target_heads_soft', detail.get('target_heads', detail.get('target_rate', preset['rate']) * span)))),
            'peak': int(hard.get('peak_1s', preset['peak'])), 'chord': int(hard.get('chord', preset['chord'])),
            'gap': float(hard.get('min_lane_gap_ms', preset['gap'])), 'release': float(hard.get('release_gap_ms', 35)),
            'hold': float(hard.get('hold_max_ms', preset['hold_ms']))}


def _supported(candidate):
    evidence = candidate.get('audio_evidence', {})
    # Audio score is absolute to the mixture; quiet stem self-normalization must
    # not make artifacts dominate the other voice. Missing evidence is neutral.
    return max(.05, min(4., float(evidence.get('salience', candidate.get('strength', 1.)))))


def _deduplicate(candidates):
    """Only explicit shared acoustic identity merges; proximity alone never does."""
    result = []; decisions = []
    known = {}
    for item in candidates:
        identity = item.get('audio_evidence', {}).get('shared_event_id')
        if identity is not None:
            # Match one cross-source head at a time; a model chord inside one
            # source is not leakage and retains its intended multiplicity.
            other = next((candidate for candidate in known.get(identity, [])
                          if abs(candidate['start_ms'] - item['start_ms']) <= 12
                          and item['source_role'] not in {origin['stem_role'] for origin in candidate['origins']}), None)
            if other:
                # Keep one complete model event from the better supported stem.
                # Mixing the weak copy's head with the strong copy's hold tail
                # invents a third event and can turn leakage into a false hold.
                representative, discarded = (item, other) if item['salience'] > other['salience'] else (other, item)
                discarded_origins = copy.deepcopy(discarded['origins'])
                origins = copy.deepcopy(representative['origins']) + discarded_origins
                kept = copy.deepcopy(representative)
                other.clear(); other.update(kept); other['origins'] = origins
                decisions.append({'type': 'duplicate_acoustic_event', 'origins': discarded_origins,
                                  'kept_origins': copy.deepcopy(origins), 'evidence': identity,
                                  'representative': {'source_role': other['source_role'], 'note_id': other['id'],
                                                     'start_ms': other['start_ms'], 'end_ms': other.get('end_ms')},
                                  'reason': '同源声学证据确认重复；采用原曲相对能量更强声部的完整原事件，原谱保留'})
                continue
            known.setdefault(identity, []).append(item)
        result.append(item)
    return result, decisions


def _unconfirmed_overlaps(rows):
    """Near heads are an uncertainty count, never proof of the same sound."""
    recent = []; count = 0
    for item in rows:
        recent = [other for other in recent if item['start_ms'] - other['start_ms'] <= 12]
        count += sum(other['source_role'] != item['source_role'] for other in recent)
        recent.append(item)
    return count


def _budget_windows(a, b, target, profile):
    """Reserve one shared budget across time, without borrowing future sound."""
    windows = []
    for start in range(a, b, 2 * SR):
        end = min(b, start + 2 * SR)
        mass = sum(max(0, min(end, row['end_sample']) - max(start, row['start_sample']))
                   * row['active_fraction'] for row in profile) if profile else end - start
        windows.append({'source_range': [start, end], 'active_mass': max(0., mass)})
    total = sum(row['active_mass'] for row in windows)
    # A small section-wide soft allowance must not multiply with window count.
    limit = math.ceil(target) + 2 if target > 0 and total > 0 else 0
    for row in windows:
        weight = row['active_mass'] / total if total else 0.
        row['target_heads'] = target * weight
        row['quota_exact'] = limit * weight
        row['max_heads'] = math.floor(row['quota_exact'])
    spare = limit - sum(row['max_heads'] for row in windows)
    groups = {}
    for i, row in enumerate(windows):
        remainder = round(row['quota_exact'] - row['max_heads'], 12)
        groups.setdefault(remainder, []).append(i)
    for remainder in sorted(groups, reverse=True):
        indices = groups[remainder]
        take = min(spare, len(indices))
        # Equal remainders are common in steady music. Spread those integer
        # heads through time instead of assigning every tie to the first bars.
        for j in range(take):
            i = indices[math.floor((j + .5) * len(indices) / take)]
            windows[i]['max_heads'] += 1
        spare -= take
        if not spare:
            break
    for row in windows:
        row.pop('quota_exact'); row.pop('active_mass')
    return windows


def fuse_revisions(vocals, accompaniment, section_plan, settings=None):
    settings = settings or vocals['settings']
    if vocals['variant'] != accompaniment['variant'] or vocals['range'] != accompaniment['range']:
        raise ValueError('融合的两个原谱须属于相同组合和源片段范围')
    for role, revision in (('vocals', vocals), ('accompaniment', accompaniment)):
        prov = revision.get('provenance', {})
        if prov.get('source_role') != role:
            raise ValueError('请选择人声原谱和伴奏原谱，不能用单音源替代')
    vp, ap = vocals['provenance'], accompaniment['provenance']
    if not vp.get('stem_set_id') or vp['stem_set_id'] != ap.get('stem_set_id') or vp.get('parent_source_id') != ap.get('parent_source_id'):
        raise ValueError('融合原谱来自不同分离结果或原曲')
    if vp.get('source_id') == ap.get('source_id'):
        raise ValueError('融合须使用两个不同音源')
    start, end = vocals['range']; pattern, key = vocals['variant'].split('--')
    plan_hash = section_plan.get('content_hash') or canonical_hash(section_plan)
    sections = section_plan.get('sections', [])
    if not sections:
        raise ValueError('融合缺少共享段落难度计划')
    source_sha = section_plan.get('source_pcm_sha')
    if source_sha and source_sha != vp.get('parent_source_id'):
        raise ValueError('共享难度计划与原曲 PCM 不匹配')
    recipe = {'version': VERSION, 'parents': [vocals['id'], accompaniment['id']], 'plan_hash': plan_hash,
              'settings': settings, 'range': [start, end], 'variant': vocals['variant']}
    recipe_hash = canonical_hash(recipe)
    candidates = []; silent_decisions = []
    for role, revision in (('vocals', vocals), ('accompaniment', accompaniment)):
        for event in revision['events']:
            item = copy.deepcopy(event)
            item['origins'] = [_origin(revision, event, role)]
            item['source_role'] = role
            item['salience'] = _supported(item)
            if item.get('audio_evidence', {}).get('audible') is False:
                silent_decisions.append({'type': 'omitted', 'origins': item['origins'], 'reason': '声部在原曲相对能量门槛下没有可用音频证据；原谱保留'})
                continue
            candidates.append(item)
    candidates.sort(key=lambda e: (e['start_ms'], e['source_role'], e['lane'], e['id']))
    candidates, decisions = _deduplicate(candidates)
    # A stronger representative may lie after its weaker copy or across a core
    # boundary. Apply ownership and rolling constraints to its actual timestamp.
    candidates.sort(key=lambda e: (e['start_ms'], e['source_role'], e['lane'], e['id']))
    decisions = silent_decisions + decisions
    output = []; last = [-1e9] * 4; occupied = [-1e9] * 4; lane_counts = [0] * 4; reports = []
    covered = set()
    for section in sorted(sections, key=lambda x: (x.get('core') or x.get('range') or x.get('source_range'))[0]):
        core = section.get('core') or section.get('range') or section.get('source_range')
        a, b = max(start, core[0]), min(end, core[1])
        if a >= b:
            continue
        caps = _caps(section, key, settings)
        # Only the selected core consumes budget. Context is read-only.
        profile = section.get('profile', [])
        if profile and section.get('active_seconds', 0) > 0:
            selected_active = sum(max(0, min(b, row['end_sample']) - max(a, row['start_sample'])) / SR * row['active_fraction'] for row in profile)
            caps['target'] *= min(1., selected_active / section['active_seconds'])
        else:
            caps['target'] *= (b - a) / (core[1] - core[0])
        rows = [e for e in candidates if a <= e['start_ms'] * SR / 1000 < b]
        if any(e['id'] + e['source_role'] in covered for e in rows):
            raise ValueError('共享段落计划核心范围不能重叠')
        covered.update(e['id'] + e['source_role'] for e in rows)
        windows = _budget_windows(a, b, caps['target'], profile)
        # Reserve density throughout the selected audio. A chronological beam
        # cannot recover early routes discarded before later audio is visited;
        # a section-wide count penalty would spend everything at the front.
        # Lane occupancy and rolling peak constraints still span every window.
        recent = [(e['start_ms'], e['lane']) for e in output if e['start_ms'] > a * 1000 / SR - 1000]
        states = [{'score': 0., 'events': [], 'last': last[:], 'occupied': occupied[:], 'counts': lane_counts[:], 'recent': recent, 'roles': Counter(), 'window_counts': Counter()}]
        for item in rows:
            timestamp = item['start_ms']; original_tail = item.get('end_ms')
            window_index = min(len(windows) - 1, int((timestamp * SR / 1000 - a) // (2 * SR)))
            window = windows[window_index]
            options = []
            for state in states:
                options.append(state)
                count = state['window_counts'][window_index]
                if count >= window['max_heads']:
                    continue
                active = [(t, lane) for t, lane in state['recent'] if t > timestamp - 1000]
                chord = sum(abs(t - timestamp) <= .1 for t, _ in active)
                if len(active) >= caps['peak'] or chord >= caps['chord']:
                    continue
                prev = state['events'][-1]['lane'] if state['events'] else (output[-1]['lane'] if output else None)
                for lane in range(4):
                    if timestamp - state['last'][lane] < caps['gap'] or timestamp < state['occupied'][lane] + caps['release']:
                        continue
                    tail = original_tail
                    if tail is not None and tail - timestamp > caps['hold']:
                        # Keep raw untouched; fusion reports its capped candidate.
                        tail = timestamp + caps['hold']
                    if tail is not None and tail - timestamp < 100:
                        tail = None
                    excess_penalty = max(0., count + 1 - window['target_heads']) * .8
                    repeat_penalty = .18 if lane == prev else 0.
                    if pattern == 'jackspeed': repeat_penalty = -.16 if lane == prev else 0.
                    hand_penalty = .12 if pattern == 'speed' and prev is not None and lane // 2 == prev // 2 else 0.
                    preference = .15 if lane == item['lane'] else 0.
                    coverage = .2 / (1 + state['roles'][item['source_role']])
                    score = state['score'] + item['salience'] + preference + coverage - repeat_penalty - hand_penalty - excess_penalty
                    event = {k: copy.deepcopy(item[k]) for k in ('start_ms', 'origins')}
                    identity = {'recipe': recipe_hash, 'origins': event['origins']}
                    event.update(id='fusion-' + canonical_hash(identity)[:32], lane=lane, end_ms=tail)
                    event['source_id'] = item['origins'][0]['source_id']
                    event['fusion_original_id'] = (item['source_role'], item['id'])
                    child = {'score': score, 'events': state['events'] + [event], 'last': state['last'][:],
                             'occupied': state['occupied'][:], 'counts': state['counts'][:], 'recent': active + [(timestamp, lane)], 'roles': state['roles'].copy(),
                             'window_counts': state['window_counts'].copy()}
                    child['last'][lane] = timestamp; child['occupied'][lane] = tail or timestamp; child['counts'][lane] += 1
                    child['roles'][item['source_role']] += 1
                    child['window_counts'][window_index] += 1
                    options.append(child)
            options.sort(key=lambda st: (-st['score'], len(st['events']), tuple((e['start_ms'], e['lane'], e['id']) for e in st['events'])))
            # Keep diverse occupancy states; identical routes need one survivor.
            states = []; fingerprints = set()
            for state in options:
                fingerprint = (tuple(state['last']), tuple(state['occupied']), len(state['events']), state['window_counts'][window_index])
                if fingerprint not in fingerprints:
                    fingerprints.add(fingerprint); states.append(state)
                if len(states) >= 16:
                    break
        winner = states[0]
        chosen = {e['fusion_original_id']: e for e in winner['events']}
        for item in rows:
            event = chosen.get((item['source_role'], item['id']))
            if event is None:
                decisions.append({'type': 'omitted', 'origins': item['origins'], 'reason': '共享密度预算、峰值或四轨可玩约束优先保留其他候选'})
            elif event['lane'] != item['lane'] or event['end_ms'] != item.get('end_ms'):
                decisions.append({'type': 'replanned', 'origins': item['origins'], 'lane': event['lane'], 'end_ms': event['end_ms'],
                                  'reason': '四轨共享排键或长条上限；原谱保留'})
        output.extend(winner['events']); last = winner['last']; occupied = winner['occupied']; lane_counts = winner['counts']
        for i, window in enumerate(windows):
            wa, wb = window['source_range']
            window['candidate_heads'] = sum(wa <= e['start_ms'] * SR / 1000 < wb for e in rows)
            window['selected_heads'] = winner['window_counts'][i]
        reports.append({'section_id': section.get('id'), 'source_range': [a, b], 'target_heads': caps['target'],
                        'candidate_heads': len(rows), 'selected_heads': len(winner['events']), 'hard_caps': caps,
                        'unconfirmed_cross_source_overlaps': _unconfirmed_overlaps(rows),
                        'budget_windows': windows,
                        'source_heads': dict(winner['roles']), 'saturated': len(winner['events']) < caps['target']})
    if len(covered) < len(candidates):
        raise ValueError('共享段落难度计划没有覆盖全部融合候选')
    output.sort(key=lambda e: (e['start_ms'], e['lane']))
    for event in output: event.pop('fusion_original_id', None)
    max_tail = max([end * 1000 / SR] + [e.get('end_ms') or e['start_ms'] for e in output])
    valid_events(output, start * 1000 / SR, end * 1000 / SR, max_tail)
    return {'events': output, 'decisions': decisions, 'stats': reports,
            'provenance': {**recipe, 'recipe_hash': recipe_hash, 'fusion_version': VERSION,
                           'source_role': 'fusion', 'source_id': 'fusion:' + recipe_hash,
                           'stem_set_id': vp['stem_set_id'], 'parent_source_id': vp['parent_source_id'],
                           'applied_plan_hash': plan_hash, 'global_budget_applied': True,
                           'overlap_policy': 'shared_acoustic_identity_only; strongest_complete_event; no_synthetic_heads',
                           'budget_policy': '2s_active_audio_windows; largest_remainder_shared_allowance; no_future_budget_borrowing',
                           'section_stats': reports, 'decisions': decisions}}
