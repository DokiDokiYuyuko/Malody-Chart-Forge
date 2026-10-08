"""Priority / relane merge of two independently generated stem charts.

Pure functions: stem events in, merged events and an audit trail out. No note is
ever created and no head time is ever changed. This is an explicit,
user-authorised exception to the "keep every model head" rule, so every
dropped / relaned / shortened note is recorded in ``decisions``.
"""
import bisect
import copy

VERSION = 'direct-priority-merge-v3'
MODES = ('vocals_priority', 'accompaniment_priority', 'relane')
PRIMARIES = ('vocals', 'accompaniment')
ROLES = ('vocals', 'accompaniment')
DEFAULT_GAP_MS = 60.
MIN_HOLD_MS = 60.  # shortest hold kept when the merge shortens one; below this it becomes a tap
MAX_SWEEPS = 64  # hard bound of the spacing fixed point
EPS = 1e-9
TOLERANCE_MS = .1  # same tolerance as advanced.valid_events
ATTACK_WINDOW_MS = 40.  # S head this close to a P head is the same musical attack
ATTACK_CAP_FRACTION = .45  # ... but never more than this share of the S stem's own nearest head interval
DEFAULT_CHORD_CAP = 4

PRIMARY_INPUT_CONFLICT = 'primary_input_conflict'
STEM_SILENT_SPAN = 'stem_silent_span'
TAIL_SPACING = 'tail_spacing'
TAIL_SPACING_RELANE = 'tail_spacing_relane'
LANE_GAP = 'lane_gap'
SAME_ATTACK_DUPLICATE = 'same_attack_duplicate'
CHORD_CAP = 'chord_cap'
COMMON_ATTACK = 'common_attack'


def validate_options(mode, primary):
    if mode not in MODES:
        raise ValueError('声部融合方式无效')
    if primary not in PRIMARIES:
        raise ValueError('声部融合主次无效')


def priority_side(mode, primary):
    """Return (P, S) role names for a fusion mode."""
    validate_options(mode, primary)
    side = {'vocals_priority': 'vocals', 'accompaniment_priority': 'accompaniment'}.get(mode, primary)
    return side, ('accompaniment' if side == 'vocals' else 'vocals')


def _occupied(event):
    end = event.get('end_ms')
    return event['start_ms'] if end is None else end


class _Lane:
    """Sorted notes of one lane with prefix-maximum occupied end."""

    def __init__(self):
        self.starts = []
        self.items = []
        self.best = []  # index of the item with the largest occupied end in items[:i+1]

    def add(self, item):
        start = item['event']['start_ms']
        index = bisect.bisect_right(self.starts, start)
        self.starts.insert(index, start)
        self.items.insert(index, item)
        self._rebuild(index)

    def _rebuild(self, first):
        del self.best[first:]
        for i in range(first, len(self.items)):
            current = _occupied(self.items[i]['event'])
            if i and _occupied(self.items[self.best[i - 1]]['event']) >= current:
                self.best.append(self.best[i - 1])
            else:
                self.best.append(i)

    def index(self, item):
        index = bisect.bisect_left(self.starts, item['event']['start_ms'])
        while index < len(self.items) and self.items[index] is not item:
            index += 1
        return index if index < len(self.items) else None

    def remove(self, item):
        index = self.index(item)
        del self.starts[index]
        del self.items[index]
        self._rebuild(index)

    def room(self, start, end, gap):
        """True when a note spanning [start, end] fits with ``gap`` ms to the previous tail / head and to the next head."""
        low = bisect.bisect_left(self.starts, start - TOLERANCE_MS)
        high = bisect.bisect_right(self.starts, start + TOLERANCE_MS)
        if high > low:
            return False
        if low and start - _occupied(self.items[self.best[low - 1]]['event']) < gap - EPS:
            return False
        if high < len(self.starts) and self.starts[high] - (start if end is None else end) < gap - EPS:
            return False
        return True

    def conflicts(self, start, end, found):
        low = bisect.bisect_left(self.starts, start - TOLERANCE_MS)
        high = bisect.bisect_right(self.starts, start + TOLERANCE_MS)
        if high > low:
            found.setdefault('same_time', self.items[low])
        if low:
            top = self.items[self.best[low - 1]]
            if _occupied(top['event']) - TOLERANCE_MS > start:
                found.setdefault('inside_hold', top)
        if end is not None and high < len(self.starts) and self.starts[high] < end - TOLERANCE_MS:
            found.setdefault('covers_note', self.items[high])


def _ref(item):
    event = item['event']
    return {'event_id': event.get('id'), 'role': item['role'], 'start_ms': event['start_ms'],
            'lane': event['lane'], 'end_ms': event.get('end_ms')}


def _residual(a, b, source, gap):
    """A same-stem pair the model itself wrote closer than ``gap`` in one lane: both notes are still in their own
    lane and their own original start times were already that close. The merge leaves these untouched and counts them."""
    if a['role'] != b['role']:
        return False
    originals = [source[(item['role'], item['event'].get('id'))] for item in (a, b)]
    return (all(item['event']['lane'] == original['lane'] for item, original in zip((a, b), originals))
            and abs(originals[1]['start_ms'] - originals[0]['start_ms']) < gap - EPS)


def _validate_input(events, role):
    if not isinstance(events, list):
        raise ValueError('声部音符列表无效')
    for event in events:
        if not isinstance(event, dict) or event.get('lane') not in range(4) or isinstance(event.get('lane'), bool):
            raise ValueError('声部融合收到无效轨道')
        if not isinstance(event.get('start_ms'), (int, float)) or isinstance(event.get('start_ms'), bool):
            raise ValueError('声部融合收到无效时间')
        end = event.get('end_ms')
        if end is not None and (not isinstance(end, (int, float)) or isinstance(end, bool) or not end > event['start_ms']):
            raise ValueError('声部融合收到无效长条尾')


def _validate_silent_spans(silent_spans):
    if not isinstance(silent_spans, dict) or set(silent_spans) - set(ROLES):
        raise ValueError('静音段声明无效')
    for runs in silent_spans.values():
        for run in runs or []:
            if (not isinstance(run, dict) or any(isinstance(run.get(k), bool) or not isinstance(run.get(k), (int, float))
                                                 for k in ('declared_start_ms', 'declared_end_ms'))
                    or not run['declared_end_ms'] > run['declared_start_ms']):
                raise ValueError('静音段声明无效')


def inside_silent_run(start_ms, runs):
    """The declared run strictly containing a head time, else None."""
    return next((run for run in runs or [] if run['declared_start_ms'] < start_ms < run['declared_end_ms']), None)




def _nearest_head(p_times, p_heads, t):
    """Index of the primary head nearest to ``t``; ties go to the earlier head, then the lower lane."""
    i = bisect.bisect_left(p_times, t)
    candidates = []
    if i < len(p_times):
        candidates.append(i)  # first head at or after t: the lowest lane at that time
    if i:
        candidates.append(bisect.bisect_left(p_times, p_times[i - 1]))  # first head of the time just before t
    return min(candidates, key=lambda k: (abs(p_times[k] - t), p_times[k], p_heads[k][1]))


def _plan_alignment(s_events, p_heads, *, window_ms, min_hold_ms, runs):
    """Decide which secondary heads are the same attack as a primary head.

    Pure planning on the ORIGINAL secondary times: ``{id(event): (new_start, new_end, primary_head, dt)}``.
    A snap is refused whenever it could cross, collapse onto or run into another note of the secondary
    stem itself, so the stem's own fast rhythm is never folded."""
    plan = {}
    if not p_heads or window_ms <= 0:
        return plan
    p_times = [row[0] for row in p_heads]
    ordered = sorted(s_events, key=lambda e: (e['start_ms'], e['lane'], str(e.get('id'))))
    s_times = [e['start_ms'] for e in ordered]
    lane_rows = {lane: sorted((e['start_ms'], _occupied(e)) for e in ordered if e['lane'] == lane) for lane in range(4)}
    lane_starts = {lane: [row[0] for row in rows] for lane, rows in lane_rows.items()}
    lane_best = {}
    for lane, rows in lane_rows.items():
        best, top = [], None
        for _, occupied in rows:
            top = occupied if top is None or occupied > top else top
            best.append(top)
        lane_best[lane] = best
    claimed = set()
    for event in ordered:
        old, lane = event['start_ms'], event['lane']
        k = _nearest_head(p_times, p_heads, old)
        new = p_times[k]
        dt = abs(new - old)
        if dt <= TOLERANCE_MS or dt > window_ms:
            continue
        low = bisect.bisect_left(s_times, old - TOLERANCE_MS)
        high = bisect.bisect_right(s_times, old + TOLERANCE_MS)
        intervals = ([old - s_times[low - 1]] if low else []) + ([s_times[high] - old] if high < len(s_times) else [])
        if intervals and dt > ATTACK_CAP_FRACTION * min(intervals):
            continue  # inside the stem's own rhythm: never folded
        if new > old:  # another own head at or between the old and the new time (chord mates at the old time move too)
            if high < len(s_times) and s_times[high] <= new + TOLERANCE_MS:
                continue
        else:
            first = bisect.bisect_left(s_times, new - TOLERANCE_MS)
            if first < len(s_times) and s_times[first] < old - TOLERANCE_MS:
                continue
            before = bisect.bisect_left(lane_starts[lane], old - TOLERANCE_MS)  # would land inside an own earlier hold
            if before and lane_best[lane][before - 1] > new + TOLERANCE_MS:
                continue
        if inside_silent_run(new, runs) or (lane, new) in claimed:
            continue
        claimed.add((lane, new))
        end = event.get('end_ms')
        new_end = None if end is not None and new >= end - min_hold_ms else end
        plan[id(event)] = (new, new_end, p_heads[k], dt)
    return plan


def _check_options(attack_window_ms, chord_cap):
    if (isinstance(attack_window_ms, bool) or not isinstance(attack_window_ms, (int, float))
            or not 0 <= attack_window_ms < float('inf')):
        raise ValueError('同击点对齐窗口无效')
    if isinstance(chord_cap, bool) or not isinstance(chord_cap, int) or not 1 <= chord_cap <= 4:
        raise ValueError('和弦上限无效')


def merge_stems(vocals, accompaniment, *, mode, primary='vocals', gap_ms=DEFAULT_GAP_MS, min_hold_ms=MIN_HOLD_MS,
                silent_spans=None, attack_window_ms=ATTACK_WINDOW_MS, chord_cap=DEFAULT_CHORD_CAP):
    """Merge two stems. P notes are placed first; their heads never move.

    ``silent_spans`` ({role: declared runs}) first drops, per stem, every note whose head lies
    strictly inside a silent run of that stem's own audio; each is recorded and counted as dropped.

    Common-attack alignment: a secondary head within ``attack_window_ms`` of a primary head (and never
    more than 45 % of the secondary stem's own nearest head interval) is the same attack from a separate
    model run, so its head time is snapped to the primary head before the lane rules run. The final
    tail pass keeps every hold tail ``gap_ms`` before the next head of its lane (reason ``tail_spacing``)."""
    p_role, s_role = priority_side(mode, primary)
    relane = mode == 'relane'
    _check_options(attack_window_ms, chord_cap)
    attack_window_ms = float(attack_window_ms)
    inputs = {'vocals': vocals, 'accompaniment': accompaniment}
    for role in ROLES:
        _validate_input(inputs[role], role)
    if silent_spans is not None:
        _validate_silent_spans(silent_spans)
    all_inputs = inputs
    inputs, silent_dropped = {}, {role: [] for role in ROLES}
    for role in ROLES:
        inputs[role] = []
        for event in all_inputs[role]:
            run = inside_silent_run(event['start_ms'], (silent_spans or {}).get(role))
            (silent_dropped[role].append((event, run)) if run else inputs[role].append(event))
    p_events, s_events = inputs[p_role], inputs[s_role]
    source = {(role, event.get('id')): event for role in ROLES for event in all_inputs[role]}

    lanes = [_Lane() for _ in range(4)]
    kept = {role: [] for role in ROLES}
    decisions = []
    counts = {role: {'input': len(all_inputs[role]), 'kept': 0, 'dropped': len(silent_dropped[role]), 'relaned': 0,
                     'shortened': 0, 'to_tap': 0} for role in ROLES}
    for role in ROLES:  # sparse: the historical count shape is unchanged when nothing is dropped
        if silent_dropped[role]:
            counts[role]['stem_silent'] = len(silent_dropped[role])
    for role in ROLES:
        for event, run in silent_dropped[role]:
            origin = (event.get('origins') or [None])[0]
            decisions.append({'event_id': event.get('id'), 'role': role, 'action': 'dropped', 'reason': STEM_SILENT_SPAN,
                              'origin': copy.deepcopy(origin), 'start_ms': event['start_ms'], 'lane': event['lane'],
                              'end_ms': event.get('end_ms'), 'conflict': None,
                              'silent_run': {k: run[k] for k in ('start_ms', 'end_ms', 'declared_start_ms',
                                                                  'declared_end_ms', 'level_db') if k in run}})

    def record(event, action, reason, conflict, role=s_role, **extra):
        origin = (event.get('origins') or [None])[0]
        decisions.append({'event_id': event.get('id'), 'role': role, 'action': action, 'reason': reason,
                          'origin': copy.deepcopy(origin), 'start_ms': event['start_ms'],
                          'conflict': _ref(conflict), **extra})

    def bump(role, key):
        counts[role][key] = counts[role].get(key, 0) + 1

    # Primary notes are placed first and are never changed by the merge. The only
    # exception is a defect of the primary stem itself (two of its own notes
    # colliding, e.g. a hold running across a generation-window seam): the same
    # minimal rule as for secondary holds is applied and recorded with reason
    # PRIMARY_INPUT_CONFLICT, because a chart with such a collision is not valid.
    for lane_index in range(4):
        previous = None
        for event in sorted((e for e in p_events if e['lane'] == lane_index),
                            key=lambda e: (e['start_ms'], str(e.get('id')))):
            copied = copy.deepcopy(event)
            if previous is not None:
                if copied['start_ms'] - previous['event']['start_ms'] <= TOLERANCE_MS:
                    counts[p_role]['dropped'] += 1
                    record(copied, 'dropped', PRIMARY_INPUT_CONFLICT, previous, role=p_role,
                           lane=lane_index, end_ms=copied.get('end_ms'))
                    continue
                before = previous['event']
                if copied['start_ms'] < _occupied(before) - TOLERANCE_MS:
                    old_end, new_end = before.get('end_ms'), copied['start_ms'] - gap_ms
                    before['end_ms'] = None if new_end - before['start_ms'] < min_hold_ms else new_end
                    action = 'to_tap' if before['end_ms'] is None else 'shortened'
                    counts[p_role][action] += 1
                    record(before, action, PRIMARY_INPUT_CONFLICT, {'event': copied, 'role': p_role}, role=p_role,
                           lane=lane_index, from_end_ms=old_end, to_end_ms=before['end_ms'])
            item = {'event': copied, 'role': p_role}
            lanes[lane_index].add(item)
            kept[p_role].append(copied)
            previous = item
    counts[p_role]['kept'] = len(kept[p_role])

    # Common-attack alignment: the two stems are separate model runs with their own time grids, so one
    # musical attack can appear as a primary head at t and a secondary head at t +- a few tens of ms.
    p_heads = sorted(((e['start_ms'], e['lane'], e) for e in kept[p_role]), key=lambda r: (r[0], r[1], str(r[2].get('id'))))
    plan = _plan_alignment(s_events, p_heads, window_ms=attack_window_ms, min_hold_ms=min_hold_ms,
                           runs=(silent_spans or {}).get(s_role))
    prepared = []
    for event in sorted(s_events, key=lambda e: (e['start_ms'], e['lane'], str(e.get('id')))):
        placed = copy.deepcopy(event)
        snap = plan.get(id(event))
        if snap:
            new, new_end, head, dt = snap
            old, old_end = placed['start_ms'], placed.get('end_ms')
            placed['start_ms'], placed['end_ms'] = new, new_end
            bump(s_role, 'aligned')
            origin = (placed.get('origins') or [None])[0]
            decisions.append({'event_id': placed.get('id'), 'role': s_role, 'action': 'aligned', 'reason': COMMON_ATTACK,
                              'origin': copy.deepcopy(origin), 'start_ms': old, 'lane': placed['lane'],
                              'from_ms': old, 'to_ms': new, 'dt_ms': dt, 'from_end_ms': old_end, 'to_end_ms': new_end,
                              'conflict': _ref({'event': head[2], 'role': p_role})})
        prepared.append((placed, bool(snap)))

    def at_instant(start):
        return [item for lane in lanes for item in lane.items[bisect.bisect_left(lane.starts, start - TOLERANCE_MS):
                                                               bisect.bisect_right(lane.starts, start + TOLERANCE_MS)]]

    def over_chord_cap(start):
        """True when joining this instant would exceed the chord cap; only instants holding a primary head count."""
        here = at_instant(start)
        return any(item['role'] == p_role for item in here) and len(here) + 1 > chord_cap

    def repeat_near(lane_index, start):
        lane = lanes[lane_index]
        index = bisect.bisect_left(lane.starts, start)
        if index and start - lane.starts[index - 1] < gap_ms * 2:
            return True
        after = bisect.bisect_right(lane.starts, start)
        return after < len(lane.starts) and lane.starts[after] - start < gap_ms * 2

    def free_lane(event):
        """Another lane with no conflict of any kind for the whole span."""
        start, end, origin_lane = event['start_ms'], event.get('end_ms'), event['lane']
        options = []
        for lane_index in range(4):
            if lane_index == origin_lane:
                continue
            found = {}
            lanes[lane_index].conflicts(start, end, found)
            if not found:
                options.append((repeat_near(lane_index, start), abs(lane_index - origin_lane), lane_index))
        return min(options)[2] if options else None

    for placed, snapped in sorted(prepared, key=lambda r: (r[0]['start_ms'], r[0]['lane'], str(r[0].get('id')))):
        start, end, lane_index = placed['start_ms'], placed.get('end_ms'), placed['lane']
        found = {}
        lanes[lane_index].conflicts(start, end, found)
        blocked = found.get('same_time') or found.get('inside_hold')
        over = over_chord_cap(start)
        if blocked:
            reason = 'same_time' if 'same_time' in found else 'inside_hold'
            target = free_lane(placed) if relane and not over else None
            if target is None:
                counts[s_role]['dropped'] += 1
                record(placed, 'dropped', SAME_ATTACK_DUPLICATE if snapped and reason == 'same_time' else reason, blocked,
                       lane=lane_index, end_ms=end)
                continue
            placed['lane'] = target
            counts[s_role]['relaned'] += 1
            record(placed, 'relaned', reason, blocked, from_lane=lane_index, to_lane=target, end_ms=end)
        elif over:
            counts[s_role]['dropped'] += 1
            record(placed, 'dropped', CHORD_CAP, next(i for i in at_instant(start) if i['role'] == p_role),
                   lane=lane_index, end_ms=end)
            continue
        elif 'covers_note' in found:
            conflict = found['covers_note']
            target = free_lane(placed) if relane else None
            if target is not None:
                placed['lane'] = target
                counts[s_role]['relaned'] += 1
                record(placed, 'relaned', 'covers_note', conflict, from_lane=lane_index, to_lane=target, end_ms=end)
            else:
                new_end = conflict['event']['start_ms'] - gap_ms
                if new_end - start < min_hold_ms:
                    placed['end_ms'] = None
                    counts[s_role]['to_tap'] += 1
                    record(placed, 'to_tap', 'covers_note', conflict, lane=lane_index, from_end_ms=end, to_end_ms=None)
                else:
                    placed['end_ms'] = new_end
                    counts[s_role]['shortened'] += 1
                    record(placed, 'shortened', 'covers_note', conflict, lane=lane_index, from_end_ms=end, to_end_ms=new_end)
        lanes[placed['lane']].add({'event': placed, 'role': s_role})
        kept[s_role].append(placed)
        counts[s_role]['kept'] += 1

    # Spacing invariant on the merged lanes, run to a fixed point: (a) a hold tail ends >= gap_ms before the next head of
    # its lane, (b) a head is >= gap_ms before the next head of its lane. Primary holds survive: the secondary note that
    # follows is relaned first (relane mode); a hold is shortened only when that is impossible, and never drops a head.
    removed = set()

    def moved(item):
        event, original = item['event'], source[(item['role'], item['event'].get('id'))]
        return event['lane'] != original['lane'] or event['start_ms'] != original['start_ms']

    def violation(a, b):
        ea, eb = a['event'], b['event']
        if eb['start_ms'] - ea['start_ms'] < gap_ms - EPS:
            if not _residual(a, b, source, gap_ms):
                return 'head'
            # the model's own close pair cannot be repaired, but a hold in it still becomes a tap
            return 'tail' if ea.get('end_ms') is not None else None
        if ea.get('end_ms') is not None and eb['start_ms'] - ea['end_ms'] < gap_ms - EPS:
            return 'tail'
        return None

    def move_lane(item, reason, conflict):
        """Move a secondary note to another lane where its whole span fits (same lane preference as relane)."""
        event, start, end, origin_lane = item['event'], item['event']['start_ms'], item['event'].get('end_ms'), item['event']['lane']
        options = [(repeat_near(lane_index, start), abs(lane_index - origin_lane), lane_index) for lane_index in range(4)
                   if lane_index != origin_lane and lanes[lane_index].room(start, end, gap_ms)]
        if not options:
            return False
        target = min(options)[2]
        lanes[origin_lane].remove(item)
        event['lane'] = target
        lanes[target].add(item)
        bump(s_role, 'tail_spacing_relane' if reason == TAIL_SPACING_RELANE else 'lane_gap_relane')
        record(event, 'relaned', reason, conflict, from_lane=origin_lane, to_lane=target, end_ms=end)
        return True

    def drop_lane_gap(item, conflict):
        event = item['event']
        lanes[event['lane']].remove(item)
        removed.add(id(event))
        counts[s_role]['kept'] -= 1
        counts[s_role]['dropped'] += 1
        bump(s_role, 'lane_gap_dropped')
        record(event, 'dropped', LANE_GAP, conflict, lane=event['lane'], end_ms=event.get('end_ms'))

    def shorten(item, following):
        event, end = item['event'], item['event']['end_ms']
        new_end = following['event']['start_ms'] - gap_ms
        event['end_ms'] = None if new_end - event['start_ms'] < min_hold_ms else new_end
        action = 'to_tap' if event['end_ms'] is None else 'shortened'
        bump(item['role'], TAIL_SPACING)
        record(event, action, TAIL_SPACING, following, role=item['role'], lane=event['lane'], from_end_ms=end,
               to_end_ms=event['end_ms'], same_stem=item['role'] == following['role'])

    def fix(kind, a, b):
        if kind == 'tail':
            if relane and a['role'] != b['role']:
                mover, other = (b, a) if b['role'] == s_role else (a, b)
                if move_lane(mover, TAIL_SPACING_RELANE, other):
                    return
            shorten(a, b)
            return
        if a['role'] != b['role']:
            target, other = (a, b) if a['role'] == s_role else (b, a)
        else:  # same stem, created by this merge: the later moved note yields, else the earlier moved one
            target, other = next(((x, y) for x, y in ((b, a), (a, b)) if x['role'] == s_role and moved(x)), (None, None))
        if target is None:
            raise ValueError('声部融合无法修复同轨间距')
        if not (relane and move_lane(target, LANE_GAP, other)):
            drop_lane_gap(target, other)

    for _ in range(MAX_SWEEPS):
        todo = []
        for lane_index, lane in enumerate(lanes):
            for a, b in zip(lane.items, lane.items[1:]):
                if violation(a, b):
                    todo.append((b['event']['start_ms'], lane_index, str(b['event'].get('id')), a, b))
        if not todo:
            break
        todo.sort(key=lambda row: row[:3])
        for _start, lane_index, _id, a, b in todo:
            lane = lanes[lane_index]
            index = lane.index(a)
            if index is None or index + 1 >= len(lane.items) or lane.items[index + 1] is not b:
                continue  # an earlier fix of this sweep changed the neighbourhood; the next sweep reads it again
            kind = violation(a, b)
            if kind:
                fix(kind, a, b)
    else:
        raise ValueError('声部融合同轨间距未能收敛')
    for role in ROLES:
        kept[role] = [e for e in kept[role] if id(e) not in removed]
    residual = {role: 0 for role in ROLES}
    for lane in lanes:
        for a, b in zip(lane.items, lane.items[1:]):
            if b['event']['start_ms'] - a['event']['start_ms'] < gap_ms - EPS and _residual(a, b, source, gap_ms):
                residual[a['role']] += 1

    events = sorted(kept['vocals'] + kept['accompaniment'], key=lambda e: (e['start_ms'], e['lane'], str(e.get('id'))))
    return {'events': events, 'decisions': decisions, 'counts': counts, 'primary_role': p_role, 'secondary_role': s_role,
            'mode': mode, 'gap_ms': gap_ms, 'min_hold_ms': min_hold_ms, 'attack_window_ms': attack_window_ms,
            'chord_cap': chord_cap, 'residual_same_stem_lane_gap': sum(residual.values()),
            'residual_same_stem_lane_gap_by_role': residual,
            **({'silent_spans': copy.deepcopy(silent_spans)} if silent_spans is not None else {})}


_RELANE_COUNTERS = {TAIL_SPACING_RELANE: 'tail_spacing_relane', LANE_GAP: 'lane_gap_relane'}


def audit(vocals, accompaniment, result):
    """Exact accounting: kept + dropped == inputs; only decided notes differ.

    Every kept note is replayed from its input through its recorded decisions (in order): a head time may differ
    only through a recorded ``aligned`` secondary note (at most ``attack_window_ms``), a lane only through recorded
    ``relaned`` steps, a tail only through recorded shortened / to_tap / aligned-to-tap steps. The merged lanes must
    keep a hold tail and a head ``gap_ms`` before the next head of their lane, except the counted same-stem pairs
    the model itself wrote that close (``residual_same_stem_lane_gap``)."""
    by_role = {'vocals': vocals, 'accompaniment': accompaniment}
    window = result.get('attack_window_ms', 0.)
    primary = result['primary_role']
    steps = {}
    for row in result['decisions']:
        steps.setdefault((row['role'], row['event_id']), []).append(row)
    source = {role: {e.get('id'): e for e in by_role[role]} for role in ROLES}
    kept_ids = {role: set() for role in ROLES}
    for event in result['events']:
        role = (event.get('origins') or [{}])[0].get('stem_role')
        if role not in ROLES or event.get('id') not in source[role]:
            raise ValueError('融合对账失败：保留音符没有可核实的声部来源')
        if event['id'] in kept_ids[role]:
            raise ValueError('融合对账失败：保留音符重复')
        kept_ids[role].add(event['id'])
        original = source[role][event['id']]
        state = {'start': original['start_ms'], 'lane': original['lane'], 'end': original.get('end_ms')}
        for row in steps.get((role, event['id']), []):
            action = row['action']
            if action == 'dropped':
                raise ValueError('融合对账失败：已舍弃音符仍在谱面中')
            if action == 'aligned':
                if (role == primary or row['from_ms'] != state['start'] or not TOLERANCE_MS < abs(row['to_ms'] - row['from_ms']) <= window
                        or row.get('from_end_ms') != state['end']):
                    raise ValueError('融合对账失败：音符头时间被改变')
                state['start'], state['end'] = row['to_ms'], row.get('to_end_ms')
            elif action == 'relaned':
                if row['from_lane'] != state['lane'] or row['to_lane'] == row['from_lane']:
                    raise ValueError('融合对账失败：挪轨记录与实际轨道不符')
                state['lane'] = row['to_lane']
            elif action in ('shortened', 'to_tap'):
                if row.get('from_end_ms') != state['end'] or (row.get('to_end_ms') is None) != (action == 'to_tap'):
                    raise ValueError('融合对账失败：长条尾与记录不符')
                state['end'] = row.get('to_end_ms')
        if event['start_ms'] != state['start']:
            raise ValueError('融合对账失败：音符头时间被改变')
        if inside_silent_run(event['start_ms'], (result.get('silent_spans') or {}).get(role)):
            raise ValueError('融合对账失败：静音段内的音符仍在谱面中')
        if event['lane'] != state['lane']:
            raise ValueError('融合对账失败：轨道在无记录时被改变')
        if event.get('end_ms') != state['end']:
            raise ValueError('融合对账失败：长条尾在无记录时被改变')
    counts = result['counts']
    decided = result['decisions']
    for role in ROLES:
        dropped_rows = [row['event_id'] for row in decided if row['role'] == role and row['action'] == 'dropped']
        dropped = set(dropped_rows)
        if len(dropped) != len(dropped_rows):
            raise ValueError('融合对账失败：同一音符被多次舍弃')
        if dropped & kept_ids[role]:
            raise ValueError('融合对账失败：舍弃与保留重叠')
        if len(kept_ids[role]) + len(dropped) != len(by_role[role]) or len(source[role]) != len(by_role[role]):
            raise ValueError('融合对账失败：保留数加舍弃数不等于输入数')
        row = counts[role]
        if row['input'] != len(by_role[role]) or row['kept'] != len(kept_ids[role]) or row['dropped'] != len(dropped):
            raise ValueError('融合对账失败：统计数与实际不符')
        if row.get('stem_silent', 0) != sum(1 for d in decided if d['role'] == role and d['reason'] == STEM_SILENT_SPAN):
            raise ValueError('融合对账失败：静音段舍弃统计不符')
        special = (TAIL_SPACING, TAIL_SPACING_RELANE, LANE_GAP)
        for name in ('relaned', 'shortened', 'to_tap'):
            if row[name] != sum(1 for d in decided if d['role'] == role and d['action'] == name and d['reason'] not in special):
                raise ValueError('融合对账失败：决策统计不符')
        if row.get('aligned', 0) != sum(1 for d in decided if d['role'] == role and d['action'] == 'aligned'):
            raise ValueError('融合对账失败：对齐统计不符')
        if row.get('tail_spacing', 0) != sum(1 for d in decided if d['role'] == role and d['reason'] == TAIL_SPACING):
            raise ValueError('融合对账失败：长条尾间距统计不符')
        for reason, key in _RELANE_COUNTERS.items():
            if row.get(key, 0) != sum(1 for d in decided if d['role'] == role and d['action'] == 'relaned' and d['reason'] == reason):
                raise ValueError('融合对账失败：同轨间距挪轨统计不符')
        if row.get('lane_gap_dropped', 0) != sum(1 for d in decided if d['role'] == role and d['action'] == 'dropped' and d['reason'] == LANE_GAP):
            raise ValueError('融合对账失败：同轨间距舍弃统计不符')
    allowed = (PRIMARY_INPUT_CONFLICT, STEM_SILENT_SPAN, TAIL_SPACING)
    if any(d['role'] == primary and d['reason'] not in allowed for d in decided):
        raise ValueError('融合对账失败：主方音符被改动')
    # Spacing invariant of the merged lanes.
    gap = result['gap_ms']
    lanes = {lane: [] for lane in range(4)}
    for event in sorted(result['events'], key=lambda e: (e['start_ms'], str(e.get('id')))):
        lanes[event['lane']].append({'event': event, 'role': event['origins'][0]['stem_role']})
    source_by_key = {(role, event_id): row for role in ROLES for event_id, row in source[role].items()}
    residual = {role: 0 for role in ROLES}
    for items in lanes.values():
        for a, b in zip(items, items[1:]):
            ea, eb = a['event'], b['event']
            if ea.get('end_ms') is not None and eb['start_ms'] - ea['end_ms'] < gap - EPS:
                raise ValueError('融合对账失败：长条尾与同轨下一个音符头间距不足')
            if eb['start_ms'] - ea['start_ms'] < gap - EPS:
                if not _residual(a, b, source_by_key, gap):
                    raise ValueError('融合对账失败：同轨音符头间距不足')
                residual[a['role']] += 1
    if (result.get('residual_same_stem_lane_gap') != sum(residual.values())
            or result.get('residual_same_stem_lane_gap_by_role') != residual):
        raise ValueError('融合对账失败：模型自身同轨过近统计不符')
    return True
