"""Bounded, order-independent common-attack decisions for model heads.

Native rhythm is a local grouping hint, never a certified global timing map.
Every automatic move still needs positive multiscale attack evidence. Unknown
and positively distinct attacks have different outcomes in the audit.
"""
import math
from collections import Counter


def _finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def _rhythm_group(events, strict):
    hints = [event.get('model_rhythm') or {} for event in events]
    missing = any(not hint.get('available', True) or
                  hint.get('version') != 'v32-rhythm-v1' or
                  not _finite(hint.get('grid_time_ms')) or
                  not _finite(hint.get('grid_error_ms')) or
                  not _finite(hint.get('beat_length_ms')) or hint['beat_length_ms'] <= 0 or
                  not isinstance(hint.get('snap_divisor'), int) or hint['snap_divisor'] <= 0
                  for hint in hints)
    if missing:
        return {'compatible': False, 'reason': 'missing_native_rhythm_evidence'}
    intervals = [hint['beat_length_ms']/hint['snap_divisor'] for hint in hints]
    grids = [hint['grid_time_ms'] for hint in hints]
    errors = [abs(hint.get('grid_error_ms', math.inf)) for hint in hints]
    # Grid error limits qualification, not the distance a note is moved. Model
    # redlines themselves are quantized and can drift; no grid is forced on PCM.
    if any(error > min(30., interval/8) for error, interval in zip(errors, intervals)):
        return {'compatible': False, 'reason': 'native_grid_uncertain'}
    spread = max(grids)-min(grids)
    if spread > min(15., min(intervals)/4):
        return {'compatible': False, 'reason': 'native_grid_disagreement'}
    sources = []
    for event, hint in zip(events, hints):
        origin = (event.get('origins') or [{}])[0]
        sources.append((origin.get('source_id') or origin.get('revision_id') or
                        event.get('source_role'), hint.get('timing_fingerprint')))
    same_clock = len(set(sources)) == 1 and all(all(value for value in source) for source in sources)
    strict_ok = same_clock and spread < .001
    return {'compatible': not strict or strict_ok, 'strict_compatible': strict_ok,
            'reason': 'native_same_tick' if strict_ok else 'native_local_grid_agreement',
            'grid_times_ms': grids, 'grid_errors_ms': errors,
            'finest_interval_ms': min(intervals), 'same_source_clock': same_clock,
            'certified_global_timing': False}


def _peaks(proof, threshold, move):
    return [p for p in proof.get('onsets', []) if p.get('multiscale_agreement')
            and _finite(p.get('time_ms')) and _finite(p.get('strength'))
            and p['strength'] >= threshold and _finite(p.get('uncertainty_ms'))
            and 0 <= p['uncertainty_ms'] <= move]


def _independent(proofs, time, anchor, first, last, move):
    for proof in proofs:
        for peak in _peaks(proof, 1., move):
            if first-3 <= peak['time_ms'] <= last+3 and abs(peak['time_ms']-time) > max(
                    3., peak['uncertainty_ms']+anchor['uncertainty_ms']):
                return True
    return False


def _separate_attacks(events, proofs, move):
    """A midpoint cannot hide two independently resolved source attacks."""
    times = sorted({event['start_ms'] for event in events})
    sources = {}
    for proof in proofs:
        for peak in _peaks(proof, 1., move):
            if not times[0]-3 <= peak['time_ms'] <= times[-1]+3:
                continue
            distances = sorted((abs(t-peak['time_ms']),t) for t in times)
            if len(distances)>1 and abs(distances[0][0]-distances[1][0])<1e-6:
                continue  # Head ownership itself is ambiguous.
            key = (peak.get('source_role'),peak.get('channel'))
            sources.setdefault(key,{})[(peak['time_ms'],peak['uncertainty_ms'])] = (peak,distances[0][1])
    for observations in sources.values():
        values = list(observations.values())
        for i,(first,owner) in enumerate(values):
            for second,other in values[i+1:]:
                if owner != other and abs(first['time_ms']-second['time_ms']) > max(
                        3.,first['uncertainty_ms']+second['uncertainty_ms']):
                    return [{k:p.get(k) for k in ('time_ms','uncertainty_ms','strength',
                                                  'source_role','channel')} for p in (first,second)]
    return None


def select_attack(events, proofs, *, strict, move, subdivision=None, model_agreement=False):
    """Return a real acoustic/native anchor plus inspectable qualification."""
    explicit = {event.get('model_group_id') or proof.get('model_group_id')
                for event, proof in zip(events, proofs)} - {None}
    if len(explicit) > 1:
        return None, 'confirmed_distinct_model_groups', {'protected': True}
    if any(proof.get('independent_onset') or proof.get('subdivision_veto') for proof in proofs):
        return None, 'confirmed_independent_attacks', {'protected': True}
    if len({event['lane'] for event in events}) < len(events):
        return None, 'same_lane_separate_heads', {'protected': True}
    separate = _separate_attacks(events,proofs,move)
    if separate:
        return None,'confirmed_independent_attacks',{'protected':True,'separate_attacks':separate}
    rhythm = _rhythm_group(events, strict)
    if strict and not model_agreement and not rhythm['compatible']:
        return None, 'missing_model_group_or_subdivision_evidence', {'rhythm': rhythm}
    limit = move
    if rhythm['compatible']:
        limit = min(limit, rhythm['finest_interval_ms']/4)
    if subdivision is not None:
        limit = min(limit, float(subdivision)/4)
    first = min(event['start_ms'] for event in events)
    last = max(event['start_ms'] for event in events)
    feasible = lambda t: all(abs(event['start_ms']-t) <= limit and
                            (subdivision is None or abs(event['start_ms']-t) < float(subdivision)/4)
                            and (not rhythm['compatible'] or
                                 abs(event['start_ms']-t) < rhythm['finest_interval_ms']/4)
                            for event in events)
    per_head = [_peaks(proof, 1., limit) for proof in proofs]
    # Enumerate the whole group's measured anchors, not the first member's.
    # This is symmetric in lane, stem and array order; never average head times.
    candidates = []
    for anchor in [p for peaks in per_head for p in peaks]:
        t = anchor['time_ms']
        if not feasible(t):
            continue
        if all(any(abs(p['time_ms']-t) <= min(2., p['uncertainty_ms']) for p in peaks)
               for peaks in per_head) and not _independent(proofs, t, anchor, first, last, limit):
            candidates.append((t, anchor, 'all_heads_common_multiscale_attack'))
    if not candidates and rhythm['compatible']:
        # A model chord can contain a soft part without an individually strong
        # stem transient. Qualify the attack as a group, with native tick hints
        # and measured sound; absence of an individual peak is not a veto.
        measured = [p for proof in proofs for p in _peaks(proof, .6, limit)
                    if p.get('source_role') == 'original' or p['strength'] >= 1.]
        votes = Counter(event['start_ms'] for event in events)
        anchors = [(p['time_ms'], 'group_measured_attack') for p in measured]
        anchors += [(t, 'existing_model_head') for t in votes]
        anchors += [(t, 'native_rhythm_grid') for t in rhythm['grid_times_ms']]
        for t, kind in anchors:
            if not feasible(t):
                continue
            # Native anchors may correct token quantization within its half
            # step, but still have to intersect a measured attack interval.
            support = [p for p in measured if abs(p['time_ms']-t) <=
                       p['uncertainty_ms']+(5. if kind != 'group_measured_attack' else 0.)]
            for anchor in support:
                if not _independent(proofs, t, anchor, first, last, limit):
                    candidates.append((t, anchor, kind))
    if not candidates:
        reason = 'no_common_attack_evidence' if any(per_head) else 'missing_attack_evidence'
        return None, reason, {'rhythm': rhythm, 'eligible_peaks_per_head': [len(p) for p in per_head]}
    def rank(row):
        t, anchor, kind = row
        return (kind not in ('all_heads_common_multiscale_attack', 'group_measured_attack'),
                anchor.get('source_role') != 'original', -anchor['strength'],
                sum(abs(event['start_ms']-t) for event in events), t)
    time, anchor, kind = min(candidates, key=rank)
    return time, 'common_attack_confirmed', {'method': kind, 'rhythm': rhythm,
        'measured_attack': {k: anchor.get(k) for k in ('time_ms', 'strength', 'uncertainty_ms',
                                                     'source_role', 'channel', 'multiscale_agreement')},
        'movement_limit_ms': limit, 'certified_global_timing': False}


def select_legacy_attack(events, proofs, *, strict, move, subdivision=None, model_agreement=False):
    """Execute an already-frozen v2 task without silently changing its policy."""
    explicit = {e.get('model_group_id') or p.get('model_group_id') for e,p in zip(events,proofs)}-{None}
    veto = len(explicit)>1 or any(p.get('independent_onset') or p.get('subdivision_veto') for p in proofs)
    per_head = [_peaks(proof,1.,move) for proof in proofs]
    chosen = None
    for anchor in per_head[0] if per_head else []:
        time = anchor['time_ms']
        if all(abs(e['start_ms']-time)<=move and
               (subdivision is None or abs(e['start_ms']-time)<float(subdivision)/4) and
               any(abs(p['time_ms']-time)<=min(2.,p['uncertainty_ms']) for p in peaks)
               for e,peaks in zip(events,per_head)) and not _independent(
                   proofs,time,anchor,min(e['start_ms'] for e in events),max(e['start_ms'] for e in events),move):
            chosen = time;break
    if veto or strict and not model_agreement:chosen=None
    return chosen,'independent_onset_or_insufficient_sound_model_agreement',{'legacy_frozen_policy':True}
