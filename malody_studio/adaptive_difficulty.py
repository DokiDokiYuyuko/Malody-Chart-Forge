"""Evidence-preserving attack selection and logged desktop four-lane constraints."""
from collections import defaultdict, deque
from bisect import bisect_left
import hashlib
import math
import numpy as np
from scipy.signal import find_peaks
from scipy.ndimage import maximum_filter1d
from .charts import Note
from .difficulty import PRESETS

VERSION = 'phrase-selection-v2'


class Candidate(tuple):
    """Legacy three-item attack tuple with inspectable evidence provenance."""
    def __new__(cls, timestamp, strength, voices, provenance=None, support=None):
        value = super().__new__(cls, (timestamp, strength, voices))
        value.provenance = provenance or {'kind': 'model' if voices else 'acoustic'}
        value.support = support or {}
        return value

    def __getnewargs__(self):
        return (*self, self.provenance, self.support)


def shared_audio_peak(first_time, first_detections, second_time, second_detections):
    """Proof for a detector copy, never proximity-only model/stagger deletion."""
    distance=abs(first_time-second_time)
    if distance>15:return None
    detector=lambda p:(p.get('cache_id'),p.get('source_role'),p.get('channel'),p.get('kind'))
    for previous in first_detections:
        for proof in second_detections:
            proof_distance=abs(float(previous['time_ms'])-float(proof['time_ms'])) if previous.get('time_ms') is not None and proof.get('time_ms') is not None else distance
            if proof_distance>15:continue
            if detector(previous)==detector(proof):
                if proof_distance<=1:return {'first':previous,'second':proof,'reason':'same_detector_structural_copy'}
                continue
            uncertainty=float(previous.get('uncertainty_ms') or 7.5)+float(proof.get('uncertainty_ms') or 7.5)
            if proof_distance<=min(15.,uncertainty):
                return {'first':previous,'second':proof,'reason':'cross_detector_overlapping_onset_uncertainty'}
    return None


def original_audibility_proof(evidence, timestamp):
    """Read the nearest frozen original frame independently in each channel."""
    readings=[]
    for channel_index,channel in enumerate((evidence or {}).get('sources',{}).get('original',{}).get('channels',[])):
        times=channel.get('frame_ms',[]);energy=channel.get('energy',[]);floor=channel.get('audible_floor')
        if not times or not energy or floor is None:continue
        i=bisect_left(times,timestamp)
        choices=[j for j in (i-1,i) if 0<=j<min(len(times),len(energy))]
        if not choices:continue
        j=min(choices,key=lambda j:abs(times[j]-timestamp))
        value=float(energy[j]);threshold=float(floor)
        readings.append({'channel':channel_index,'frame_ms':times[j],'energy':value,'audible_floor':threshold,
            'audible':math.isfinite(value) and value>=threshold})
    return {'cache_id':(evidence or {}).get('id',(evidence or {}).get('cache_key')),
        'verified':bool(readings),'audible':any(r['audible'] for r in readings),'channels':readings,
        'method':'frozen_original_nearest_frame_per_channel'}


def evidence_candidates(notes, acoustic=None, timing=None, origin_ms=0, extra_acoustic=None, bounds_ms=None, require_original_audibility=False):
    """One real audio head per measured onset, plus untouched actual model voices.

    Frozen onset caches are consumed directly: pulse/energy alone never creates
    a head. A nearby audio detection annotates a model attack, never doubles it.
    Model-model proximity is deliberately irrelevant to stagger or chord intent.
    """
    groups=defaultdict(list)
    for note in notes:groups[note.start].append(note)
    acoustic=acoustic or {};timing=timing or {}
    sr=timing.get('source',{}).get('sample_rate',acoustic.get('source',{}).get('sample_rate',44100))
    beats=np.asarray(timing.get('beat_samples',[]),dtype=float)*1000/sr-origin_ms
    down=np.asarray(timing.get('downbeat_samples',[]),dtype=float)*1000/sr-origin_ms
    detections=[]
    for cache in [acoustic]+list(extra_acoustic or []):
        rate=cache.get('source',{}).get('sample_rate',cache.get('sample_rate',sr))
        for role,source in cache.get('sources',{}).items():
            for channel_index,channel in enumerate(source.get('channels',[])):
                for onset in channel.get('onsets',[]):
                    strength=float(onset.get('strength',0));timestamp=float(onset['time_ms'])-origin_ms
                    if not math.isfinite(strength) or strength<.6 or timestamp<0:continue
                    if bounds_ms and not bounds_ms[0]<=timestamp<bounds_ms[1]:continue
                    if not onset.get('multiscale_agreement') and onset.get('scale_support',0)<2:continue
                    proof={'cache_id':cache.get('id',cache.get('cache_key')), 'time_ms':onset['time_ms'],
                           'channel':channel_index,'source_role':role,'kind':'multiscale_positive_spectral_flux',
                           'strength':strength,'uncertainty_ms':onset.get('uncertainty_ms'),
                           'scale_support':onset.get('scale_support'),'multiscale_agreement':bool(onset.get('multiscale_agreement'))}
                    detections.append((timestamp,strength,proof))
        for sample,strength in zip(cache.get('onset_samples',[]),cache.get('onset_strengths',[])):
            if not math.isfinite(float(strength)) or float(strength)<.3:continue
            profile=next((p for p in cache.get('profile',[]) if p['start_sample']<=sample<p['end_sample']),None)
            if profile and (profile.get('exact_silence') or profile.get('active_fraction',1)<=0):continue
            timestamp=float(sample)*1000/rate-origin_ms
            if timestamp<0:continue
            if bounds_ms and not bounds_ms[0]<=timestamp<bounds_ms[1]:continue
            detections.append((timestamp,float(strength),{'cache_id':cache.get('id',cache.get('cache_key')),
                'sample':sample,'time_ms':float(sample)*1000/rate,'source_role':cache.get('source_role','original'), 'kind':'positive_spectral_flux',
                'strength':float(strength),'uncertainty_ms':7.5}))
    model_times=np.asarray(sorted(groups),dtype=float)
    support=defaultdict(list);audio=[];audio_times=[]
    # Strongest cross-cache detection represents the same measured sound.
    for timestamp,strength,proof in sorted(detections,key=lambda c:(-c[1],c[0])):
        if len(model_times):
            i=int(np.argmin(abs(model_times-timestamp)))
            if abs(model_times[i]-timestamp)<=15:
                support[float(model_times[i])].append(proof);continue
        if require_original_audibility and proof.get('source_role')!='original':
            audibility=original_audibility_proof(acoustic,timestamp+origin_ms)
            if not audibility['verified'] or not audibility['audible']:continue
            proof={**proof,'original_audibility':audibility}
        location=bisect_left(audio_times,timestamp)
        lo=bisect_left(audio_times,timestamp-15)
        hi=bisect_left(audio_times,timestamp+15.000001)
        existing=next((audio[i] for i in range(lo,hi) if shared_audio_peak(audio[i][0],audio[i].support['detections'],timestamp,[proof])),None)
        if existing:
            existing.support['detections'].append(proof);continue
        audio_times.insert(location,timestamp)
        audio.insert(location,Candidate(timestamp,strength,[],{'kind':'acoustic','reason':'independent_audible_onset'},
                               {'detections':[proof]}))
    model=[]
    for timestamp,voices in sorted(groups.items()):
        score=.25
        nearby=[strength for onset,strength,_ in detections if abs(onset-timestamp)<=65]
        if nearby:score+=min(3.,max(nearby))
        if len(beats) and np.min(abs(beats-timestamp))<=35:score+=.8
        if len(down) and np.min(abs(down-timestamp))<=35:score+=.8
        model.append(Candidate(timestamp,score,voices,{'kind':'model','voices':len(voices)},
                               {'detections':support[timestamp]}))
    return sorted(audio+model,key=lambda c:c[0])


def model_candidates(notes, acoustic=None, timing=None, origin_ms=0, selection_policy='model_only'):
    candidates=evidence_candidates(notes,acoustic,timing,origin_ms)
    # Audio can rank or validate existing model attacks; the current contract
    # never treats an unused detector onset as new model supply. Explicit old
    # policies remain readable for immutable historical jobs.
    return [candidate for candidate in candidates if candidate[2]] if selection_policy=='model_only' else candidates


def attacks_adaptive(y, sr, master):
    import librosa
    y = np.asarray(y, dtype=np.float32)
    if y.ndim not in (1,2) or not len(y) or not np.isfinite(y).all():
        raise ValueError('起音分析需要有效音频声道')
    if np.max(np.abs(y)) < 1e-5:
        return []
    hop = max(1, round(sr * .005))
    channels = y[None,:] if y.ndim == 1 else y.T
    spectra = np.abs(librosa.stft(channels, n_fft=512, hop_length=hop))
    magnitude = np.sqrt(np.mean(spectra ** 2, axis=0))
    frequencies = librosa.fft_frequencies(sr=sr, n_fft=512)
    envelopes = []
    for lower, upper in ((40, 250), (250, 2000), (2000, sr / 2 + 1)):
        band = np.log1p(spectra[:,(frequencies >= lower) & (frequencies < upper)] * 10)
        flux = np.maximum(np.diff(band, axis=-1, prepend=band[..., :1]), 0).mean(axis=(0,1))
        envelopes.append(flux / max(float(np.percentile(flux, 95)), 1e-6))
    envelope = np.maximum.reduce(envelopes)
    rms = librosa.feature.rms(S=magnitude, frame_length=512, hop_length=hop)[0]
    nearby = maximum_filter1d(rms, size=5)
    audible = max(float(np.max(rms)) * .008, 1e-5)
    peaks, _ = find_peaks(envelope, distance=max(1, round(.02 * sr / hop)), prominence=.2, height=.3)
    groups = defaultdict(list)
    previous = [-math.inf] * 4
    # Keep distinct model attacks, even 31ms apart. The only merge is a same-lane
    # structural duplicate <=1ms; exact source timestamps remain unchanged.
    for note in sorted(master, key=lambda n: (n.start, n.lane)):
        if note.lane not in range(4) or not math.isfinite(note.start) or note.start < 0:
            continue
        frame = min(len(rms)-1, max(0, round(note.start * sr / (1000 * hop))))
        if nearby[frame] <= audible or note.start - previous[note.lane] <= 1:
            continue
        groups[float(note.start)].append(note)
        previous[note.lane] = note.start
    model_times = np.array(sorted(groups))
    audio = []
    for frame in peaks:
        if nearby[frame] <= audible:
            continue
        timestamp = float(frame * hop * 1000 / sr)
        # Audio peaks near an existing model head are evidence, not a second head.
        if len(model_times):
            index = np.searchsorted(model_times, timestamp)
            if any(abs(timestamp-model_times[i]) <= 15 for i in (index-1, index) if 0 <= i < len(model_times)):
                continue
        audio.append(Candidate(timestamp, float(envelope[frame]), [], {'kind':'acoustic','reason':'independent_audible_onset'}, {'analysis':'channel_band_positive_spectral_flux'}))
    model = []
    for timestamp, notes in groups.items():
        frame = min(len(envelope)-1, max(0, round(timestamp * sr / (1000*hop))))
        model.append(Candidate(timestamp, float(envelope[frame]) + .7, notes, {'kind':'model','voices':len(notes)}))
    return sorted(audio + model, key=lambda item: item[0])


def _lanes(original, pattern, counts, last, previous):
    preferred = original.lane if original else None
    def cost(lane):
        preference = -1.5 if lane == preferred else 0
        hand = .35 if previous is not None and lane//2 == previous//2 else 0
        repeat = .25 if lane == previous else 0
        if pattern == 'jackspeed':
            repeat = -.9 if lane == previous else 0
            hand = 0
        elif pattern == 'chordjack':
            hand *= .2
        elif pattern == 'speed':
            hand *= 2
        return preference + repeat + hand + counts[lane]*.005, last[lane], lane
    return sorted(range(4), key=cost)


def repair_playable(raw, duration_ms, rule, pattern='balanced', min_notes=0):
    """Hard caps only: raw is untouched; no target NPS-based deletion."""
    result, decisions = [], []
    last, occupied, counts = [-math.inf]*4, [-math.inf]*4, [0]*4
    recent = deque()
    chord = defaultdict(int)
    previous = None
    seen = [-math.inf]*4
    for note in sorted(raw, key=lambda n: (n.start, n.lane)):
        if note.lane not in range(4) or not math.isfinite(note.start) or not 0 <= note.start < duration_ms:
            decisions.append({'type':'invalid', 'start_ms':note.start, 'lane':note.lane});continue
        if note.start-seen[note.lane] <= 1:
            decisions.append({'type':'structural_duplicate', 'start_ms':note.start, 'lane':note.lane});continue
        seen[note.lane] = note.start
        while recent and recent[0] <= note.start - 1000:
            recent.popleft()
        if len(recent) >= rule['peak'] or chord[round(note.start)] >= rule['chord']:
            decisions.append({'type':'omitted', 'start_ms':note.start, 'lane':note.lane, 'reason':'user_peak_or_chord_cap'});continue
        available = [lane for lane in _lanes(note,pattern,counts,last,previous)
                     if note.start-last[lane] >= rule['gap'] and note.start >= occupied[lane]+35]
        if not available:
            decisions.append({'type':'omitted', 'start_ms':note.start, 'lane':note.lane, 'reason':'user_gap_or_hold_occupancy'});continue
        # A valid model lane is part of its chord/hold intent. Soft balancing
        # may choose a replacement only after a hard conflict blocks that lane.
        lane = note.lane if note.lane in available else available[0]
        end = note.end
        if end is not None:
            if not math.isfinite(end) or end <= note.start:
                end = None
            else:
                end = min(end, note.start+rule['hold_ms'],duration_ms)
                if end-note.start < 100:
                    end = None
        result.append(Note(note.start,lane,end))
        if lane != note.lane or end != note.end:
            decisions.append({'type':'replanned','start_ms':note.start,'original_lane':note.lane,'lane':lane,
                              'original_end_ms':note.end,'end_ms':end,'reason':'user_hard_caps'})
        last[lane], occupied[lane] = note.start,end if end is not None else note.start
        counts[lane]+=1;previous=lane;recent.append(note.start);chord[round(note.start)]+=1
    if len(result)<min_notes:
        raise ValueError('有效音符太少，请检查音频或调整难度后重试')
    return result, {'decisions':decisions,'raw_heads':len(raw),'selected_heads':len(result),
                    'hard_caps_only':True,'global_budget_applied':False,'hand_policy':'soft_cost_no_hard_cap'}


def _plan_sections(plan, duration_ms, origin_ms, key, preset):
    if not plan:
        return [{'id':'whole','a':0.,'b':duration_ms,'target':preset['rate']*duration_ms/1000,'profile':[]}]
    sections=[]
    for section in plan['sections']:
        a,b=(sample*1000/plan['sample_rate']-origin_ms for sample in section['core'])
        left,right=max(0.,a),min(duration_ms,b)
        if left>=right:continue
        detail=section['per_difficulty'][key]
        # Integrate only the owned slice, not context or neighboring cores.
        total=0.;owned=0.
        for p in section.get('profile',[]):
            pa,pb=p['start_sample']*1000/plan['sample_rate']-origin_ms,p['end_sample']*1000/plan['sample_rate']-origin_ms
            weight=max(0.,min(b,pb)-max(a,pa))*p['active_fraction']
            total+=weight
            owned+=max(0.,min(right,pb)-max(left,pa))*p['active_fraction']
        fraction=owned/total if total else 0.
        sections.append({'id':section['id'],'a':left,'b':right,'target':detail['target_heads_soft']*fraction,
                         'hard_caps':detail.get('hard_caps',{}),'profile':section.get('profile',[]),'sr':plan['sample_rate'],'origin':origin_ms,
                         'phrases':[[max(left,p['range'][0]*1000/plan['sample_rate']-origin_ms),min(right,p['range'][1]*1000/plan['sample_rate']-origin_ms)] for p in section.get('phrases',[]) if p['range'][0]*1000/plan['sample_rate']-origin_ms<right and p['range'][1]*1000/plan['sample_rate']-origin_ms>left]})
    return sections


def model_skeleton_qualification(support, events, evidence=None):
    """Qualification uses frozen multiscale attack or matched spectral sustain."""
    from .chart_quality import _proof
    onset=any(d.get('kind')=='multiscale_positive_spectral_flux' and d.get('strength',0)>=.6
              and (d.get('multiscale_agreement') or (d.get('scale_support') or 0)>=2)
              for d in support.get('detections',[]))
    sustained=[]
    for event in events:
        if event.get('end_ms') is None:continue
        channels=[ch for role,source in (evidence or {}).get('sources',{}).items() if role in ('original',event.get('source_role')) for ch in source.get('channels',[])]
        if any(any(key not in ch for key in ('frame_ms','energy','spectral_continuity','audible_floor')) for ch in channels):continue
        proof=_proof(event,evidence or {})
        value=proof.get('sustain_support')
        if value is not None and float(value)-.2*int(proof.get('rearticulations',0))>=.75:
            sustained.append(proof)
    return {'multiscale_onset':onset,'spectral_sustain':bool(sustained),'sustain_proof':sustained or None}


def _fixed_phrase_selection(events, selected, desired, preset, duration_ms, origin_ms, evidence, audio_vote_cap, stage="both"):
    from .fusion import _insertion_lanes
    caps={'peak':preset['peak'],'chord':preset['chord'],'gap':preset['gap'],'release':preset.get('release_gap_ms',35.),'hold':preset['hold_ms'],'section_caps':preset.get('section_caps',[])}
    fixed=[{'start_ms':a,'lane':l,'end_ms':b} for a,l,b in dict.fromkeys((n.start,n.lane,n.end) for n in selected)]
    groups=[];decisions=[]
    for candidate in events:
        originals=candidate[2]
        support=dict(getattr(candidate,'support',{}));detections=support.get('detections',[])
        if not originals and detections and not any(d.get('source_role')=='original' for d in detections):
            audibility=original_audibility_proof(evidence,candidate[0]+origin_ms)
            support['original_audibility']=audibility
            if not audibility['verified'] or not audibility['audible']:
                decisions.append({'type':'omitted','start_ms':candidate[0],'reason':'stem_onset_original_audibility_unverified_or_below_frozen_floor','reason_category':'unsupported_audio_evidence','candidate_support':{'detections':detections,'original_audibility':audibility}})
                continue
        absolute=[{'start_ms':n.start+origin_ms,'lane':n.lane,'end_ms':n.end+origin_ms if n.end is not None else None} for n in originals]
        proof=model_skeleton_qualification(support,absolute,evidence) if originals else {}
        qualified=bool(originals and (proof['multiscale_onset'] or proof['spectral_sustain']))
        if qualified:proof['whole_model_group_qualified']=True
        if stage=='skeleton' and not qualified:continue
        groups.append((candidate,qualified,proof,support))
    bounded=float(audio_vote_cap) if audio_vote_cap is not None else 4.
    ranked=sorted(groups,key=lambda g:(not g[1],-min(4. if g[0][2] else bounded,g[0][1]),g[0][0]))
    voices=[];model_fixed=0
    for candidate,qualified,proof,support in ranked:
        originals=candidate[2];timestamp=candidate[0]
        notes=originals or [Note(timestamp,0,None)]
        # Real model chords are whole units for the soft quota. Oversized
        # groups cannot fit the user's hard chord cap and are explicitly omitted.
        category=None;conflicts=[];trial=[]
        if len(notes)>preset['chord']:category='hard_constraint';conflicts=['chord_cap']
        elif len(voices)+len(notes)>desired:category='soft_selection'
        else:
            for note in notes:
                tail=min(note.end,note.start+preset['hold_ms'],duration_ms) if note.end is not None else None
                if tail is not None and tail-note.start<100:tail=None
                item={'start_ms':note.start,'end_ms':tail}
                lanes,reasons=_insertion_lanes(item,fixed+trial,caps)
                if not lanes:category='hard_constraint';conflicts=reasons;break
                # Preserve a legal model lane; acoustic storage lane0 is neutral.
                lane=note.lane if originals and note.lane in lanes else min(lanes,key=lambda l:(sum(e['lane']==l for e in fixed+trial),l))
                trial.append({'start_ms':note.start,'lane':lane,'end_ms':tail})
        selection_stage='qualified_model_skeleton' if qualified and stage!='fill' else 'same_phrase_unused_quota_fill'
        metadata={**support,'skeleton_eligibility':proof,'selection_stage':selection_stage}
        provenance=getattr(candidate,'provenance',{'kind':'model' if originals else 'acoustic'})
        if category:
            decisions.append({'type':'omitted','start_ms':timestamp,'heads':len(notes),'reason_category':category,
                'hard_conflicts':conflicts,'reason':'final_route_has_no_legal_insertion' if category=='hard_constraint' else 'frozen_phrase_budget_whole_candidate',
                'candidate_provenance':provenance,'candidate_support':metadata})
            continue
        fixed.extend(trial)
        for event in trial:
            voices.append(Note(event['start_ms'],event['lane'],event['end_ms']))
            decisions.append({'type':'selected','start_ms':event['start_ms'],'lane':event['lane'],
               'reason':'fixed_skeleton_then_same_phrase_unused_quota','candidate_provenance':provenance,'candidate_support':metadata})
        if qualified and stage!='fill':model_fixed+=len(trial)
    return sorted(voices,key=lambda n:(n.start,n.lane)),decisions,model_fixed


def calibrate_adaptive(candidates, duration_ms, key, ln_ratio, seed, overrides=None,
                       pattern='balanced', plan=None, min_notes=0, origin_ms=0, evidence=None, timing=None,
                       selection_policy='ranked_beam', audio_vote_cap=None):
    if selection_policy not in ('ranked_beam','model_skeleton_then_audio','model_only'):raise ValueError('Unknown adaptive selection policy')
    if audio_vote_cap is not None and (not math.isfinite(float(audio_vote_cap)) or not 0<=float(audio_vote_cap)<=4):raise ValueError('Invalid acoustic vote cap')
    if selection_policy=='model_only':candidates=[candidate for candidate in candidates if candidate[2]]
    if selection_policy=='ranked_beam' and plan is not None and not plan.get('dynamic_enabled',False) and not plan.get('arrangement_enabled'):
        from .difficulty import calibrate
        return calibrate(candidates,duration_ms,key,ln_ratio,seed,overrides,pattern,min_notes=min_notes)
    preset={**PRESETS[key],**(overrides or {})}
    sections=_plan_sections(plan,duration_ms,origin_ms,key,preset)
    rng=np.random.default_rng(seed)
    selected=[];reports=[];decisions=[]
    valid=[c for c in candidates if math.isfinite(c[0]) and 0<=c[0]<duration_ms]
    base_preset=preset
    section_caps=[{'range_ms':[s['a'],s['b']],'peak':s.get('hard_caps',{}).get('peak_1s',preset['peak']),
        'gap':s.get('hard_caps',{}).get('min_lane_gap_ms',preset['gap']),
        'release':s.get('hard_caps',{}).get('release_gap_ms',35.)} for s in sections]
    global_bones=[];global_bone_times=set();frozen_bone_map={}
    for selection_pass in ('skeleton','fill') if selection_policy=='model_skeleton_then_audio' else ('legacy',):
        for section in sections:
            preset=base_preset
            if selection_policy in ('model_skeleton_then_audio','model_only'):
                hard=section.get('hard_caps',{})
                preset={**base_preset,'peak':hard.get('peak_1s',base_preset['peak']),'chord':hard.get('chord',base_preset['chord']),
                    'gap':hard.get('min_lane_gap_ms',base_preset['gap']),'hold_ms':hard.get('hold_max_ms',base_preset['hold_ms']),
                    'release_gap_ms':hard.get('release_gap_ms',35.),'section_caps':section_caps}
            rows=[c for c in valid if section['a']<=c[0]<section['b']]
            # Quantities follow evidence phrases (up to two seconds), never a fixed
            # every-four-second equal quota. Profile rests have zero target weight.
            frozen=section.get('phrases',[])
            windows=frozen or [[section['a']+i*2000,min(section['b'],section['a']+(i+1)*2000)] for i in range(math.ceil((section['b']-section['a'])/2000))]
            phrases=defaultdict(list)
            for item in rows:
                for i,(a,b) in enumerate(windows):
                    if a<=item[0]<b:phrases[i].append(item);break
            weights={}
            for phrase,(start,end) in enumerate(windows):
                activity=1.
                if section['profile']:
                    support=0.
                    for p in section['profile']:
                        pa,pb=p['start_sample']*1000/section['sr']-origin_ms,p['end_sample']*1000/section['sr']-origin_ms
                        support+=max(0.,min(end,pb)-max(start,pa))*p['active_fraction']*(1+.4*p.get('activity_delta',0))
                    activity=support/max(1.,end-start)
                weights[phrase]=activity*(end-start)
            total=sum(weights.values());target=section['target'];capacity=sum(min(preset['chord'],max(1,len(c[2]))) for c in rows)
            # Allocate each phrase exactly once, including empty/rest phrases. An
            # internal empty window never gets a second copy of its phrase quota.
            exact=[target*weights[i]/total if total else 0. for i in range(len(windows))]
            quotas=[math.floor(q) for q in exact]
            spare=max(0,round(sum(exact))-sum(quotas))
            for i in sorted(range(len(windows)),key=lambda i:(-(exact[i]-quotas[i]),i))[:spare]:quotas[i]+=1
            taken=0;phrase_reports=[]
            phrase_bones={};all_bones=global_bones;bone_times=global_bone_times
            if selection_policy=='model_skeleton_then_audio':
                if selection_pass=='skeleton':
                    for phrase in range(len(windows)):
                        bones,bone_decisions,_=_fixed_phrase_selection(phrases[phrase],global_bones,quotas[phrase],preset,duration_ms,origin_ms,evidence,audio_vote_cap,stage='skeleton')
                        frozen_bone_map[(section['id'],phrase)]=bones;global_bones.extend(bones)
                        global_bone_times.update(d['start_ms'] for d in bone_decisions if d['type']=='selected')
                        decisions.extend(bone_decisions)
                    continue
                phrase_bones={phrase:frozen_bone_map[(section['id'],phrase)] for phrase in range(len(windows))}
            for phrase in range(len(windows)):
                events=phrases[phrase];desired=quotas[phrase]
                if selection_policy=='model_only':
                    voices,choice_decisions,_=_fixed_phrase_selection(events,selected,desired,preset,duration_ms,origin_ms,evidence,0.,stage='both')
                    for decision in choice_decisions:
                        if isinstance(decision.get('candidate_support'),dict):
                            decision['candidate_support']['selection_stage']='model_only_frozen_phrase'
                    decisions.extend(choice_decisions);selected.extend(voices);taken+=len(voices)
                    phrase_reports.append({'range_ms':windows[phrase],'target_heads':desired,'selected_heads':len(voices),
                        'candidate_heads':sum(len(c[2]) for c in events),
                        'unspent_heads':max(0,desired-len(voices)),'redistributed':False})
                    continue
                if selection_policy=='model_skeleton_then_audio':
                    bones=phrase_bones[phrase]
                    remaining=[c for c in events if not (c[2] and c[0] in bone_times)]
                    fill,choice_decisions,_=_fixed_phrase_selection(remaining,selected+all_bones,desired-len(bones),preset,duration_ms,origin_ms,evidence,audio_vote_cap,stage='fill')
                    voices=sorted(bones+fill,key=lambda n:(n.start,n.lane))
                    decisions.extend(choice_decisions);selected.extend(voices);taken+=len(voices)
                    phrase_reports.append({'range_ms':windows[phrase],'target_heads':desired,'selected_heads':len(voices),
                        'candidate_heads':sum(max(1,len(c[2])) for c in events),'fixed_qualified_skeleton_heads':len(bones),
                        'unspent_heads':max(0,desired-len(voices)),'redistributed':False})
                    continue
                ranked=sorted(events,key=lambda c:(-min(4.,c[1]),c[0]))
                supply=[]
                for candidate in ranked:
                    timestamp,strength,originals=candidate
                    ordered=sorted(originals,key=lambda n:(n.end is None,n.lane,n.start))
                    for original in ordered[preset['chord']:]:
                        decisions.append({'type':'omitted','start_ms':original.start,'lane':original.lane,
                            'reason':'user_chord_cap','candidate_provenance':getattr(candidate,'provenance',{'kind':'model'})})
                    for original in ordered[:preset['chord']] if ordered else [None]:
                        note=Note(original.start if original else timestamp,
                                  original.lane if original else int(rng.integers(0,4)),
                                  original.end if original else None)
                        supply.append((note,candidate))
                voices=[];cursor=0;attempted=[];repair_decisions=[]
                while len(voices)<desired and cursor<len(supply):
                    count=desired-len(voices);batch=[]
                    while cursor<len(supply) and len(batch)<count:
                        candidate=supply[cursor][1];stop=cursor+1
                        while stop<len(supply) and supply[stop][1] is candidate:stop+=1
                        group=supply[cursor:stop]
                        atomic=1<len(candidate[2])<=preset['chord']
                        if atomic and len(group)>count-len(batch):
                            decisions.append({'type':'omitted_model_chord','start_ms':candidate[0],
                                'heads':len(group),'reason':'frozen_phrase_budget_whole_model_chord',
                                'candidate_provenance':getattr(candidate,'provenance',{'kind':'model'})})
                            cursor=stop;continue
                        take=min(len(group),count-len(batch))
                        batch.extend(group[:take]);cursor+=take
                    if not batch:break
                    attempted.extend(batch)
                    repaired,hard=repair_playable(selected+voices+[n for n,c in batch],duration_ms,preset,pattern)
                    voices=[n for n in repaired if windows[phrase][0]<=n.start<windows[phrase][1]]
                    repair_decisions.extend(hard['decisions'])
                for note,candidate in supply[cursor:]:
                    decisions.append({'type':'omitted','start_ms':note.start,'lane':note.lane,
                        'reason':'frozen_phrase_budget_lower_priority',
                        'candidate_provenance':getattr(candidate,'provenance',{'kind':'model' if candidate[2] else 'acoustic'})})
                decisions.extend(repair_decisions)
                for note in voices:
                    source=next((c for n,c in attempted if n.start==note.start),None)
                    decisions.append({'type':'selected','start_ms':note.start,'lane':note.lane,
                        'reason':'frozen_phrase_evidence_after_hard_repair',
                        'candidate_provenance':getattr(source,'provenance',{'kind':'model' if source and source[2] else 'acoustic'}),
                        'candidate_support':getattr(source,'support',{})})
                selected.extend(voices);taken+=len(voices)
                phrase_reports.append({'range_ms':windows[phrase],'target_heads':desired,'selected_heads':len(voices),
                    'candidate_heads':len(supply),'attempted_heads':cursor,'refilled_after_hard_conflict':max(0,len(attempted)-desired),
                    'unspent_heads':max(0,desired-len(voices)),'redistributed':False})
            reports.append({'section_id':section['id'],'target_heads_soft':round(target,5),'candidate_heads':capacity,
                            'scheduled_heads':taken,'capacity_saturated':capacity<target,
                            'warning':'真实起音候选不足，未补网格音符' if capacity<target else None,'phrase_stats':phrase_reports})
    preset=base_preset
    if selection_policy in ('model_skeleton_then_audio','model_only'):
        notes=sorted(selected,key=lambda n:(n.start,n.lane));repair={'decisions':[]}
        if len(notes)<min_notes:raise ValueError('Too few valid notes')
    else:notes,repair=repair_playable(selected,duration_ms,preset,pattern,min_notes)
    decisions.extend(repair['decisions'])
    return notes, {'policy':VERSION,'selection_policy':selection_policy,'audio_vote_cap':audio_vote_cap,'skeleton_scope':'complete_frozen_plan' if selection_policy=='model_skeleton_then_audio' else None,'target_active_nps':preset['rate'],'target_chord_size':preset['chord_size'],
                   'max_chord':preset['chord'],'min_lane_gap_ms':preset['gap'],'max_hold_ms':preset['hold_ms'],
                   'peak_cap':preset['peak'],'candidate_attacks':len(valid),'scheduled_notes':len(selected),
                   'phone_filtered_notes':len(selected)-len(notes),'model_anchor_notes':sum(any(n.start==c[0] and c[2] for c in valid) for n in notes),
                   'candidate_model_chords':sum(len(c[2])>1 for c in valid),
                   'selected_complete_model_chords':sum(len(c[2])>1 and sum(n.start==c[0] for n in notes)==len(c[2]) for c in valid),
                   'selected_partial_model_chords':sum(len(c[2])>1 and 0<sum(n.start==c[0] for n in notes)<len(c[2]) for c in valid),
                   'candidate_model_heads':sum(len(c[2]) for c in valid),
                   'candidate_acoustic_heads':sum(not c[2] for c in valid),
                   'selected_model_heads':sum(any(n.start==c[0] and c[2] for c in valid) for n in notes),
                   'selected_acoustic_heads':sum(not any(n.start==c[0] and c[2] for c in valid) for n in notes),
                   'pattern':pattern,'global_budget_applied':True,'budget_passes':1,'section_stats':reports,'decisions':decisions,
                   'capacity_saturated':any(row['capacity_saturated'] for row in reports),
                   'same_lane_repeat_ratio':round(sum(a.lane==b.lane for a,b in zip(notes,notes[1:]))/max(1,len(notes)-1),4),
                   'hand_alternation_ratio':round(sum(a.lane//2!=b.lane//2 for a,b in zip(notes,notes[1:]))/max(1,len(notes)-1),4)}
