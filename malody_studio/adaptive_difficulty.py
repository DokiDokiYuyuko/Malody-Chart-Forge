"""Evidence-preserving attack selection and separately logged phone constraints."""
from collections import defaultdict, deque
import hashlib
import math
import numpy as np
from scipy.signal import find_peaks
from scipy.ndimage import maximum_filter1d
from .charts import Note
from .difficulty import PRESETS

VERSION = 'phrase-selection-v1'


def attacks_adaptive(y, sr, master):
    import librosa
    y = np.asarray(y, dtype=np.float32)
    if y.ndim != 1 or not len(y) or not np.isfinite(y).all():
        raise ValueError('起音分析需要有效单声道音频')
    if np.max(np.abs(y)) < 1e-5:
        return []
    hop = max(1, round(sr * .005))
    magnitude = np.abs(librosa.stft(y, n_fft=512, hop_length=hop))
    frequencies = librosa.fft_frequencies(sr=sr, n_fft=512)
    envelopes = []
    for lower, upper in ((40, 250), (250, 2000), (2000, sr / 2 + 1)):
        band = np.log1p(magnitude[(frequencies >= lower) & (frequencies < upper)] * 10)
        flux = np.maximum(np.diff(band, axis=1, prepend=band[:, :1]), 0).mean(axis=0)
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
        audio.append((timestamp, float(envelope[frame]), []))
    model = []
    for timestamp, notes in groups.items():
        frame = min(len(envelope)-1, max(0, round(timestamp * sr / (1000*hop))))
        model.append((timestamp, float(envelope[frame]) + .7, notes))
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
        lane = available[0]
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
                              'original_end_ms':note.end,'end_ms':end,'reason':'four_lane_preference_and_user_hard_caps'})
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
                         'profile':section.get('profile',[]),'sr':plan['sample_rate'],'origin':origin_ms})
    return sections


def calibrate_adaptive(candidates, duration_ms, key, ln_ratio, seed, overrides=None,
                       pattern='balanced', plan=None, min_notes=0, origin_ms=0):
    if plan is not None and not plan.get('dynamic_enabled',False):
        from .difficulty import calibrate
        return calibrate(candidates,duration_ms,key,ln_ratio,seed,overrides,pattern,min_notes=min_notes)
    preset={**PRESETS[key],**(overrides or {})}
    sections=_plan_sections(plan,duration_ms,origin_ms,key,preset)
    rng=np.random.default_rng(seed)
    selected=[];reports=[];decisions=[]
    valid=[c for c in candidates if math.isfinite(c[0]) and 0<=c[0]<duration_ms]
    for section in sections:
        rows=[c for c in valid if section['a']<=c[0]<section['b']]
        # Quantities follow evidence phrases (up to two seconds), never a fixed
        # every-four-second equal quota. Profile rests have zero target weight.
        phrases=defaultdict(list)
        for item in rows:phrases[int((item[0]-section['a'])//2000)].append(item)
        weights={}
        for phrase,events in phrases.items():
            start=section['a']+phrase*2000;end=min(section['b'],start+2000)
            activity=1.
            if section['profile']:
                support=0.
                for p in section['profile']:
                    pa,pb=p['start_sample']*1000/section['sr']-origin_ms,p['end_sample']*1000/section['sr']-origin_ms
                    support+=max(0.,min(end,pb)-max(start,pa))*p['active_fraction']*(1+.4*p.get('activity_delta',0))
                activity=support/max(1.,end-start)
            evidence=sum(min(4.,max(.1,c[1]))*min(preset['chord'],max(1,len(c[2]))) for c in events)
            weights[phrase]=activity*math.sqrt(evidence)
        total=sum(weights.values());target=section['target'];capacity=sum(min(preset['chord'],max(1,len(c[2]))) for c in rows)
        taken=0
        for phrase,events in sorted(phrases.items()):
            budget=target*weights[phrase]/total if total else 0.
            if budget<=0:continue
            desired=max(0,round(budget))
            ranked=sorted(events,key=lambda c:(-min(4.,c[1]),c[0]))
            voices=[];used=0
            for timestamp,strength,originals in ranked:
                count=min(preset['chord'],max(1,len(originals)))
                # A chord needs actual model voices; an audio-only peak is one.
                count=min(count,max(0,desired-used))
                if not count:continue
                ordered=sorted(originals,key=lambda n:(n.lane,n.start))
                for index in range(count):
                    original=ordered[index] if index<len(ordered) else None
                    end=original.end if original and original.end is not None and rng.random()<ln_ratio else None
                    note=Note(original.start if original else timestamp,original.lane if original else int(rng.integers(0,4)),end)
                    voices.append(note)
                used+=count
                if used>=desired:break
            selected.extend(voices);taken+=len(voices)
        reports.append({'section_id':section['id'],'target_heads_soft':round(target,5),'candidate_heads':capacity,
                        'scheduled_heads':taken,'capacity_saturated':capacity<target,
                        'warning':'采音容量不足，未补网格音符' if capacity<target else None})
    notes,repair=repair_playable(selected,duration_ms,preset,pattern,min_notes)
    decisions.extend(repair['decisions'])
    return notes, {'policy':VERSION,'target_active_nps':preset['rate'],'target_chord_size':preset['chord_size'],
                   'max_chord':preset['chord'],'min_lane_gap_ms':preset['gap'],'max_hold_ms':preset['hold_ms'],
                   'peak_cap':preset['peak'],'candidate_attacks':len(valid),'scheduled_notes':len(selected),
                   'phone_filtered_notes':len(selected)-len(notes),'model_anchor_notes':sum(any(n.start==c[0] and c[2] for c in valid) for n in notes),
                   'pattern':pattern,'global_budget_applied':True,'budget_passes':1,'section_stats':reports,'decisions':decisions,
                   'capacity_saturated':any(row['capacity_saturated'] for row in reports),
                   'same_lane_repeat_ratio':round(sum(a.lane==b.lane for a,b in zip(notes,notes[1:]))/max(1,len(notes)-1),4),
                   'hand_alternation_ratio':round(sum(a.lane//2!=b.lane//2 for a,b in zip(notes,notes[1:]))/max(1,len(notes)-1),4)}
