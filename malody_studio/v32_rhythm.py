"""Read native model rhythm evidence without importing model dependencies."""
from bisect import bisect_right
from collections import defaultdict, deque
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

VERSION = 'v32-rhythm-v1'
_IGNORE = {1: (), 4: (2,), 6: (2, 3), 8: (4,), 9: (3,), 10: (2, 5),
           12: (4, 6), 14: (2, 7), 15: (3, 5), 16: (8,)}
_TYPES = {'circle', 'hold_note', 'hold_note_end', 'beat', 'measure', 'kiai',
          'timing_point', 'spinner', 'spinner_end', 'slider_head', 'slider_end',
          'bezier_anchor', 'perfect_anchor', 'catmull_anchor', 'red_anchor', 'last_anchor',
          'scroll_speed_change', 'drumroll', 'drumroll_end', 'denden', 'denden_end'}


def unavailable(reason):
    return {'version': VERSION, 'token_step_ms': 10, 'available': False, 'reason': reason}


def _groups(events, types_first, event_times=None):
    group = {}
    for index, (kind, value) in enumerate(events):
        if kind in _TYPES and types_first:
            if 'kind' in group:
                yield group
                group = {}
            group['kind'] = kind
            if event_times is not None: group['time'] = event_times[index]
        elif kind == 't': group['time'] = float(value)
        elif kind == 'snap': group['snap'] = int(value)
        elif kind == 'pos_x': group['x'] = float(value)
        elif kind == 'column': group['lane'] = int(value)
        if kind in _TYPES and not types_first:
            group['kind'] = kind
            if event_times is not None: group['time'] = event_times[index]
            yield group
            group = {}
    if 'kind' in group:
        yield group


def _timing(lines):
    points = {}
    for line in lines:
        fields = line.split(',') if isinstance(line, str) else line
        if len(fields) >= 7 and str(fields[6]).strip() != '1': continue
        anchor, beat = float(fields[0]), float(fields[1])
        if math.isfinite(anchor) and math.isfinite(beat) and beat > 0:
            points[anchor] = beat
    return sorted(points.items())


def _grid(time, divisor, points):
    """Match native snapping's integer ticks, coarse exclusions and ±20ms seams."""
    index = max(0, bisect_right([p[0] for p in points], time) - 1)
    ticks = {}
    def local(anchor, beat, div):
        step = beat / div
        remainder = (time-anchor) % step
        return {int(time-remainder+k*step) for k in (-1, 0, 1, 2)}
    for j in range(max(0, index-1), min(len(points), index+2)):
        anchor, beat = points[j]
        candidates = local(anchor, beat, divisor)
        for ignored in _IGNORE.get(divisor, (1,)):
            candidates -= local(anchor, beat, ignored)
        left, right = points[index][0], points[index+1][0] if index+1<len(points) else math.inf
        if j == index: candidates = {t for t in candidates if left-20 <= t <= right+20}
        elif j < index: candidates = {t for t in candidates if t <= left+20}
        else: candidates = {t for t in candidates if t >= right-20}
        for tick in candidates: ticks.setdefault(tick, (anchor, beat))
    if not ticks: return None
    tick = min(ticks, key=lambda t: (abs(time-t), t))
    return tick, *ticks[tick]


def read_rhythm(path):
    path = Path(path)
    if not path.is_file(): return [], unavailable('model_events_sidecar_missing')
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        times = payload.get('event_times')
        if times is not None and (len(times) != len(payload['events']) or
                                  not all(math.isfinite(float(t)) for t in times)):
            raise ValueError('Invalid native parallel clock')
        points = _timing(payload.get('timing', []))
        fingerprint = hashlib.sha256(json.dumps(points, separators=(',', ':')).encode()).hexdigest()
        rows = []
        for group in _groups(payload['events'], bool(payload.get('types_first')), times):
            if group['kind'] not in ('circle', 'hold_note') or 'time' not in group: continue
            lane = group.get('lane')
            if lane is None and 'x' in group: lane = int(group['x']*payload.get('keycount',4)//512)
            if lane not in range(4): continue
            snap, time = group.get('snap'), group['time']
            rhythm = {'version': VERSION, 'token_step_ms': 10, 'snap_divisor': snap,
                      'model_time_ms': time, 'grid_time_ms': None, 'grid_error_ms': None,
                      'beat_length_ms': None, 'timing_anchor_ms': None,
                      'timing_fingerprint': fingerprint, 'timing_source': 'model_output',
                      'available': False}
            grid = _grid(time, snap, points) if points and snap is not None and snap > 0 else None
            if grid:
                tick, anchor, beat = grid
                rhythm.update(grid_time_ms=tick, grid_error_ms=time-tick,
                              beat_length_ms=beat, timing_anchor_ms=anchor, available=True)
            else: rhythm['reason'] = 'native_timing_missing' if not points else 'snap_tag_missing_or_unsnapped' if not snap else 'native_grid_unavailable'
            rows.append({'start_ms': time, 'lane': lane, 'model_rhythm': rhythm})
        return rows, None
    except (ValueError, TypeError, KeyError, IndexError, OverflowError):
        return [], unavailable('model_events_sidecar_invalid')


def attach_notes(notes, rows=(), missing=None, source_role=None):
    """Exact native head ownership: no near-time pairing or note synthesis."""
    index = defaultdict(deque)
    for row in rows: index[(float(row['start_ms']), int(row['lane']))].append(row['model_rhythm'])
    for note in notes:
        candidates = index[(float(note.start), int(note.lane))]
        rhythm = deepcopy(candidates.popleft() if candidates else missing or unavailable('model_head_evidence_missing'))
        if source_role is not None: rhythm['source_role'] = source_role
        # Note remains a three-field immutable value; this additional evidence is
        # explicitly serialized beside its values in raw caches below.
        object.__setattr__(note, 'model_rhythm', rhythm)
    return notes


def note_evidence(notes):
    return [{'start_ms': n.start, 'lane': n.lane, 'model_rhythm': deepcopy(getattr(n, 'model_rhythm', unavailable('model_events_sidecar_missing')))} for n in notes]


def transfer_notes(notes, originals, source_role=None, *, unavailable_on_missing=True):
    """Carry an existing head's evidence through lane/duration arrangement."""
    pool = defaultdict(list)
    for original in originals: pool[float(original.start)].append(original)
    for note in notes:
        candidates = pool[float(note.start)]
        original = min(candidates, key=lambda n: n.lane != note.lane) if candidates else None
        if original is not None: candidates.remove(original)
        if not unavailable_on_missing and not hasattr(original, 'model_rhythm'):
            continue
        rhythm = deepcopy(getattr(original, 'model_rhythm', unavailable('model_head_evidence_missing')))
        if source_role is not None: rhythm['source_role'] = source_role
        object.__setattr__(note, 'model_rhythm', rhythm)
    return notes


def hydrate_cache(cache, raw, directory):
    """Read old sidecars in place; never rewrite cache or historical revisions."""
    metadata = cache.get('metadata', {})
    charts = metadata.get('charts', {})
    for key, notes in raw.items():
        rows = cache.get('model_rhythm', {}).get(key)
        if rows is None:
            rows = []
            records = [r for r in cache.get('records', []) if r.get('difficulty_key') == key]
            if records:
                for record in records:
                    group = record.get('inference_group', record.get('key'))
                    retry = record.get('selected_round', 0)
                    suffix = '__density_retry'+str(retry) if retry and record.get('density_policy') else '__retry' if retry else ''
                    chart = charts.get(str(group)+suffix)
                    if chart:
                        evidence, _ = read_rhythm(Path(chart).parent/'model-events.json')
                        a,b = (v*1000/44100 for v in record['core'])
                        rows.extend(r for r in evidence if a <= r['start_ms'] < b)
            elif key in charts: rows, _ = read_rhythm(Path(charts[key]).parent/'model-events.json')
        attach_notes(notes, rows or (), source_role=cache.get('source', {}).get('source_role'))
    return raw
