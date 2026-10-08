"""Evidence-aware two-source selection and four-lane planning under one budget."""
import copy
from collections import Counter
from bisect import bisect_left
import math

from .advanced import SR, valid_events
from .difficulty import PRESETS
from .separation import canonical_hash

VERSION = 'stem-fusion-v6'


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


def _budget_windows(a, b, target, profile, phrases=None):
    """Reserve one shared budget across time, without borrowing future sound."""
    windows = []
    ranges=[(max(a,row['range'][0]),min(b,row['range'][1])) for row in phrases or [] if row['range'][0]<b and row['range'][1]>a]
    if not ranges:ranges=[(start,min(b,start+2*SR)) for start in range(a,b,2*SR)]
    for start,end in ranges:
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


def _lane_preference(item, lane):
    # An acoustic singleton has no model lane; lane0 is a storage placeholder.
    return .15 if item['source_role']!='original_acoustic' and lane==item['lane'] else 0.


def _insertion_lanes(item, existing, caps):
    """Only a proven impossible insertion is a hard-constraint omission."""
    timestamp=item['start_ms'];tail=item.get('end_ms')
    if tail is not None:
        tail=min(tail,timestamp+caps['hold'])
        if tail-timestamp<100:tail=None
    # Older released events cannot block insertion. Retain every active hold
    # plus the full gap/peak/possible-tail neighborhood in both directions.
    # Broaden inspection for custom adjacent caps, without applying a global
    # strictest rule: pair/window checks below still use only affected sections.
    inspected_gap=max([caps['gap']]+[row['gap'] for row in caps.get('section_caps',[])])
    inspected_release=max([caps['release']]+[row['release'] for row in caps.get('section_caps',[])])
    before_span=max(1000.,inspected_gap);after_span=max(1000.,inspected_gap,caps['hold'])+inspected_release
    existing=[e for e in existing if timestamp-before_span<e['start_ms']<=timestamp+after_span
              or (e['start_ms']<=timestamp and (e.get('end_ms') or e['start_ms'])+inspected_release>timestamp)]
    times=sorted(e['start_ms'] for e in existing)
    rolling=sorted(times+[timestamp]);peak=False
    for i,time in enumerate(rolling):
        if time>timestamp:break
        if time<=timestamp-1000:continue
        ceilings=[row['peak'] for row in caps.get('section_caps',[]) if time<row['range_ms'][1] and time+1000>row['range_ms'][0]] or [caps['peak']]
        if bisect_left(rolling,time+1000)-i>min(ceilings):
            peak=True;break
    if peak:return [],['rolling_peak_cap']
    if sum(abs(e['start_ms']-timestamp)<=.1 for e in existing)>=caps['chord']:
        return [],['chord_cap']
    blocked=[];legal=[]
    for lane in range(4):
        before=[e for e in existing if e['lane']==lane and e['start_ms']<=timestamp]
        after=[e for e in existing if e['lane']==lane and e['start_ms']>timestamp]
        previous=max(before,key=lambda e:e['start_ms']) if before else None
        following=min(after,key=lambda e:e['start_ms']) if after else None
        reasons=[]
        def pair_caps(other):
            policies=[row for row in caps.get('section_caps',[]) if row['range_ms'][0]<=other['start_ms']<row['range_ms'][1]]
            return max([caps['gap']]+[row['gap'] for row in policies]),max([caps['release']]+[row['release'] for row in policies])
        if previous:
            gap,release=pair_caps(previous)
            if timestamp-previous['start_ms']<gap:reasons.append('previous_lane_gap')
            if previous.get('end_ms') is not None and timestamp<previous['end_ms']+release:reasons.append('previous_hold_occupancy')
        if following:
            gap,release=pair_caps(following)
            if following['start_ms']-timestamp<gap:reasons.append('following_lane_gap')
            if tail is not None and following['start_ms']<tail+release:reasons.append('following_hold_occupancy')
        if not reasons:legal.append(lane)
        blocked.extend(reasons)
    return legal,sorted(set(blocked))


def _omission_category(item, existing, caps):
    lanes,reasons=_insertion_lanes(item,existing,caps)
    return ('soft_selection',[]) if lanes else ('hard_constraint',reasons)


def fuse_revisions(vocals, accompaniment, section_plan, settings=None, evidence=None, acoustic=None, timing=None, preceding_events=None, audio_vote_cap=None, model_energy_evidence_id=None, selection_policy="model_only", fill_unused_quota=True, fixed_skeleton_events=None, following_events=None):
    if selection_policy not in ('ranked_beam','model_skeleton_then_audio','model_only'):raise ValueError('Unknown fusion selection policy')
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
              'settings': settings, 'range': [start, end], 'variant': vocals['variant'], 'selection_policy':selection_policy}
    if model_energy_evidence_id is not None:
        recipe['model_energy_evidence_id']=model_energy_evidence_id
    if audio_vote_cap is not None:
        minimum=0. if selection_policy=='model_only' else 1e-12
        if not math.isfinite(float(audio_vote_cap)) or not minimum<=float(audio_vote_cap)<=4:
            raise ValueError('声学单头证据权重上限无效')
        recipe['acoustic_base_vote_cap']=float(audio_vote_cap)
    if acoustic is not None:
        recipe['acoustic_evidence_id'] = acoustic.get('id', acoustic.get('cache_key'))
    if evidence is not None:
        from .chart_quality import contract
        recipe['quality_evidence_id'] = evidence.get('id', evidence.get('cache_key'))
        recipe['preplanning_quality_policy'] = contract()
    following_events=sorted(following_events or [],key=lambda e:(e['start_ms'],e['lane']))
    if any(e['start_ms']<end*1000/SR or e['lane'] not in range(4) for e in following_events):raise ValueError('Following constraints must be valid future four-lane events')
    if (fixed_skeleton_events is not None or following_events or not fill_unused_quota) and selection_policy!='model_skeleton_then_audio':raise ValueError('Fixed skeleton requires skeleton policy')
    preceding_events = sorted(preceding_events or [], key=lambda e:(e['start_ms'],e['lane']))
    if preceding_events:
        recipe['preceding_constraints_hash'] = canonical_hash([
            {k:e.get(k) for k in ('id','start_ms','lane','end_ms')} for e in preceding_events])
    if selection_policy=='model_skeleton_then_audio':
        recipe['fill_unused_quota']=bool(fill_unused_quota)
        recipe['following_constraints_hash']=canonical_hash([{k:e.get(k) for k in ('id','start_ms','lane','end_ms')} for e in following_events])
        recipe['fixed_skeleton_constraints_hash']=canonical_hash([{k:e.get(k) for k in ('id','start_ms','lane','end_ms')} for e in fixed_skeleton_events]) if fixed_skeleton_events is not None else None
        recipe['stem_audio_original_audibility']=True
    recipe_hash = canonical_hash(recipe)
    candidates = []; silent_decisions = []
    for role, revision in (('vocals', vocals), ('accompaniment', accompaniment)):
        for event in revision['events']:
            item = copy.deepcopy(event)
            item['origins'] = [_origin(revision, event, role)]
            item['source_role'] = role
            item['salience'] = _supported(item)
            if item.get('audio_evidence', {}).get('audible') is False:
                silent_decisions.append({'type':'omitted','origins':item['origins'],
                    'reason':'声部在原曲相对能量门槛下没有可用音频证据；原谱保留',
                    'reason_category':'unsupported_audio_evidence','candidate_provenance':{'kind':'model'},
                    'candidate_support':{'mixture_relative_energy':copy.deepcopy(item.get('audio_evidence',{})),
                                         'energy_evidence_id':model_energy_evidence_id},'salience':item['salience']})
                continue
            candidates.append(item)
    # One cached original onset is one optional tap; two stem copies or an
    # existing model chord cannot multiply that sound into extra heads.
    from .adaptive_difficulty import evidence_candidates, shared_audio_peak
    from .charts import Note
    if acoustic is None and evidence and evidence.get('sources'):
        acoustic = evidence
    if acoustic is None:
        onsets = sorted({(sample, strength) for section in sections for sample, strength in section.get('onsets', [])})
        acoustic = {'id': plan_hash, 'source': {'sample_rate': SR},
                    'onset_samples': [sample for sample, strength in onsets],
                    'onset_strengths': [strength for sample, strength in onsets],
                    'profile': [row for section in sections for row in section.get('profile', [])]}
    def acoustic_head(event):
        return event.get('candidate_provenance',{}).get('kind')=='acoustic' or any(
            p.get('candidate_kind')=='acoustic' for p in event.get('origins',[]))
    prefix_models=[e for e in preceding_events if not acoustic_head(e) and start*1000/SR-15<=e['start_ms']<start*1000/SR]
    following_models=[e for e in following_events if not acoustic_head(e) and end*1000/SR<=e['start_ms']<=end*1000/SR+15]
    supplied = evidence_candidates([Note(e['start_ms'],e['lane'],e.get('end_ms')) for e in candidates+prefix_models+following_models],
                                   acoustic,timing,bounds_ms=[start*1000/SR,end*1000/SR],require_original_audibility=selection_policy=='model_skeleton_then_audio')
    model_support={c[0]:c for c in supplied if c[2]}
    for item in candidates:
        candidate=model_support[item['start_ms']]
        item['candidate_provenance']=copy.deepcopy(candidate.provenance)
        item['candidate_support']=copy.deepcopy(candidate.support)
        if item.get('audio_evidence'):
            item['candidate_support']['mixture_relative_energy']=copy.deepcopy(item['audio_evidence'])
            item['candidate_support']['energy_evidence_id']=model_energy_evidence_id
    for candidate in supplied:
        if candidate[2] and candidate[0]>=end*1000/SR and candidate.support.get('detections'):
            matches=[e for e in following_models if e['start_ms']==candidate[0]]
            silent_decisions.append({'type':'audio_support','reason':'nearby_onset_supports_following_model_head',
                'reason_category':'acoustic_model_support','following_event_ids':[e['id'] for e in matches],
                'following_origins':[copy.deepcopy(e.get('origins',[])) for e in matches],
                'candidate_support':copy.deepcopy(candidate.support),'candidate_provenance':copy.deepcopy(candidate.provenance)})
    for candidate in supplied:
        if candidate[2] and candidate[0]<start*1000/SR and candidate.support.get('detections'):
            matches=[e for e in prefix_models if e['start_ms']==candidate[0]]
            silent_decisions.append({'type':'audio_support','reason':'nearby_onset_supports_preceding_model_head',
                'reason_category':'acoustic_model_support','prefix_event_ids':[e['id'] for e in matches],
                'prefix_origins':[copy.deepcopy(e.get('origins',[])) for e in matches],
                'candidate_support':copy.deepcopy(candidate.support),'candidate_provenance':copy.deepcopy(candidate.provenance)})
    for candidate in supplied:
        if selection_policy=='model_only' or candidate[2] or not start <= candidate[0] * SR / 1000 < end:
            continue
        proof = candidate.support
        prefix_match=None
        for previous in reversed(preceding_events):
            if candidate[0]-previous['start_ms']>15:break
            acoustic_previous=previous.get('candidate_provenance',{}).get('kind')=='acoustic' or any(
                p.get('candidate_kind')=='acoustic' for p in previous.get('origins',[]))
            if not acoustic_previous:continue
            matched=shared_audio_peak(previous['start_ms'],previous.get('candidate_support',{}).get('detections',[]),
                                      candidate[0],proof.get('detections',[]))
            if matched:
                prefix_match=(previous,matched);break
        if prefix_match:
            previous,matched=prefix_match
            silent_decisions.append({'type':'omitted','start_ms':candidate[0],
                'reason':'shared_acoustic_onset_already_selected_in_prefix','reason_category':'duplicate_acoustic_evidence',
                'candidate_provenance':candidate.provenance,'candidate_support':copy.deepcopy(proof),
                'salience':min(float(audio_vote_cap) if audio_vote_cap is not None else 4.,candidate[1]),
                'prefix_event_id':previous['id'],'prefix_origins':copy.deepcopy(previous.get('origins',[])),
                'shared_onset_proof':copy.deepcopy(matched)})
            continue
        identity = canonical_hash({'acoustic': acoustic.get('id'), 'start_ms': candidate[0]})
        origins = [{'revision_id': acoustic.get('id', plan_hash), 'note_id': 'audio-' + identity[:32],
                    'source_id': vp['parent_source_id'], 'stem_role': 'original_acoustic',
                    'original_start_ms': candidate[0], 'original_end_ms': None, 'original_lane': None,
                    'candidate_kind': 'acoustic', 'support': proof}]
        candidates.append({'id': 'audio-' + identity[:32], 'start_ms': candidate[0], 'end_ms': None,
                           'lane': 0, 'source_role': 'original_acoustic', 'origins': origins,
                           'salience': min(float(audio_vote_cap) if audio_vote_cap is not None else 4., candidate[1]), 'candidate_provenance': candidate.provenance,
                           'candidate_support': proof})
    candidates.sort(key=lambda e: (e['start_ms'], e['source_role'], e['lane'], e['id']))
    candidates, decisions = _deduplicate(candidates)
    stage_counts = {'raw_model_heads':len(vocals['events'])+len(accompaniment['events']),
                    'unsupported_model_heads':sum(d.get('reason_category')=='unsupported_audio_evidence' for d in silent_decisions),
                    'cross_source_duplicate_heads':len(decisions),
                    'deduplicated_model_heads':sum(e['source_role']!='original_acoustic' for e in candidates),
                    'acoustic_new_heads':sum(e['source_role']=='original_acoustic' for e in candidates)}
    # A stronger representative may lie after its weaker copy or across a core
    # boundary. Apply ownership and rolling constraints to its actual timestamp.
    candidates.sort(key=lambda e: (e['start_ms'], e['source_role'], e['lane'], e['id']))
    decisions = silent_decisions + decisions
    if evidence is not None:
        from .chart_quality import classify_holds
        classified = classify_holds(candidates, evidence)
        candidates = classified['events']; decisions.extend(classified['decisions'])
    output = []; last = [-1e9] * 4; occupied = [-1e9] * 4; lane_counts = [0] * 4; reports = []
    for event in preceding_events:
        lane=event['lane']
        if lane not in range(4) or event['start_ms']>=start*1000/SR:
            raise ValueError('融合前序约束必须来自当前片段之前的有效四轨事件')
        last[lane]=max(last[lane],event['start_ms'])
        occupied[lane]=max(occupied[lane],event.get('end_ms') or event['start_ms'])
        lane_counts[lane]+=1
    covered = set()
    for section in sorted(sections, key=lambda x: (x.get('core') or x.get('range') or x.get('source_range'))[0]):
        core = section.get('core') or section.get('range') or section.get('source_range')
        a, b = max(start, core[0]), min(end, core[1])
        if a >= b:
            continue
        caps = _caps(section, key, settings)
        if selection_policy in ('model_skeleton_then_audio','model_only'):
            caps['section_caps']=[{**_caps(other,key,settings),'range_ms':[v*1000/SR for v in (other.get('core') or other.get('range') or other.get('source_range'))]} for other in sections]
        # Only the selected core consumes budget. Context is read-only.
        profile = section.get('profile', [])
        if profile and section.get('active_seconds', 0) > 0:
            selected_active = sum(max(0, min(b, row['end_sample']) - max(a, row['start_sample'])) / SR * row['active_fraction'] for row in profile)
            caps['target'] *= min(1., selected_active / section['active_seconds'])
        else:
            caps['target'] *= (b - a) / (core[1] - core[0])
        rows = [e for e in candidates if a <= e['start_ms'] * SR / 1000 < b]
        # Shared original evidence ranks audible skeleton and sustained model
        # intent; relative stem loudness is supporting evidence only.
        for item in rows:
            sample=item['start_ms']*SR/1000
            item['musical_priority']=0.
            if any(abs(sample-beat)<=SR*.035 for beat in section.get('beat_samples',[])):item['musical_priority']+=.5
            nearby=[strength for onset,strength in section.get('onsets',[]) if abs(sample-onset)<=SR*.065]
            if nearby:item['musical_priority']+=min(1.5,max(nearby))
        if any(e['id'] + e['source_role'] in covered for e in rows):
            raise ValueError('共享段落计划核心范围不能重叠')
        covered.update(e['id'] + e['source_role'] for e in rows)
        windows = _budget_windows(a, b, caps['target'], profile, section.get('phrases'))
        all_rows=rows
        if selection_policy=='model_skeleton_then_audio':
            from .adaptive_difficulty import model_skeleton_qualification
            eligible_times=set()
            for item in rows:
                if item['source_role']=='original_acoustic':continue
                proof=model_skeleton_qualification(item.get('candidate_support',{}),[item],evidence)
                item['candidate_support']['skeleton_eligibility']=proof
                if proof['multiscale_onset'] or proof['spectral_sustain']:eligible_times.add(item['start_ms'])
            rows=[e for e in rows if e['source_role']!='original_acoustic' and e['start_ms'] in eligible_times]
            for item in rows:item['candidate_support']['skeleton_eligibility']['whole_model_group_qualified']=True
            if fixed_skeleton_events is not None:rows=[]

        # Reserve density throughout the selected audio. A chronological beam
        # cannot recover early routes discarded before later audio is visited;
        # a section-wide count penalty would spend everything at the front.
        # Lane occupancy and rolling peak constraints still span every window.
        recent = [(e['start_ms'], e['lane']) for e in preceding_events + output if e['start_ms'] > a * 1000 / SR - 1000]
        states = [{'score': 0., 'events': [], 'last': last[:], 'occupied': occupied[:], 'counts': lane_counts[:], 'recent': recent, 'roles': Counter(), 'window_counts': Counter()}]
        model_groups = Counter(e['start_ms'] for e in rows if e['source_role'] != 'original_acoustic')
        group_last = {e['start_ms']: i for i,e in enumerate(rows)}
        group_bases = None; atomic_group = False
        for item_index,item in enumerate(rows):
            timestamp = item['start_ms']; original_tail = item.get('end_ms')
            group_size=model_groups[timestamp]
            if group_size>1 and (item_index==0 or rows[item_index-1]['start_ms']!=timestamp):
                group_bases=states[:]
                # When a whole real chord fits hard constraints, soft density
                # may retain it whole or omit it whole; it cannot erase one voice
                # merely to reserve the remaining quota for singleton peaks.
                atomic_group = group_size<=caps['chord'] and any(
                    len([(t,lane) for t,lane in st['recent'] if t>timestamp-1000])+group_size<=caps['peak']
                    and sum(timestamp-st['last'][lane]>=caps['gap']
                            and (timestamp>=st['occupied'][lane]+caps['release'] or selection_policy in ('model_skeleton_then_audio','model_only') and st['occupied'][lane]<=st['last'][lane]) for lane in range(4))>=group_size
                    for st in states)
            window_index = next((i for i,w in enumerate(windows) if w['source_range'][0]<=timestamp*SR/1000<w['source_range'][1]),len(windows)-1)
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
                prev = state['events'][-1]['lane'] if state['events'] else (output[-1]['lane'] if output else (preceding_events[-1]['lane'] if preceding_events else None))
                lanes=range(4)
                if selection_policy in ('model_skeleton_then_audio','model_only'):
                    lanes,_=_insertion_lanes(item,preceding_events+output+state['events']+following_events,caps)
                    if item['lane'] in lanes:lanes=[item['lane']]
                for lane in lanes:
                    if selection_policy not in ('model_skeleton_then_audio','model_only') and (timestamp-state['last'][lane]<caps['gap'] or timestamp<state['occupied'][lane]+caps['release']):
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
                    preference = _lane_preference(item,lane)
                    coverage = .2 / (1 + state['roles'][item['source_role']])
                    score = state['score'] + item['salience'] + item.get('musical_priority',0) + preference + coverage - repeat_penalty - hand_penalty - excess_penalty
                    event = {k: copy.deepcopy(item[k]) for k in ('start_ms', 'origins')}
                    identity = {'recipe': recipe_hash, 'origins': event['origins']}
                    event.update(id='fusion-' + canonical_hash(identity)[:32], lane=lane, end_ms=tail)
                    event['source_id'] = item['origins'][0]['source_id']
                    event['candidate_provenance'] = copy.deepcopy(item.get('candidate_provenance', {'kind': 'model'}))
                    event['candidate_support'] = copy.deepcopy(item.get('candidate_support', {}))
                    if item.get('model_rhythm') is not None:
                        event['model_rhythm'] = copy.deepcopy(item['model_rhythm'])
                    if original_tail is not None and tail is not None and tail < original_tail:
                        event['tail_policy'] = {'kind': 'rule_cap', 'model_end_ms': original_tail,
                                                'cap_ms': caps['hold'], 'uncapped_start_ms': timestamp}
                    if item.get('model_group_id') is not None: event['model_group_id']=item['model_group_id']
                    event['fusion_original_id'] = (item['source_role'], item['id'])
                    child = {'score': score, 'events': state['events'] + [event], 'last': state['last'][:],
                             'occupied': state['occupied'][:], 'counts': state['counts'][:], 'recent': active + [(timestamp, lane)], 'roles': state['roles'].copy(),
                             'window_counts': state['window_counts'].copy()}
                    child['last'][lane] = timestamp; child['occupied'][lane] = tail or timestamp; child['counts'][lane] += 1
                    child['roles'][item['source_role']] += 1
                    child['window_counts'][window_index] += 1
                    options.append(child)
            if group_size>1 and item_index==group_last[timestamp] and atomic_group:
                options=[st for st in options if sum(e['start_ms']==timestamp for e in st['events']) in (0,group_size)]
                options.extend(group_bases)
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
        if selection_policy=='model_skeleton_then_audio':
            if fixed_skeleton_events is not None:
                lookup={(o.get('revision_id'),o.get('note_id')):item for item in all_rows for o in item['origins']}
                for source_event in fixed_skeleton_events:
                    if not a<=source_event['start_ms']*SR/1000<b:continue
                    item=next((lookup.get((o.get('revision_id'),o.get('note_id'))) for o in source_event.get('origins',[]) if lookup.get((o.get('revision_id'),o.get('note_id')))),None)
                    if item is None or item['source_role']=='original_acoustic' or source_event['start_ms']!=item['start_ms']:raise ValueError('Fixed skeleton is not an actual model candidate')
                    expected_tail=item.get('end_ms')
                    if expected_tail is not None:
                        expected_tail=min(expected_tail,item['start_ms']+caps['hold'])
                        if expected_tail-item['start_ms']<100:expected_tail=None
                    if source_event.get('end_ms')!=expected_tail or source_event.get('origins')!=item['origins']:raise ValueError('Fixed skeleton changes actual model voice or tail')
                    event=copy.deepcopy(source_event);event['fusion_original_id']=(item['source_role'],item['id'])
                    wi=next((i for i,w in enumerate(windows) if w['source_range'][0]<=event['start_ms']*SR/1000<w['source_range'][1]),len(windows)-1)
                    if winner['window_counts'][wi]>=windows[wi]['max_heads']:raise ValueError('Fixed skeleton exceeds frozen phrase quota')
                    legal,reasons=_insertion_lanes(event,preceding_events+output+winner['events']+following_events,caps)
                    if event['lane'] not in legal:raise ValueError('Fixed skeleton violates hard constraints: '+repr(reasons))
                    winner['events'].append(event);winner['window_counts'][wi]+=1
                    lane=event['lane'];winner['counts'][lane]+=1;winner['roles'][item['source_role']]+=1
                    winner['last'][lane]=max(winner['last'][lane],event['start_ms'])
                    winner['occupied'][lane]=max(winner['occupied'][lane],event.get('end_ms') or event['start_ms'])
            skeleton_count=len(winner['events'])
            for event in winner['events']:
                event['candidate_support']['selection_stage']='qualified_model_skeleton'
                event['candidate_support'].setdefault('skeleton_recipe_hash',recipe_hash)
            selected={e['fusion_original_id'] for e in winner['events']};groups={}
            for item in all_rows:
                if (item['source_role'],item['id']) in selected:continue
                identity=('audio',item['id']) if item['source_role']=='original_acoustic' else ('model',item['start_ms'])
                groups.setdefault(identity,[]).append(item)
            supplements=sorted(groups.values(),key=lambda g:(-sum(e['salience']+e.get('musical_priority',0) for e in g)/len(g),g[0]['start_ms']))
            # Frozen model heads/tails are checked in both time directions;
            # stages share exactly the same original phrase quota.
            for group in supplements if fill_unused_quota else []:
                timestamp=group[0]['start_ms']
                wi=next((i for i,w in enumerate(windows) if w['source_range'][0]<=timestamp*SR/1000<w['source_range'][1]),len(windows)-1)
                if winner['window_counts'][wi]+len(group)>windows[wi]['max_heads']:continue
                trial=[]
                for item in group:
                    lanes,_=_insertion_lanes(item,preceding_events+output+winner['events']+trial+following_events,caps)
                    if not lanes:break
                    lane=min(lanes,key=lambda l:(-_lane_preference(item,l),winner['counts'][l]+sum(e['lane']==l for e in trial),l))
                    tail=item.get('end_ms')
                    if tail is not None:
                        tail=min(tail,timestamp+caps['hold'])
                        if tail-timestamp<100:tail=None
                    event={k:copy.deepcopy(item[k]) for k in ('start_ms','origins')}
                    event.update(id='fusion-'+canonical_hash({'recipe':recipe_hash,'origins':event['origins']})[:32],lane=lane,end_ms=tail)
                    event['source_id']=item['origins'][0]['source_id']
                    event['candidate_provenance']=copy.deepcopy(item.get('candidate_provenance',{'kind':'model'}))
                    event['candidate_support']=copy.deepcopy(item.get('candidate_support',{}))
                    event['candidate_support']['selection_stage']='same_phrase_unused_quota_fill'
                    event['fusion_original_id']=(item['source_role'],item['id']);trial.append(event)
                if len(trial)!=len(group):continue
                winner['events'].extend(trial);winner['window_counts'][wi]+=len(trial)
                for item,event in zip(group,trial):
                    lane=event['lane'];winner['counts'][lane]+=1
                    winner['last'][lane]=max(winner['last'][lane],timestamp)
                    winner['occupied'][lane]=max(winner['occupied'][lane],event.get('end_ms') or timestamp)
                    winner['roles'][item['source_role']]+=1
            winner['events'].sort(key=lambda e:(e['start_ms'],e['lane']))
            rows=all_rows;model_groups=Counter(e['start_ms'] for e in rows if e['source_role']!='original_acoustic')
        chosen = {e['fusion_original_id']: e for e in winner['events']}
        for item in rows:
            event = chosen.get((item['source_role'], item['id']))
            if event is None:
                category,conflicts=_omission_category(item,preceding_events+output+winner['events']+following_events,caps)
                decisions.append({'type':'omitted','origins':item['origins'],'reason_category':category,
                    'candidate_provenance':copy.deepcopy(item.get('candidate_provenance',{'kind':'model'})),
                    'candidate_support':copy.deepcopy(item.get('candidate_support',{})),
                    'salience':item['salience'],'musical_priority':item.get('musical_priority',0),
                    'hard_conflicts':conflicts,'reason':'final_route_has_no_legal_insertion' if category=='hard_constraint'
                    else 'shared_phrase_budget_or_ranked_selection'})
            elif event['lane'] != item['lane'] or event['end_ms'] != item.get('end_ms'):
                decisions.append({'type': 'replanned', 'origins': item['origins'], 'lane': event['lane'], 'end_ms': event['end_ms'],
                                  'candidate_provenance':copy.deepcopy(item.get('candidate_provenance',{'kind':'model'})),
                                  'candidate_support':copy.deepcopy(item.get('candidate_support',{})),
                                  'salience':item['salience'],'musical_priority':item.get('musical_priority',0),
                                  'reason': '四轨共享排键或长条上限；原谱保留'})
        output.extend(winner['events']); last = winner['last']; occupied = winner['occupied']; lane_counts = winner['counts']
        for i, window in enumerate(windows):
            wa, wb = window['source_range']
            window['candidate_heads'] = sum(wa <= e['start_ms'] * SR / 1000 < wb for e in rows)
            window['selected_heads'] = winner['window_counts'][i]
        reports.append({'section_id': section.get('id'), 'source_range': [a, b], 'target_heads': caps['target'],
                        'candidate_heads': len(rows), 'candidate_model_heads': sum(e['source_role'] != 'original_acoustic' for e in rows),
                        'candidate_acoustic_heads': sum(e['source_role'] == 'original_acoustic' for e in rows),
                        'selected_heads': len(winner['events']),
                        'fixed_qualified_skeleton_heads':skeleton_count if selection_policy=='model_skeleton_then_audio' else None,
                        'selected_model_heads': sum(e.get('candidate_provenance',{}).get('kind')!='acoustic' for e in winner['events']),
                        'selected_acoustic_heads': sum(e.get('candidate_provenance',{}).get('kind')=='acoustic' for e in winner['events']),
                        'candidate_model_chords':sum(count>1 for count in model_groups.values()),
                        'selected_complete_model_chords':sum(count>1 and sum(e['start_ms']==timestamp for e in winner['events'])==count for timestamp,count in model_groups.items()),
                        'selected_partial_model_chords':sum(count>1 and 0<sum(e['start_ms']==timestamp for e in winner['events'])<count for timestamp,count in model_groups.items()),
                        'hard_caps': {name:value for name,value in caps.items() if name!='section_caps'},
                        'unconfirmed_cross_source_overlaps': _unconfirmed_overlaps(rows),
                        'budget_windows': windows,
                        'source_heads': dict(winner['roles']), 'saturated': len(winner['events']) < caps['target']})
    if len(covered) < len(candidates):
        raise ValueError('共享段落难度计划没有覆盖全部融合候选')
    output.sort(key=lambda e: (e['start_ms'], e['lane']))
    for event in output: event.pop('fusion_original_id', None)
    max_tail = max([end * 1000 / SR] + [e.get('end_ms') or e['start_ms'] for e in output])
    valid_events(output, start * 1000 / SR, end * 1000 / SR, max_tail)
    stage_counts.update(selected_model_heads=sum(e.get('candidate_provenance',{}).get('kind')!='acoustic' for e in output),
                        selected_acoustic_heads=sum(e.get('candidate_provenance',{}).get('kind')=='acoustic' for e in output),
                        hard_constraint_omissions=sum(d.get('reason_category')=='hard_constraint' for d in decisions),
                        soft_selection_omissions=sum(d.get('reason_category')=='soft_selection' for d in decisions))
    return {'events': output, 'decisions': decisions, 'stats': reports,
            'provenance': {**recipe, 'recipe_hash': recipe_hash, 'fusion_version': VERSION,
                           'source_role': 'fusion', 'source_id': 'fusion:' + recipe_hash,
                           'stem_set_id': vp['stem_set_id'], 'parent_source_id': vp['parent_source_id'],
                           'applied_plan_hash': plan_hash, 'global_budget_applied': True, 'budget_stage':1, 'timing_map':vp.get('timing_map'),
                           'overlap_policy': 'shared_acoustic_identity_only; strongest_complete_event; no_synthetic_heads',
                           'budget_policy': 'frozen_phrases_or_2s_active_audio_windows; largest_remainder_shared_allowance; no_future_budget_borrowing',
                           'head_stage_counts': stage_counts, 'section_stats': reports, 'decisions': decisions}}
