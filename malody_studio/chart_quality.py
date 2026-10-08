"""Versioned evidence-safe selected-head quality policy for classic desktop 4K."""
import copy
import hashlib
import json
from collections import defaultdict
from bisect import bisect_left
from .difficulty import PRESETS
from .event_evidence import head_evidence

VERSION = 'chart-quality-v4'
_CONTRACT = {'version': VERSION, 'default_ln_ratio': .15, 'ratio_mode': 'soft_goal',
             'normal_span_ms': 15., 'strict_span_ms': 10., 'normal_move_ms': 10.,
             'strict_move_ms': 5., 'finest_subdivision_fraction': .25,
             'head_count_preserved': True, 'absolute_hold_tails_preserved_on_alignment': True,
             'rule_clipped_tails': 'recompute_from_model_release_after_head_alignment',
             'anchor_selection': 'symmetric_group_measured_or_native_anchor',
             'new_neighbour_group_policy': 'retain_original_when_full_span_membership_changes',
             'unresolved_is_acceptance_failure': True,
             'proof': 'independent_acoustic_common_anchor_and_model_agreement',
             'uncertainty_policy': 'retain_original', 'hold_policy': 'matched_stem_sustain_no_fabrication',
             'independent_onset_veto': 'reliable_multiscale_nonoverlapping_uncertainty_intervals'}


def contract(version=None):
    if version not in (None, VERSION, 'chart-quality-v3', 'chart-quality-v2'):
        raise ValueError('不支持的谱面质量策略版本')
    value = copy.deepcopy(_CONTRACT)
    if version in ('chart-quality-v2', 'chart-quality-v3'):
        value['version'] = version
        value.pop('new_neighbour_group_policy')
    if version == 'chart-quality-v2':
        value['version'] = version
        for key in ('rule_clipped_tails', 'anchor_selection', 'unresolved_is_acceptance_failure'):
            value.pop(key)
    value['hash'] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return value


def alignment_summary(decisions):
    chords = [row for row in decisions if row.get('type') in
              ('chord_aligned', 'chord_retained', 'chord_rollback')]
    unresolved = sum(row.get('type') == 'chord_rollback' or
                     row.get('type') == 'chord_retained' and
                     not row.get('qualification', {}).get('protected', False) and
                     row.get('reason') != 'evidenced_acoustic_singletons_do_not_create_chords'
                     for row in chords)
    return {'chord_unresolved': unresolved,
            'chord_rollback': sum(row.get('type') == 'chord_rollback' for row in chords),
            'alignment_status': 'needs_review' if unresolved else 'no_unresolved_candidates',
            'musical_quality_status': 'not_human_verified'}


def summarize_region(events, decisions, *, before_events=None, ln_target=.15):
    """Count an owned region from existing quality proof, without transforming it.

    When pre-quality selected heads are unavailable, the head-preserving policy
    and each recorded hold-to-tap's original tail reconstruct the before counts.
    Group counts describe corrections touching this region, not global totals.
    """
    ids={event['id'] for event in events}
    if len(ids)!=len(events):raise ValueError('区域质量摘要须有唯一音符身份')
    local=[]
    for decision in decisions:
        row=copy.deepcopy(decision)
        if 'event_id' in row:
            if row['event_id'] not in ids:continue
        elif 'event_ids' in row:
            row['event_ids']=[identity for identity in row['event_ids'] if identity in ids]
            if not row['event_ids']:continue
        elif 'moves' in row:
            row['moves']=[move for move in row['moves'] if move.get('event_id') in ids]
            if not row['moves']:continue
        elif 'note_ids' in row:
            row['note_ids']=[identity for identity in row['note_ids'] if identity in ids]
            if not row['note_ids']:continue
        else:
            # A span-only decision has no proven regional ownership.
            continue
        local.append(row)
    ln_after=sum(event.get('end_ms') is not None for event in events)
    if before_events is not None:
        if {event['id'] for event in before_events}!=ids or len(before_events)!=len(events):
            raise ValueError('质量前后区域音符头身份不符')
        ln_before=sum(event.get('end_ms') is not None for event in before_events)
        proof='selected_pre_quality_events'
    else:
        converted={row['event_id'] for row in local if row.get('type')=='hold_to_tap'
                   and row.get('original_end_ms') is not None}
        # Repeated audit records cannot multiply a single converted head.
        now_taps={event['id'] for event in events if event.get('end_ms') is None}
        ln_before=ln_after+len(converted & now_taps)
        proof='head_preservation_contract_and_recorded_original_hold_tails'
    chord=[row for row in local if row.get('type') in ('chord_aligned','chord_retained','chord_rollback')]
    uncertain=sum(row.get('type')=='chord_retained' and
                  not row.get('qualification',{}).get('protected',False) and
                  row.get('reason')!='evidenced_acoustic_singletons_do_not_create_chords' for row in chord)
    corrected=sum(row.get('type')=='chord_aligned' for row in chord)
    return {'summary':{'heads_before':len(events),'heads_after':len(events),
            'chord_candidates':len(chord),'chord_corrected':corrected,
            'chord_retained':len(chord)-uncertain-corrected,'chord_uncertain':uncertain,
            'ln_before':ln_before,'ln_after':ln_after,'ln_target':min(1.,max(0.,float(ln_target))),
            'ln_protected':len({row['event_id'] for row in local if row.get('type')=='hold_protected'}),
            'scope':'owned_region','before_evidence':proof,**alignment_summary(local)},'decisions':local}


def _proof(event, evidence):
    supplied = (evidence.get('heads', {}) or {}).get(str(event.get('id')), {})
    return {**head_evidence(event, evidence), **supplied}


def _model_group(event):
    # Acoustic leakage identity is deliberately not accepted as chord proof.
    value = event.get('model_group_id') or event.get('audio_evidence', {}).get('model_group_id')
    if value:
        return value
    origins = event.get('origins') or []
    keys = [(p['revision_id'], p['original_start_ms']) for p in origins
            if p.get('revision_id') is not None and p.get('original_start_ms') is not None]
    return keys[0] if keys else None


def classify_holds(events, evidence):
    """Before lane planning, reject only acoustically weak existing holds.

    There is no ratio, stacking or lane judgment on unselected model supply.
    Raw parents remain untouched and each head remains available to the beam.
    """
    output = copy.deepcopy(events); decisions = []
    for event in output:
        if event.get('end_ms') is None:
            continue
        proof = _proof(event, evidence or {})
        support = proof.get('sustain_support')
        if support is None or proof.get('important_sustain'):
            continue
        value = float(support)-.2*int(proof.get('rearticulations', 0))
        if value < .75:
            decisions.append({'type': 'hold_to_tap_before_planning', 'event_id': event.get('id'),
                              'start_ms': event['start_ms'], 'original_end_ms': event['end_ms'],
                              'origins': copy.deepcopy(event.get('origins', [])),
                              'reason': 'weak_sustain_evidence', 'evidence': proof})
            event['end_ms'] = None
    return {'events': output, 'decisions': decisions, 'version': VERSION}


def _caps(event, settings, difficulty, plan):
    caps = {**PRESETS.get(difficulty, PRESETS['hard']), **settings.get('difficulty_rules', {}).get(difficulty, {})}
    for section in (plan or {}).get('sections', []):
        core = section.get('core') or section.get('range') or section.get('source_range')
        if core and core[0] * 1000 / 44100 <= event['start_ms'] < core[1] * 1000 / 44100:
            detail = (section.get('per_difficulty') or section.get('perDifficulty') or {}).get(difficulty, {})
            hard = detail.get('hard_caps', {})
            caps.update(gap=hard.get('min_lane_gap_ms', caps['gap']), chord=hard.get('chord', caps['chord']),
                        peak=hard.get('peak_1s',caps['peak']),hold_ms=hard.get('hold_max_ms',caps['hold_ms']))
            caps['release_gap_ms'] = hard.get('release_gap_ms', 35)
            break
    return caps


def _conflict(before, trial, changed, settings, difficulty, plan):
    before_times=sorted(e['start_ms'] for e in before)
    trial_times=sorted(e['start_ms'] for e in trial)
    groups = defaultdict(list)
    for event in trial:
        groups[event['start_ms']].append(event)
    for index in changed:
        event = trial[index]; caps = _caps(event, settings, difficulty, plan)
        lower=min(before[index]['start_ms'],event['start_ms'])-1000
        upper=max(before[index]['start_ms'],event['start_ms'])
        for timestamp in set(before_times+trial_times):
            if not lower<timestamp<=upper:continue
            count=bisect_left(trial_times,timestamp+1000)-bisect_left(trial_times,timestamp)
            previous=bisect_left(before_times,timestamp+1000)-bisect_left(before_times,timestamp)
            ceilings=[]
            for section in (plan or {}).get('sections',[]):
                core=section.get('core') or section.get('range') or section.get('source_range')
                if core and timestamp<core[1]*1000/44100 and timestamp+1000>core[0]*1000/44100:
                    ceilings.append(_caps({'start_ms':core[0]*1000/44100},settings,difficulty,plan)['peak'])
            ceiling=min(ceilings) if ceilings else caps['peak']
            if count>ceiling and count>previous:return 'rolling_peak_cap'
        if len(groups[event['start_ms']]) > caps['chord']:
            return 'chord_cap'
        if event.get('end_ms') is not None and event['end_ms'] <= event['start_ms']:
            return 'hold_tail_before_head'
        if event.get('end_ms') is not None and event['end_ms']-event['start_ms'] < 100:
            return 'hold_minimum_duration'
        if event.get('end_ms') is not None and event['end_ms']-event['start_ms'] > caps['hold_ms']+1e-6:
            return 'hold_duration_cap'
        for section in (plan or {}).get('sections', []):
            core = section.get('core') or section.get('range') or section.get('source_range')
            if core:
                a, b = (v * 1000 / 44100 for v in core)
                if (a <= before[index]['start_ms'] < b) != (a <= event['start_ms'] < b):
                    return 'section_seam'
        boundaries = (plan or {}).get('region_boundaries', [])
        for boundary in boundaries:
            time = boundary*1000/44100
            if (before[index]['start_ms'] < time) != (event['start_ms'] < time):
                return 'region_seam'
        for j, other in enumerate(trial):
            if j == index or other['lane'] != event['lane']:
                continue
            earlier, later = sorted((event, other), key=lambda p: p['start_ms'])
            other_caps=_caps(other,settings,difficulty,plan)
            if later['start_ms']-earlier['start_ms'] < max(caps['gap'],other_caps['gap']):
                return 'lane_gap'
            if earlier.get('end_ms') is not None and later['start_ms'] < earlier['end_ms']+max(caps.get('release_gap_ms',35),other_caps.get('release_gap_ms',35)):
                return 'hold_occupancy'
    return None


def _new_neighbour_groups(before, trial, changed, span):
    """Reject partial repairs that pull unqualified outside heads into a group.

    Use the same complete, first-head-anchored span as candidate grouping. An
    outside head may already be near the LAST member of the old group; minimum
    pair distance would miss this regression. Never extend the group by chains.
    """
    changed = set(changed)
    old_times = [before[index]['start_ms'] for index in changed]
    new_times = [trial[index]['start_ms'] for index in changed]
    blocked = []
    for index, other in enumerate(before):
        if index in changed:
            continue
        old_span = max(*old_times, other['start_ms'])-min(*old_times, other['start_ms'])
        new_span = max(*new_times, trial[index]['start_ms'])-min(*new_times, trial[index]['start_ms'])
        if old_span > span and new_span <= span:
            blocked.append({'event_id':other['id'],'outside_start_ms':other['start_ms'],
                            'before_full_span_ms':old_span,'attempted_full_span_ms':new_span,
                            'span_limit_ms':span})
    return blocked


def apply(events, settings, evidence, difficulty, pattern, timing=None, plan=None, policy=None):
    """Transform only a deep copy of selected events; unknown evidence retains heads."""
    settings = settings or {}; evidence = evidence or {}; timing = timing or {}
    policy = policy or contract()
    if policy != contract(policy.get('version')):
        raise ValueError('谱面质量策略哈希校验失败')
    legacy = policy['version'] == 'chart-quality-v2'
    output = copy.deepcopy(events); decisions = []
    proofs = [_proof(e, evidence) for e in output]
    target = min(1., max(0., float(settings.get('ln_ratio', .15))))
    before_holds = sum(e.get('end_ms') is not None for e in output)
    # Sustain requires spectral continuity AND no repeated attacks. Energy alone
    # cannot protect a long vocal phrase over unrelated accompaniment.
    protected = []; ranked = []
    for index, event in enumerate(output):
        if event.get('end_ms') is None:
            continue
        proof = proofs[index]
        support = proof.get('sustain_support')
        if support is None:
            decisions.append({'type': 'hold_uncertain', 'event_id': event.get('id'),
                              'start_ms': event['start_ms'], 'reason': 'missing_sustain_evidence'})
            continue
        value = float(support)-.2*int(proof.get('rearticulations', 0))
        strong = support >= .92 and proof.get('rearticulations', 0) == 0 and proof.get('release_supported', False)
        if strong or proof.get('important_sustain', False):
            protected.append(index)
            decisions.append({'type': 'hold_protected', 'event_id': event.get('id'), 'start_ms': event['start_ms'],
                              'reason': 'spectral_continuity_release_or_important_sustain', 'evidence': proof})
        else:
            ranked.append((value, index))
    allowance = max(0, round(len(output)*target)-len(protected))
    kept = protected[:]
    accepted = 0
    def stacks(index, others):
        event = output[index]
        return any(max(event['start_ms'], output[j]['start_ms']) < min(event['end_ms'], output[j]['end_ms'])
                   for j in others if j != index)
    for index in protected:
        if stacks(index, protected):
            decisions.append({'type': 'hold_stack_exception', 'event_id': output[index].get('id'),
                              'start_ms': output[index]['start_ms'], 'reason': 'evidence_supported_protected_sustain'})
    for value, index in sorted(ranked, reverse=True):
        local_stack = stacks(index, kept)
        if value < .75 or accepted >= allowance or local_stack:
            event = output[index]
            decisions.append({'type': 'hold_to_tap', 'event_id': event.get('id'), 'start_ms': event['start_ms'],
                              'original_end_ms': event['end_ms'], 'origins': copy.deepcopy(event.get('origins', [])),
                              'reason': 'weak_sustain' if value < .75 else ('local_hold_stacking' if local_stack else 'soft_ln_style_goal'),
                              'evidence': proofs[index]})
            event['end_ms'] = None
        else:
            kept.append(index); accepted += 1
    strict = difficulty in ('master', 'lunatic') or pattern == 'technical'
    span = 10. if strict else 15.; move = 5. if strict else 10.
    subdivision = timing.get('finest_reliable_subdivision_ms', evidence.get('finest_reliable_subdivision_ms'))
    if subdivision is not None:
        move = min(move, float(subdivision)/4)
    exact = defaultdict(list)
    for index, event in enumerate(output):
        exact[event['start_ms']].append(index)
    groups = sorted(exact.items()); candidates = corrected = uncertain = retained = 0; used = set()
    for position, (timestamp, indices) in enumerate(groups):
        if timestamp in used:
            continue
        nearby = [(t, ids) for t, ids in groups[position:] if 0 <= t-timestamp <= span and t not in used]
        if len(nearby) < 2:
            continue
        candidates += 1
        members = [index for _, ids in nearby for index in ids]
        used.update(t for t, _ in nearby)  # Full-span cluster; never chain extension.
        if any(output[i].get('candidate_provenance',{}).get('kind')=='acoustic'
               or any(p.get('candidate_kind')=='acoustic' for p in output[i].get('origins',[]))
               for i in members):
            retained += 1
            decisions.append({'type':'chord_retained','event_ids':[output[i].get('id') for i in members],
                              'reason':'evidenced_acoustic_singletons_do_not_create_chords'})
            continue
        from .accent_alignment import select_attack, select_legacy_attack
        agreed = {str(_model_group(output[i])) for i in members}
        model_agreement = len(agreed) == 1 and 'None' not in agreed
        if strict:
            model_agreement = model_agreement and all(proofs[i].get('subdivision_agreement', False) for i in members)
        selector = select_legacy_attack if legacy else select_attack
        chosen, reason, qualification = selector([output[i] for i in members],
            [proofs[i] for i in members], strict=strict, move=move,
            subdivision=subdivision, model_agreement=model_agreement)
        if chosen is None:
            if qualification.get('protected'): retained += 1
            else: uncertain += 1
            decisions.append({'type': 'chord_retained', 'event_ids': [output[i].get('id') for i in members],
                              'reason': reason, 'qualification': qualification})
            continue
        trial = output[:]
        tail_changes = []
        for index in members:
            trial[index]=copy.deepcopy(output[index])
            trial[index]['start_ms'] = chosen
            tail_policy = output[index].get('tail_policy', {})
            if not legacy and output[index].get('end_ms') is not None and tail_policy.get('kind') == 'rule_cap':
                original_tail = tail_policy.get('model_end_ms')
                cap = _caps(trial[index], settings, difficulty, plan)['hold_ms']
                if isinstance(original_tail, (int, float)):
                    trial[index]['end_ms'] = min(original_tail, chosen+cap)
                    tail_changes.append({'event_id': output[index].get('id'),
                        'from_ms': output[index]['end_ms'], 'to_ms': trial[index]['end_ms'],
                        'model_end_ms': original_tail, 'cap_ms': cap,
                        'reason': 'recompute_artificial_rule_cap_after_head_alignment'})
        neighbours = _new_neighbour_groups(output, trial, members, span) if policy['version'] == VERSION else []
        conflict = 'new_neighbor_group_needs_review' if neighbours else _conflict(output, trial, members, settings, difficulty, plan)
        if conflict:
            retained += 1
            decisions.append({'type': 'chord_rollback', 'event_ids': [output[i].get('id') for i in members],
                              'reason': conflict, 'qualification': qualification, 'attempted_anchor_ms': chosen,
                              **({'blocked_neighbors':neighbours} if neighbours else {})})
            continue
        identities = [output[i].get('id') for i in members]
        if not legacy: identities = sorted(identities, key=str)
        group_id = 'chord-' + hashlib.sha256(repr((policy['version'], identities, chosen)).encode()).hexdigest()[:20]
        decisions.append({'type': 'chord_aligned', 'chord_group_id': group_id, 'anchor_ms': chosen,
                          'qualification': qualification, 'tail_changes': tail_changes,
                          'moves': [{'event_id': output[i].get('id'), 'from_ms': output[i]['start_ms'], 'to_ms': chosen,
                                     'origins': copy.deepcopy(output[i].get('origins', []))} for i in members]})
        for index in members:
            trial[index]['chord_group_id'] = group_id
        output = trial; corrected += 1
    output.sort(key=lambda e: (e['start_ms'], e['lane']))
    return {'version': policy['version'], 'contract': policy, 'events': output, 'decisions': decisions,
            'summary': {'heads_before': len(events), 'heads_after': len(output), 'chord_candidates': candidates,
                        'chord_corrected': corrected, 'chord_retained': retained, 'chord_uncertain': uncertain,
                        'ln_before': before_holds, 'ln_after': sum(e.get('end_ms') is not None for e in output),
                        'ln_target': target, 'ln_protected': len(protected), **alignment_summary(decisions)}}
