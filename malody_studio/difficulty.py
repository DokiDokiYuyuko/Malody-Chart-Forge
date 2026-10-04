"""Audio-assisted difficulty calibration for V32; these are not official levels.

Use detected attacks, never invented grid subdivisions. One model master supplies
timing, lane preferences and hold candidates for all four versions.
"""
from collections import defaultdict
import math
import numpy as np
from scipy.signal import find_peaks
from scipy.ndimage import maximum_filter1d
from .charts import Note, clean_notes

PRESETS = {
    'easy': dict(label='Easy', sr=8.0, rate=2.5, chord_size=1.0, gap=190, chord=1, peak=5, hold_ms=1200),
    'medium': dict(label='Medium', sr=8.0, rate=5.0, chord_size=1.05, gap=120, chord=2, peak=8, hold_ms=900),
    'hard': dict(label='Hard', sr=8.0, rate=8.5, chord_size=1.15, gap=80, chord=3, peak=13, hold_ms=650),
    'expert': dict(label='Expert', sr=8.0, rate=13.0, chord_size=1.25, gap=55, chord=4, peak=19, hold_ms=400),
    'master': dict(label='Master', sr=8.0, rate=18.5, chord_size=1.6, gap=45, chord=4, peak=27, hold_ms=250),
    'lunatic': dict(label='Lunatic', sr=8.0, rate=26.0, chord_size=2.1, gap=35, chord=4, peak=38, hold_ms=180),
}
PATTERN_CHOICES = ('balanced', 'stream', 'jumpstream', 'handstream', 'chordjack',
                   'stamina', 'jackspeed', 'speed', 'technical')
PATTERN_LABELS = {
    'balanced': 'Balanced', 'jackspeed': 'Jack', 'stream': 'Stream',
    'speed': 'Speed', 'jumpstream': 'Jumpstream', 'handstream': 'Handstream',
    'chordjack': 'Chordjack', 'stamina': 'Stamina', 'technical': 'Technical',
}
V32_PATTERN_TAGS = {
    'stream': 'skillset/streams', 'jumpstream': 'style/jumpstream',
    'handstream': 'style/handstream', 'chordjack': 'style/chordjack',
    'stamina': 'streams/stamina', 'jackspeed': 'skillset/speedjack',
    'speed': 'skillset/streams', 'technical': 'skillset/tech',
}
POLICY = '共享母谱 + 音频起音辅助六档难度分层 v2'

def attacks(y, sr, master):
    import librosa
    hop = max(1, round(sr * .01))
    # Positive log spectral flux in three bands finds kick, voice and percussion
    # attacks independently. STFT columns are centered on the source timestamps.
    magnitude = np.abs(librosa.stft(y, n_fft=1024, hop_length=hop))
    frequencies = librosa.fft_frequencies(sr=sr, n_fft=1024)
    envelopes = []
    for lower, upper in ((50, 250), (250, 2000), (2000, sr / 2)):
        band = np.log1p(magnitude[(frequencies >= lower) & (frequencies < upper)] * 10)
        flux = np.maximum(np.diff(band, axis=1, prepend=band[:, :1]), 0).mean(axis=0)
        scale = float(np.percentile(flux, 95))
        envelopes.append(flux / max(scale, 1e-6))
    envelope = np.maximum.reduce(envelopes)
    peaks, _ = find_peaks(envelope, distance=7, prominence=.18, height=.25)
    rms = librosa.feature.rms(S=magnitude, frame_length=1024, hop_length=hop)[0]
    nearby_rms = maximum_filter1d(rms, size=5)
    audible = max(float(np.max(rms)) * .015, 1e-5)
    candidates = [(float(p * hop * 1000 / sr), float(envelope[p]), [])
                  for p in peaks if nearby_rms[p] > audible]
    groups = defaultdict(list)
    for note in master:
        frame = min(len(rms) - 1, max(0, round(note.start * sr / (1000 * hop))))
        if rms[frame] > audible:
            groups[round(note.start)].append(note)
    candidates += [(float(t), float(envelope[min(len(envelope)-1, round(t * sr / (1000*hop)))]) + .7, notes)
                   for t, notes in groups.items()]
    # Keep the model's original timestamp when an attack is close to its note.
    merged = []
    for item in sorted(candidates):
        if merged and item[0] - merged[-1][0] < 50:
            previous = merged[-1]
            if item[2] or (not previous[2] and item[1] > previous[1]):
                merged[-1] = item
        else:
            merged.append(item)
    return merged

def calibrate(candidates, duration_ms, key, ln_ratio, seed, overrides=None, pattern='balanced', min_notes=8):
    preset = {**PRESETS[key], **(overrides or {}), 'min_notes': min_notes}
    rng = np.random.default_rng(seed)
    windows = defaultdict(list)
    for candidate in candidates:
        if 0 <= candidate[0] < duration_ms:
            windows[int(candidate[0] // 4000)].append(candidate)
    scheduled = []
    for index, events in sorted(windows.items()):
        # Restrict budgets to detected musical activity, including song endings.
        span = min(4000, duration_ms - index * 4000,
                   events[-1][0] - events[0][0] + 250) / 1000
        budget = max(1, round(preset['rate'] * span))
        count = min(len(events), max(1, math.ceil(budget / preset['chord_size'])))
        # Prefer strong attacks while discouraging clusters at one moment.
        chosen = []
        remaining = events[:]
        while remaining and len(chosen) < count:
            def priority(event):
                distance = min((abs(event[0] - e[0]) for e in chosen), default=1000)
                return min(event[1], 4) * .35 + min(distance / 200, 2)
            event = max(remaining, key=priority)
            chosen.append(event)
            remaining.remove(event)
        chosen.sort(key=lambda event: event[0])
        voices = [1] * len(chosen)
        # Avoid manufacturing dense chords just to meet an average-NPS target
        # when the audio supplies too few distinct attacks. The target chord
        # size is a soft ceiling on voices per selected attack.
        desired = min(budget, round(len(chosen) * preset['chord_size']))
        extra = min(max(0, desired - len(chosen)), len(chosen) * (preset['chord'] - 1))
        order = sorted(range(len(chosen)), key=lambda i: chosen[i][1], reverse=True)
        while extra:
            for i in order:
                if extra and voices[i] < preset['chord']:
                    voices[i] += 1
                    extra -= 1
        scheduled.extend((event, voice) for event, voice in zip(chosen, voices))
    hold_slots = sum(min(voices, sum(n.end is not None for n in event[2])) for event, voices in scheduled)
    hold_probability = min(1, ln_ratio * sum(v for _, v in scheduled) / max(1, hold_slots))
    output, last, occupied, counts = [], [-1e9]*4, [-1e9]*4, [0]*4
    last_lane = None
    lane_history = []
    for (timestamp, strength, originals), voices in scheduled:
        preferred = [note.lane for note in originals]
        if pattern == 'jackspeed' and last_lane is not None:
            # Favor a repeated lane while the selected difficulty's gap rule
            # remains the hard limit. If unavailable, fall back to a safe lane.
            lanes = [last_lane] + [lane for lane in range(4) if lane != last_lane]
        elif pattern == 'stream':
            previous = lane_history[-1] if lane_history else None
            lanes = sorted(range(4), key=lambda lane: (
                lane == previous, counts[lane] * 60 - (200 if lane in preferred else 0), last[lane]))
        elif pattern == 'speed':
            previous = lane_history[-1] if lane_history else None
            previous_hand = previous // 2 if previous is not None else None
            lanes = sorted(range(4), key=lambda lane: (
                lane // 2 == previous_hand, lane == previous,
                counts[lane] * 60 - (200 if lane in preferred else 0), last[lane]))
        else:
            lanes = sorted(range(4), key=lambda lane: (
                counts[lane]*60 - (200 if lane in preferred else 0), last[lane]))
        available = [lane for lane in lanes if timestamp - last[lane] >= preset['gap']
                     and timestamp >= occupied[lane] + 35]
        for lane in available[:voices]:
            original = next((note for note in originals if note.lane == lane and note.end), None)
            end = None
            if original and rng.random() < hold_probability:
                end = min(original.end, timestamp + preset['hold_ms'], duration_ms)
                if end - timestamp < 100:
                    end = None
            output.append(Note(timestamp, lane, end))
            last[lane], occupied[lane] = timestamp, end or timestamp
            counts[lane] += 1
            last_lane = lane
            lane_history.append(lane)
    notes = clean_notes(output, duration_ms, preset)
    return notes, {'policy': POLICY, 'target_active_nps': preset['rate'],
                   'target_chord_size': preset['chord_size'],
                   'max_chord': preset['chord'], 'min_lane_gap_ms': preset['gap'],
                   'max_hold_ms': preset['hold_ms'], 'peak_cap': preset['peak'],
                   'candidate_attacks': len(candidates), 'scheduled_notes': sum(v for _, v in scheduled),
                   'phone_filtered_notes': len(output) - len(notes),
                   'model_anchor_notes': sum(any(abs(n.start-c[0]) < .1 and c[2] for c in candidates) for n in notes),
                   'pattern': pattern,
                   'same_lane_repeat_ratio': round(sum(a.lane == b.lane for a, b in zip(notes, notes[1:])) / max(1, len(notes)-1), 4),
                   'hand_alternation_ratio': round(sum((a.lane // 2) != (b.lane // 2) for a, b in zip(notes, notes[1:])) / max(1, len(notes)-1), 4)}
