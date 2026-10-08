import copy
import itertools
import random

import pytest

from malody_studio import stem_merge
from malody_studio.advanced import valid_events

GAP = 60.
MIN_HOLD = 120.
END = 100000.


def ev(role, name, start, lane, end=None):
    return {'id': f'{role[0]}-{name}', 'start_ms': float(start), 'lane': lane, 'end_ms': None if end is None else float(end),
            'origins': [{'revision_id': role, 'note_id': name, 'stem_role': role, 'original_start_ms': float(start),
                         'original_lane': lane, 'original_end_ms': None if end is None else float(end)}]}


def random_stem(role, rng, count, hold_chance=.3, step=5, span=4000):
    """A stem that is internally valid, like a real raw stem chart."""
    rows, taken = [], {lane: [] for lane in range(4)}
    for i in range(count):
        start = rng.randrange(0, span) * step
        lane = rng.randrange(4)
        end = start + rng.choice([200, 450, 1200]) if rng.random() < hold_chance else None
        if any(not (max(start, s0) > min(end or start, e0) + .5) or abs(start - s0) <= .5 for s0, e0 in taken[lane]):
            continue
        taken[lane].append((start, end or start))
        rows.append(ev(role, str(i), start, lane, end))
    return rows


def run(vocals, accompaniment, mode, primary='vocals', gap=GAP, **options):
    options.setdefault('min_hold_ms', MIN_HOLD)
    result = stem_merge.merge_stems(vocals, accompaniment, mode=mode, primary=primary, gap_ms=gap, **options)
    stem_merge.audit(vocals, accompaniment, result)
    valid_events(result['events'], 0, END, END + 1000)
    return result


def lane_of(result, event_id):
    return next(e['lane'] for e in result['events'] if e['id'] == event_id)


def event(result, event_id):
    return next((e for e in result['events'] if e['id'] == event_id), None)


def decisions(result, action=None):
    return [d for d in result['decisions'] if action is None or d['action'] == action]


def test_modes_and_priority_sides():
    assert stem_merge.priority_side('vocals_priority', 'accompaniment') == ('vocals', 'accompaniment')
    assert stem_merge.priority_side('accompaniment_priority', 'vocals') == ('accompaniment', 'vocals')
    assert stem_merge.priority_side('relane', 'accompaniment') == ('accompaniment', 'vocals')
    assert stem_merge.priority_side('relane', 'vocals') == ('vocals', 'accompaniment')
    with pytest.raises(ValueError):
        stem_merge.priority_side('union', 'vocals')
    with pytest.raises(ValueError):
        stem_merge.priority_side('relane', 'mix')


def test_non_conflicting_notes_pass_through_in_every_mode():
    v = [ev('vocals', 'a', 100, 0), ev('vocals', 'b', 400, 1, 900)]
    a = [ev('accompaniment', 'a', 100, 1), ev('accompaniment', 'b', 1000, 1, 1500)]
    for mode in stem_merge.MODES:
        result = run(v, a, mode)
        assert len(result['events']) == 4 and result['decisions'] == []
        assert result['counts']['vocals']['kept'] == 2 and result['counts']['accompaniment']['kept'] == 2


@pytest.mark.parametrize('mode,primary_role,secondary_role', [('vocals_priority', 'vocals', 'accompaniment'),
                                                                 ('accompaniment_priority', 'accompaniment', 'vocals')])
def test_priority_modes_drop_same_time_same_lane_secondary_note(mode, primary_role, secondary_role):
    stems = {'vocals': [ev('vocals', 'x', 500, 2)], 'accompaniment': [ev('accompaniment', 'x', 500, 2)]}
    result = run(stems['vocals'], stems['accompaniment'], mode)
    assert [e['id'] for e in result['events']] == [stems[primary_role][0]['id']]
    (decision,) = result['decisions']
    assert (decision['role'], decision['action'], decision['reason']) == (secondary_role, 'dropped', 'same_time')
    assert decision['conflict']['event_id'] == stems[primary_role][0]['id']
    assert result['counts'][secondary_role] == {'input': 1, 'kept': 0, 'dropped': 1, 'relaned': 0, 'shortened': 0, 'to_tap': 0}


def test_tolerance_is_one_tenth_of_a_millisecond():
    v = [ev('vocals', 'a', 500, 0)]
    near = run(v, [ev('accompaniment', 'a', 500.1, 0)], 'vocals_priority')
    assert len(near['events']) == 1
    apart = run(v, [ev('accompaniment', 'a', 500.2, 0)], 'vocals_priority', attack_window_ms=0.)  # alignment off: 0.2 ms is a different time
    assert len(apart['events']) == 1  # ... which is not the same time, but far inside the lane gap
    (row,) = apart['decisions']
    assert (row['action'], row['reason']) == ('dropped', 'lane_gap')
    assert len(run(v, [ev('accompaniment', 'a', 500.2, 1)], 'vocals_priority', attack_window_ms=0.)['events']) == 2


def test_secondary_note_starting_inside_primary_hold_is_dropped_never_the_hold():
    v = [ev('vocals', 'hold', 1000, 1, 2000)]
    a = [ev('accompaniment', 'tap', 1500, 1), ev('accompaniment', 'edge', 2000, 1)]
    result = run(v, a, 'vocals_priority')
    assert {d['event_id']: d['reason'] for d in decisions(result, 'dropped')} == {'a-tap': 'inside_hold'}
    assert event(result, 'a-edge') is not None  # starting exactly at the hold tail is legal for the placement rules
    assert event(result, 'v-hold')['end_ms'] == 2000. - GAP  # ... but the tail pass then spaces it (recorded, primary side)


def test_secondary_hold_covering_primary_note_is_shortened_keeping_head():
    v = [ev('vocals', 'tap', 1000, 2)]
    a = [ev('accompaniment', 'hold', 200, 2, 1500)]
    result = run(v, a, 'vocals_priority')
    shortened = event(result, 'a-hold')
    assert shortened['start_ms'] == 200. and shortened['end_ms'] == 1000. - GAP
    (decision,) = result['decisions']
    assert decision['action'] == 'shortened' and decision['reason'] == 'covers_note'
    assert (decision['from_end_ms'], decision['to_end_ms']) == (1500., 940.)
    assert decision['conflict']['event_id'] == 'v-tap' and result['counts']['accompaniment']['shortened'] == 1


def test_shortened_to_first_primary_note_inside_span():
    v = [ev('vocals', 'late', 1200, 0), ev('vocals', 'early', 700, 0)]
    a = [ev('accompaniment', 'hold', 100, 0, 2000)]
    result = run(v, a, 'vocals_priority')
    assert event(result, 'a-hold')['end_ms'] == 700. - GAP


def test_hold_becomes_tap_when_remaining_length_is_below_minimum():
    v = [ev('vocals', 'tap', 300, 3)]
    a = [ev('accompaniment', 'hold', 200, 3, 900)]  # would be 40 ms long
    result = run(v, a, 'vocals_priority')
    tap = event(result, 'a-hold')
    assert tap['start_ms'] == 200. and tap['end_ms'] is None
    (decision,) = result['decisions']
    assert decision['action'] == 'to_tap' and decision['to_end_ms'] is None and decision['from_end_ms'] == 900.
    assert result['counts']['accompaniment']['to_tap'] == 1 and result['counts']['accompaniment']['shortened'] == 0
    exact = run([ev('vocals', 'tap', 380, 3)], a, 'vocals_priority')
    assert event(exact, 'a-hold')['end_ms'] == 320.  # exactly min_hold_ms is kept as a hold


def test_primary_notes_are_never_changed_even_when_secondary_is_dense():
    rng = random.Random(7)
    v = random_stem('vocals', rng, 300, span=300)
    a = random_stem('accompaniment', rng, 800, span=300)
    for mode, primary in itertools.product(stem_merge.MODES, stem_merge.PRIMARIES):
        before = copy.deepcopy((v, a))
        result = run(v, a, mode, primary)
        assert (v, a) == before  # inputs are not mutated
        p_role = result['primary_role']
        source = {'vocals': v, 'accompaniment': a}[p_role]
        kept = [e for e in result['events'] if e['origins'][0]['stem_role'] == p_role]
        tail = {d['event_id']: d for d in result['decisions'] if d['role'] == p_role}
        assert all(d['reason'] == 'tail_spacing' for d in tail.values())  # the only change to the primary side
        for e, o in zip(sorted(kept, key=lambda e: e['id']), sorted(source, key=lambda e: e['id'])):
            assert (e['id'], e['start_ms'], e['lane']) == (o['id'], o['start_ms'], o['lane'])
            assert e.get('end_ms') == o.get('end_ms') or e['id'] in tail


def test_relane_moves_conflicting_note_to_nearest_free_lane_at_same_time():
    v = [ev('vocals', 'a', 500, 1)]
    a = [ev('accompaniment', 'a', 500, 1)]
    result = run(v, a, 'relane')
    moved = event(result, 'a-a')
    assert moved['start_ms'] == 500. and moved['lane'] == 0  # nearest lane, lower index on tie
    (decision,) = result['decisions']
    assert (decision['action'], decision['from_lane'], decision['to_lane']) == ('relaned', 1, 0)
    assert result['counts']['accompaniment']['relaned'] == 1 and result['counts']['accompaniment']['kept'] == 1
    assert moved['origins'][0]['original_lane'] == 1


def test_relane_uses_only_lanes_free_for_the_whole_span():
    v = [ev('vocals', 'p0', 500, 0), ev('vocals', 'p2', 700, 2), ev('vocals', 'p1', 500, 1)]
    a = [ev('accompaniment', 'h', 500, 0, 1000)]  # lane 1 same time, lane 2 has a note in span -> lane 3 only
    result = run(v, a, 'relane')
    assert lane_of(result, 'a-h') == 3 and event(result, 'a-h')['end_ms'] == 1000.


def test_relane_prefers_lane_without_near_same_lane_repeat_over_nearest_lane():
    # Lane 0 is nearest to the conflicted lane 1 but has a note 100 ms later (< 2*gap = 120).
    v = [ev('vocals', 'p1', 500, 1), ev('vocals', 'near', 600, 0)]
    a = [ev('accompaniment', 'a', 500, 1)]
    result = run(v, a, 'relane')
    assert lane_of(result, 'a-a') == 2
    # A preceding note within 2*gap counts as well.
    v = [ev('vocals', 'p1', 500, 1), ev('vocals', 'before', 400, 0)]
    assert lane_of(run(v, a, 'relane'), 'a-a') == 2
    # Far enough (>= 2*gap) is not a repeat: nearest lane wins again.
    v = [ev('vocals', 'p1', 500, 1), ev('vocals', 'before', 380, 0)]
    assert lane_of(run(v, a, 'relane'), 'a-a') == 0


def test_relane_falls_back_to_nearest_when_every_free_lane_creates_a_repeat():
    v = [ev('vocals', 'p', 500, 1)] + [ev('vocals', f'n{lane}', 570, lane) for lane in (0, 2, 3)]
    result = run(v, [ev('accompaniment', 'a', 500, 1)], 'relane')
    assert result['decisions'] == [] or result['decisions'][0]['action'] != 'dropped'
    assert lane_of(result, 'a-a') in (0, 2)  # nearest wins (lane 0 on tie)
    assert lane_of(result, 'a-a') == 0


def test_relane_drops_when_all_lanes_conflict():
    v = [ev('vocals', f'p{lane}', 500, lane) for lane in range(4)]
    result = run(v, [ev('accompaniment', 'a', 500, 2)], 'relane')
    assert event(result, 'a-a') is None
    (decision,) = result['decisions']
    assert decision['action'] == 'dropped' and decision['reason'] == 'same_time'
    assert result['counts']['accompaniment']['dropped'] == 1


def test_relane_start_inside_hold_moves_or_drops():
    v = [ev('vocals', 'h', 1000, 0, 2000), ev('vocals', 'h1', 1000, 1, 2000), ev('vocals', 'h2', 1000, 2, 2000)]
    moved = run(v, [ev('accompaniment', 't', 1500, 0)], 'relane')
    assert lane_of(moved, 'a-t') == 3
    assert moved['decisions'][0]['reason'] == 'inside_hold'
    v.append(ev('vocals', 'h3', 1000, 3, 2000))
    dropped = run(v, [ev('accompaniment', 't', 1500, 0)], 'relane')
    assert event(dropped, 'a-t') is None and dropped['decisions'][0]['action'] == 'dropped'


def test_relane_hold_covering_note_prefers_free_lane_then_shortens_never_drops():
    v = [ev('vocals', 'tap', 1000, 1)]
    a = [ev('accompaniment', 'hold', 200, 1, 1500)]
    moved = run(v, a, 'relane')
    assert lane_of(moved, 'a-hold') == 0 and event(moved, 'a-hold')['end_ms'] == 1500.
    assert moved['decisions'][0]['action'] == 'relaned' and moved['decisions'][0]['reason'] == 'covers_note'
    # Every other lane has a note inside the hold span: shorten, keep head.
    v = [ev('vocals', f'p{lane}', 1000, lane) for lane in range(4)]
    kept = run(v, a, 'relane')
    assert event(kept, 'a-hold')['lane'] == 1 and event(kept, 'a-hold')['end_ms'] == 940.
    assert kept['decisions'][0]['action'] == 'shortened'
    short = [ev('vocals', f'p{lane}', 300, lane) for lane in range(4)]
    tap = run(short, a, 'relane')
    assert event(tap, 'a-hold')['end_ms'] is None and tap['decisions'][0]['action'] == 'to_tap'


def test_relane_primary_selects_the_untouched_side():
    v = [ev('vocals', 'a', 500, 1)]
    a = [ev('accompaniment', 'a', 500, 1)]
    by_vocals = run(v, a, 'relane', 'vocals')
    assert lane_of(by_vocals, 'v-a') == 1 and lane_of(by_vocals, 'a-a') == 0
    by_accompaniment = run(v, a, 'relane', 'accompaniment')
    assert lane_of(by_accompaniment, 'a-a') == 1 and lane_of(by_accompaniment, 'v-a') == 0
    assert by_accompaniment['decisions'][0]['role'] == 'vocals'


def test_secondary_notes_collide_with_previously_placed_secondary_notes():
    a = [ev('accompaniment', 'first', 500, 1, 1500), ev('accompaniment', 'inside', 900, 1), ev('accompaniment', 'twin', 500, 1)]
    result = run([], a, 'vocals_priority')
    # 'first' sorts before 'twin' (same time and lane, by id); both later notes collide with placed notes.
    assert {d['event_id'] for d in decisions(result, 'dropped')} == {'a-inside', 'a-twin'}
    assert [e['id'] for e in result['events']] == ['a-first']
    relaned = run([], a, 'relane')
    assert len(relaned['events']) == 3 and decisions(relaned, 'dropped') == []
    assert {d['event_id'] for d in decisions(relaned, 'relaned')} == {'a-inside', 'a-twin'}


def test_tie_order_is_by_time_then_lane_and_deterministic():
    v = [ev('vocals', 'p', 500, 1), ev('vocals', 'q', 500, 2)]
    a = [ev('accompaniment', 'z', 500, 2), ev('accompaniment', 'y', 500, 1)]
    first = run(v, a, 'relane')
    # lane-1 note is processed before lane-2: it takes lane 0 (nearest), lane-2 note then takes lane 3.
    assert lane_of(first, 'a-y') == 0 and lane_of(first, 'a-z') == 3
    for _ in range(3):
        assert run(copy.deepcopy(v), list(reversed(copy.deepcopy(a))), 'relane') == first
        assert run(list(reversed(v)), a, 'relane')['events'] == first['events']


def test_empty_stems():
    v = [ev('vocals', 'a', 500, 1), ev('vocals', 'b', 500, 2)]
    a = [ev('accompaniment', 'a', 500, 1), ev('accompaniment', 'b', 500, 2)]
    for mode in stem_merge.MODES:
        for primary in stem_merge.PRIMARIES:
            only_a = run([], a, mode, primary)
            only_v = run(v, [], mode, primary)
            assert only_a['counts']['vocals']['input'] == 0 and only_v['counts']['accompaniment']['input'] == 0
            assert len(only_a['events']) >= 1 and len(only_v['events']) >= 1
    # Both empty is allowed here; the caller keeps today's "both empty" error.
    assert run([], [], 'relane')['events'] == []


def test_invalid_input_is_rejected():
    for bad in ({'id': 'x', 'start_ms': 1., 'lane': 4}, {'id': 'x', 'start_ms': 'a', 'lane': 1},
                {'id': 'x', 'start_ms': 5., 'lane': 1, 'end_ms': 5.}):
        with pytest.raises((ValueError, TypeError)):
            stem_merge.merge_stems([bad], [], mode='relane', gap_ms=GAP)


def test_audit_detects_silent_changes_and_missing_accounting():
    v = [ev('vocals', 'a', 500, 1)]
    a = [ev('accompaniment', 'a', 500, 1), ev('accompaniment', 'b', 900, 2, 1500)]
    result = stem_merge.merge_stems(v, a, mode='vocals_priority', gap_ms=GAP, min_hold_ms=MIN_HOLD)
    stem_merge.audit(v, a, result)
    for mutate in (lambda r: r['events'][-1].__setitem__('lane', 0),
                   lambda r: r['events'][-1].__setitem__('end_ms', 1400.),
                   lambda r: r['events'][-1].__setitem__('start_ms', 901.),
                   lambda r: r['events'].pop(),
                   lambda r: r['decisions'].clear(),
                   lambda r: r['counts']['accompaniment'].__setitem__('kept', 5)):
        broken = copy.deepcopy(result)
        mutate(broken)
        with pytest.raises(ValueError):
            stem_merge.audit(v, a, broken)


def test_randomised_merges_always_satisfy_structure_and_accounting():
    for seed in range(40):
        rng = random.Random(seed)
        vocals = random_stem('vocals', rng, rng.randrange(0, 160))
        accompaniment = random_stem('accompaniment', rng, rng.randrange(0, 160))
        for mode, primary in itertools.product(stem_merge.MODES, stem_merge.PRIMARIES):
            gap = rng.choice([35., 45., 55., 60.])
            result = stem_merge.merge_stems(vocals, accompaniment, mode=mode, primary=primary, gap_ms=gap, min_hold_ms=MIN_HOLD)
            stem_merge.audit(vocals, accompaniment, result)
            valid_events(result['events'], 0, END, END + 1000)
            for d in result['decisions']:
                if d['action'] == 'shortened':
                    assert d['to_end_ms'] - d['start_ms'] >= MIN_HOLD
                if d['action'] == 'dropped' and mode == 'relane':
                    assert d['reason'] in ('same_time', 'inside_hold', 'same_attack_duplicate', 'chord_cap', 'lane_gap')
            if mode == 'relane':
                priority = stem_merge.priority_side('vocals_priority' if primary == 'vocals' else 'accompaniment_priority', primary)
                plain = stem_merge.merge_stems(vocals, accompaniment, mode='vocals_priority' if primary == 'vocals' else 'accompaniment_priority',
                                               gap_ms=gap, min_hold_ms=MIN_HOLD)
                assert (result['counts'][priority[1]]['dropped'] <= plain['counts'][priority[1]]['dropped'] + result['counts'][priority[1]].get('aligned', 0)
                        + result['counts'][priority[1]].get('lane_gap_dropped', 0))


def test_primary_stem_own_collisions_are_repaired_minimally_and_recorded():
    # A hold running across a generation-window seam into the next note of the same stem.
    v = [ev('vocals', 'seam', 1000, 2, 1400), ev('vocals', 'next', 1300, 2), ev('vocals', 'twin', 2000, 1), ev('vocals', 'twin2', 2000.05, 1)]
    a = [ev('accompaniment', 'x', 3000, 0)]
    for mode in ('vocals_priority', 'relane'):  # vocals are the primary side
        result = run(v, a, mode)
        seam = event(result, 'v-seam')
        assert seam['start_ms'] == 1000. and seam['end_ms'] == 1300. - GAP and event(result, 'v-next')['end_ms'] is None
        assert event(result, 'v-twin') is not None and event(result, 'v-twin2') is None
        rows = {d['event_id']: d for d in result['decisions']}
        assert set(rows) == {'v-seam', 'v-twin2'}
        assert all(d['reason'] == stem_merge.PRIMARY_INPUT_CONFLICT and d['role'] == 'vocals' for d in rows.values())
        assert rows['v-seam']['action'] == 'shortened' and rows['v-twin2']['action'] == 'dropped'
    tap = run([ev('vocals', 'seam', 1000, 2, 1400), ev('vocals', 'next', 1100, 2)], a, 'relane')
    assert event(tap, 'v-seam')['end_ms'] is None and tap['decisions'][0]['action'] == 'to_tap'


# --------------------------------------------------------------------------------------------
# Common-attack alignment (policy v2) and tail spacing
# --------------------------------------------------------------------------------------------
SIDES = {'vocals_priority': ('vocals', 'accompaniment'), 'accompaniment_priority': ('accompaniment', 'vocals'),
         'relane': ('vocals', 'accompaniment')}
MODES_ = list(SIDES)


def pair(mode, p_notes, s_notes):
    """p_notes / s_notes: tuples (name, start, lane[, end]) placed on the primary / secondary side of the mode."""
    p_role, s_role = SIDES[mode]
    p = [ev(p_role, *n) for n in p_notes]
    s = [ev(s_role, *n) for n in s_notes]
    vocals, accompaniment = (p, s) if p_role == 'vocals' else (s, p)
    return vocals, accompaniment, p_role, s_role


def go(mode, p_notes, s_notes, **options):
    vocals, accompaniment, p_role, s_role = pair(mode, p_notes, s_notes)
    result = run(vocals, accompaniment, mode, 'vocals', **options)
    return result, p_role, s_role


def sid(role, name):
    return f'{role[0]}-{name}'


def test_version_and_options_are_recorded():
    assert stem_merge.VERSION == 'direct-priority-merge-v3'
    assert stem_merge.ATTACK_WINDOW_MS == 40.
    result = stem_merge.merge_stems([], [ev('accompaniment', 'a', 5, 0)], mode='relane', gap_ms=GAP, min_hold_ms=MIN_HOLD)
    assert result['attack_window_ms'] == 40. and result['chord_cap'] == 4


@pytest.mark.parametrize('mode', MODES_)
@pytest.mark.parametrize('offset', [30., -30., 40., -40., 10., 0.5])
def test_secondary_note_within_window_is_snapped_to_the_primary_head(mode, offset):
    result, p_role, s_role = go(mode, [('p', 1000, 0)], [('s', 1000 + offset, 2)])
    snapped = event(result, sid(s_role, 's'))
    assert snapped['start_ms'] == 1000. and snapped['lane'] == 2
    assert snapped['origins'][0]['original_start_ms'] == 1000. + offset  # origin keeps the original time
    (row,) = result['decisions']
    assert row['action'] == 'aligned' and row['role'] == s_role and row['from_ms'] == 1000. + offset and row['to_ms'] == 1000.
    assert row['dt_ms'] == pytest.approx(abs(offset)) and row['conflict']['event_id'] == sid(p_role, 'p')
    assert result['counts'][s_role]['aligned'] == 1 and 'aligned' not in result['counts'][p_role]
    assert event(result, sid(p_role, 'p'))['start_ms'] == 1000.


@pytest.mark.parametrize('mode', MODES_)
def test_no_snap_outside_the_window_and_exact_matches_are_not_recorded(mode):
    result, _, s_role = go(mode, [('p', 1000, 0)], [('s', 1040.5, 2)])
    assert event(result, sid(s_role, 's'))['start_ms'] == 1040.5 and result['decisions'] == []
    result, _, s_role = go(mode, [('p', 1000, 0)], [('s', 1000.05, 2)])  # inside the 0.1 ms tolerance: already the same time
    assert event(result, sid(s_role, 's'))['start_ms'] == 1000.05 and result['decisions'] == []
    result, _, s_role = go(mode, [('p', 1000, 0)], [('s', 1030, 2)], attack_window_ms=20.)
    assert event(result, sid(s_role, 's'))['start_ms'] == 1030. and result['decisions'] == []
    result, _, s_role = go(mode, [('p', 1000, 0)], [('s', 1030, 2)], attack_window_ms=0.)
    assert result['decisions'] == []


@pytest.mark.parametrize('mode', MODES_)
def test_secondary_own_close_neighbour_protects_the_stream_from_folding(mode):
    # S plays 1030 and 1050 itself (a 20 ms rhythm): 45 % of 20 ms is 9 ms, the 30 ms offset must not fold it.
    result, _, s_role = go(mode, [('p', 1000, 0)], [('a', 1030, 1), ('b', 1050, 2)])
    assert [event(result, sid(s_role, n))['start_ms'] for n in 'ab'] == [1030., 1050.] and result['decisions'] == []
    # A neighbour exactly at the snap target on the S side also blocks (it would collapse onto it).
    result, _, s_role = go(mode, [('p', 1000, 0)], [('a', 1030, 1), ('b', 1000.0, 2)])
    assert event(result, sid(s_role, 'a'))['start_ms'] == 1030.


@pytest.mark.parametrize('mode', MODES_)
def test_window_is_capped_at_45_percent_of_the_nearest_own_interval(mode):
    s_notes = [('s', 1050, 1), ('n', 1150, 2)]  # nearest own neighbour 100 ms away -> cap 45 ms
    result, _, s_role = go(mode, [('p', 1000, 0)], s_notes, attack_window_ms=60.)
    assert event(result, sid(s_role, 's'))['start_ms'] == 1050. and result['decisions'] == []
    result, _, s_role = go(mode, [('p', 1000, 0)], [('s', 1044, 1), ('n', 1150, 2)], attack_window_ms=60.)
    assert event(result, sid(s_role, 's'))['start_ms'] == 1000.  # 44 <= 0.45 * 106
    result, _, s_role = go(mode, [('p', 1000, 0)], [('s', 1046, 1), ('n', 1146, 2)], attack_window_ms=60.)
    assert event(result, sid(s_role, 's'))['start_ms'] == 1046.  # 46 > 0.45 * 100
    # the earlier neighbour counts as well
    result, _, s_role = go(mode, [('p', 1000, 0)], [('n', 950, 2), ('s', 1045, 1)], attack_window_ms=60.)
    assert event(result, sid(s_role, 's'))['start_ms'] == 1045.  # earlier neighbour 95 ms away -> cap 42.75 < 45


@pytest.mark.parametrize('mode', MODES_)
def test_snap_is_blocked_by_the_own_stem_tail_it_would_run_into(mode):
    s_notes = [('hold', 500, 1, 1020), ('s', 1030, 1)]  # snapping the tap to 1000 would land inside its own previous hold
    result, _, s_role = go(mode, [('p', 1000, 0)], s_notes, attack_window_ms=40.)
    assert event(result, sid(s_role, 's'))['start_ms'] == 1030.


@pytest.mark.parametrize('mode', MODES_)
def test_secondary_chord_moves_together(mode):
    result, _, s_role = go(mode, [('p', 1000, 0)], [('a', 1030, 1), ('b', 1030, 2)])
    assert [event(result, sid(s_role, n))['start_ms'] for n in 'ab'] == [1000., 1000.]
    assert result['counts'][s_role]['aligned'] == 2


@pytest.mark.parametrize('mode', MODES_)
def test_snap_into_own_silent_run_is_not_done(mode):
    silent = {'declared_start_ms': 900., 'declared_end_ms': 1010., 'start_ms': 900., 'end_ms': 1010.}
    vocals, accompaniment, p_role, s_role = pair(mode, [('p', 1000, 0)], [('s', 1030, 2)])
    spans = {s_role: [silent], p_role: []}
    result = stem_merge.merge_stems(vocals, accompaniment, mode=mode, gap_ms=GAP, min_hold_ms=MIN_HOLD, silent_spans=spans)
    stem_merge.audit(vocals, accompaniment, result)
    assert event(result, sid(s_role, 's'))['start_ms'] == 1030.


@pytest.mark.parametrize('mode', MODES_)
def test_tie_break_prefers_the_nearest_then_earlier_primary_head_then_the_lower_lane(mode):
    result, p_role, s_role = go(mode, [('a', 1000, 2), ('b', 1040, 0)], [('s', 1020, 3)])
    assert event(result, sid(s_role, 's'))['start_ms'] == 1000.
    (row,) = decisions(result, 'aligned')
    assert row['conflict']['event_id'] == sid(p_role, 'a')
    result, p_role, s_role = go(mode, [('hi', 1000, 3), ('lo', 1000, 1)], [('s', 1020, 0)])
    (row,) = decisions(result, 'aligned')
    assert row['conflict']['event_id'] == sid(p_role, 'lo') and row['conflict']['lane'] == 1
    result, p_role, s_role = go(mode, [('a', 1000, 2), ('b', 1030, 0)], [('s', 1020, 3)])
    assert event(result, sid(s_role, 's'))['start_ms'] == 1030.  # nearest wins over the earlier head


@pytest.mark.parametrize('mode', ['vocals_priority', 'accompaniment_priority'])
def test_same_lane_duplicate_of_the_same_attack_is_dropped_in_priority_modes(mode):
    result, p_role, s_role = go(mode, [('p', 1000, 1)], [('s', 1020, 1)])
    assert event(result, sid(s_role, 's')) is None and event(result, sid(p_role, 'p')) is not None
    reasons = {d['action']: d['reason'] for d in result['decisions'] if d['event_id'] == sid(s_role, 's')}
    assert reasons == {'aligned': 'common_attack', 'dropped': 'same_attack_duplicate'}
    assert result['counts'][s_role]['dropped'] == 1 and result['counts'][s_role]['kept'] == 0


def test_same_lane_duplicate_is_relaned_only_within_the_chord_cap():
    result, p_role, s_role = go('relane', [('p', 1000, 1)], [('s', 1020, 1)])
    moved = event(result, sid(s_role, 's'))
    assert moved['start_ms'] == 1000. and moved['lane'] == 0
    assert {d['action'] for d in result['decisions']} == {'aligned', 'relaned'} and result['counts'][s_role]['relaned'] == 1
    # cap 1: a chord of two is not allowed -> dropped as a duplicate of the attack
    result, p_role, s_role = go('relane', [('p', 1000, 1)], [('s', 1020, 1)], chord_cap=1)
    assert event(result, sid(s_role, 's')) is None
    assert [d['reason'] for d in decisions(result, 'dropped')] == ['same_attack_duplicate']
    # cap 2 would allow a chord of two, but this instant already has two notes
    result, p_role, s_role = go('relane', [('p', 1000, 1), ('q', 1000, 2)], [('s', 1020, 1)], chord_cap=2)
    assert event(result, sid(s_role, 's')) is None
    result, p_role, s_role = go('relane', [('p', 1000, 1), ('q', 1000, 2)], [('s', 1020, 1)], chord_cap=3)
    assert event(result, sid(s_role, 's'))['lane'] in (0, 3)


@pytest.mark.parametrize('mode', MODES_)
def test_different_lane_snapped_note_joins_the_chord_within_the_cap(mode):
    result, p_role, s_role = go(mode, [('p', 1000, 0)], [('s', 1020, 1)])
    assert event(result, sid(s_role, 's'))['start_ms'] == 1000. and decisions(result, 'dropped') == []
    primary3 = [('a', 1000, 0), ('b', 1000, 1), ('c', 1000, 2)]
    result, p_role, s_role = go(mode, primary3, [('s', 1020, 3)], chord_cap=4)
    assert event(result, sid(s_role, 's')) is not None
    result, p_role, s_role = go(mode, primary3, [('s', 1020, 3)], chord_cap=3)
    assert event(result, sid(s_role, 's')) is None
    (row,) = decisions(result, 'dropped')
    assert row['reason'] == 'chord_cap' and row['start_ms'] == 1000.
    assert result['counts'][s_role]['dropped'] == 1 and result['counts'][s_role]['aligned'] == 1


@pytest.mark.parametrize('mode', MODES_)
def test_chord_cap_does_not_touch_a_secondary_only_chord(mode):
    result, _, s_role = go(mode, [('p', 5000, 0)], [('a', 1000, 0), ('b', 1000, 1), ('c', 1000, 2)], chord_cap=2)
    assert len([e for e in result['events'] if e['start_ms'] == 1000.]) == 3


@pytest.mark.parametrize('mode', MODES_)
def test_hold_head_snaps_and_keeps_its_tail_or_becomes_a_tap(mode):
    result, _, s_role = go(mode, [('p', 1000, 0)], [('h', 1030, 2, 2000)])
    hold = event(result, sid(s_role, 'h'))
    assert hold['start_ms'] == 1000. and hold['end_ms'] == 2000.
    result, _, s_role = go(mode, [('p', 1050, 0)], [('h', 1030, 2, 1150)])  # 1050 >= 1150 - 120 -> too short
    tap = event(result, sid(s_role, 'h'))
    assert tap['start_ms'] == 1050. and tap['end_ms'] is None
    (row,) = decisions(result, 'aligned')
    assert row['from_end_ms'] == 1150. and row['to_end_ms'] is None
    result, _, s_role = go(mode, [('p', 1049, 0)], [('h', 1030, 2, 1170)])  # exactly min_hold_ms stays a hold
    assert event(result, sid(s_role, 'h'))['end_ms'] == 1170.


@pytest.mark.parametrize('mode', MODES_)
def test_primary_notes_are_never_moved_and_alignment_is_deterministic(mode):
    for seed in range(60):
        rng = random.Random(1000 + seed)
        p_role, s_role = SIDES[mode]
        grid = [100. + i * 20. + rng.choice([0, 0, 7, 13]) for i in range(60)]
        p = [ev(p_role, f'p{i}', t, rng.randrange(4)) for i, t in enumerate(sorted(rng.sample(grid, 25)))]
        s = [ev(s_role, f's{i}', t + rng.choice([-30, -12, 0, 9, 25, 35]), rng.randrange(4), None)
             for i, t in enumerate(sorted(rng.sample(grid, 35)))]
        for rows in (p, s):  # keep the inputs internally valid (no same-lane same-time twins)
            seen = set()
            rows[:] = [e for e in rows if (e['lane'], round(e['start_ms'], 1)) not in seen and not seen.add((e['lane'], round(e['start_ms'], 1)))]
        vocals, accompaniment = (p, s) if p_role == 'vocals' else (s, p)
        first = run(vocals, accompaniment, mode)
        again = run(copy.deepcopy(vocals), copy.deepcopy(accompaniment), mode)
        assert first == again
        original = {e['id']: e for e in p + s}
        for e in first['events']:
            if e['origins'][0]['stem_role'] == p_role:
                assert e['start_ms'] == original[e['id']]['start_ms'] and e['lane'] == original[e['id']]['lane']
        assert not [d for d in first['decisions'] if d['role'] == p_role]
        for d in decisions(first, 'aligned'):
            assert 0.1 < abs(d['to_ms'] - d['from_ms']) <= 40.


def test_audit_accepts_recorded_snaps_and_rejects_unrecorded_or_oversized_time_changes():
    v = [ev('vocals', 'p', 1000, 0)]
    a = [ev('accompaniment', 's', 1030, 2), ev('accompaniment', 'other', 3000, 1)]
    result = stem_merge.merge_stems(v, a, mode='relane', gap_ms=GAP, min_hold_ms=MIN_HOLD)
    assert stem_merge.audit(v, a, result) is True
    for mutate in (lambda r: r['decisions'].clear(),
                   lambda r: r['counts']['accompaniment'].__delitem__('aligned'),
                   lambda r: r['counts']['accompaniment'].__setitem__('aligned', 2),
                   lambda r: next(e for e in r['events'] if e['id'] == 'a-other').__setitem__('start_ms', 3010.),
                   lambda r: next(e for e in r['events'] if e['id'] == 'a-s').__setitem__('start_ms', 1001.),
                   lambda r: r['decisions'][0].__setitem__('from_ms', 1031.),
                   lambda r: r.__setitem__('attack_window_ms', 20.)):
        broken = copy.deepcopy(result)
        mutate(broken)
        with pytest.raises(ValueError):
            stem_merge.audit(v, a, broken)
    forged = copy.deepcopy(result)  # a primary note may never carry an alignment
    forged['decisions'].append({**forged['decisions'][0], 'event_id': 'v-p', 'role': 'vocals'})
    with pytest.raises(ValueError):
        stem_merge.audit(v, a, forged)


# ---- same-lane spacing (v3): (a) hold tail -> next head >= gap, (b) head -> next head >= gap ---------------

PRIORITY_MODES = ['vocals_priority', 'accompaniment_priority']


def lane_pairs(result):
    for i in range(4):
        rows = sorted((e for e in result['events'] if e['lane'] == i), key=lambda e: e['start_ms'])
        yield from zip(rows, rows[1:])


def assert_spacing(result, gap=GAP):
    """The merged invariant; returns the number of head pairs left closer than gap (the counted residual)."""
    close = 0
    for a, b in lane_pairs(result):
        assert a.get('end_ms') is None or b['start_ms'] - a['end_ms'] >= gap - 1e-6
        close += b['start_ms'] - a['start_ms'] < gap - 1e-6
    return close


def test_min_hold_default_is_60_and_is_recorded():
    assert stem_merge.MIN_HOLD_MS == 60.
    result = stem_merge.merge_stems([ev('vocals', 'h', 1000, 1, 1990), ev('vocals', 't', 1130, 1)], [], mode='relane', gap_ms=GAP)
    assert result['min_hold_ms'] == 60.
    assert event(result, 'v-h')['end_ms'] == 1070.  # 1130 - 60 - 1000 = 70 >= 60 stays a hold
    short = stem_merge.merge_stems([ev('vocals', 'h', 1000, 1, 1990), ev('vocals', 't', 1115, 1)], [], mode='relane', gap_ms=GAP)
    assert event(short, 'v-h')['end_ms'] is None  # 55 < 60: tap


@pytest.mark.parametrize('mode', ['relane'])
def test_rule1_primary_hold_survives_by_relaning_the_next_secondary_note(mode):
    result, p_role, s_role = go(mode, [('h', 1000, 1, 2000)], [('t', 2020, 1)], attack_window_ms=0.)
    assert event(result, sid(p_role, 'h'))['end_ms'] == 2000.
    assert lane_of(result, sid(s_role, 't')) == 0 and event(result, sid(s_role, 't'))['start_ms'] == 2020.
    (row,) = result['decisions']
    assert (row['role'], row['action'], row['reason']) == (s_role, 'relaned', 'tail_spacing_relane')
    assert (row['from_lane'], row['to_lane']) == (1, 0) and row['conflict']['event_id'] == sid(p_role, 'h')
    counts = result['counts'][s_role]
    assert counts['tail_spacing_relane'] == 1 and counts['relaned'] == 0 and counts['kept'] == 1 and counts['dropped'] == 0
    assert 'tail_spacing' not in result['counts'][p_role]
    # a secondary HOLD is moved with its whole span
    moved, p_role, s_role = go(mode, [('h', 1000, 1, 2000)], [('t', 2020, 1, 2600)], attack_window_ms=0.)
    assert lane_of(moved, sid(s_role, 't')) == 0 and event(moved, sid(s_role, 't'))['end_ms'] == 2600.


@pytest.mark.parametrize('mode', ['relane'])
def test_rule1_falls_back_to_shortening_the_hold_when_no_lane_can_take_the_note(mode):
    blockers = [('b%d' % lane, 2010, lane) for lane in (0, 2, 3)]
    result, p_role, s_role = go(mode, [('h', 1000, 1, 2000)] + blockers, [('t', 2020, 1)], attack_window_ms=0.)
    assert lane_of(result, sid(s_role, 't')) == 1
    assert event(result, sid(p_role, 'h'))['end_ms'] == 2020. - GAP
    assert [(d['action'], d['reason']) for d in result['decisions']] == [('shortened', 'tail_spacing')]
    # a lane that only fits the head but not the hold span does not count
    blocked = blockers[:2] + [('late', 2700, 3)]
    result, p_role, s_role = go(mode, [('h', 1000, 1, 2000)] + blocked, [('t', 2020, 1, 2800)], attack_window_ms=0.)
    assert lane_of(result, sid(s_role, 't')) == 1 and event(result, sid(p_role, 'h'))['end_ms'] == 1960.


@pytest.mark.parametrize('mode', PRIORITY_MODES)
def test_rule1_priority_modes_shorten_the_primary_hold_and_never_move_the_secondary_note(mode):
    result, p_role, s_role = go(mode, [('h', 1000, 1, 2000)], [('t', 2020, 1)], attack_window_ms=0.)
    assert event(result, sid(p_role, 'h'))['end_ms'] == 1960. and lane_of(result, sid(s_role, 't')) == 1
    (row,) = result['decisions']
    assert (row['role'], row['action'], row['reason']) == (p_role, 'shortened', 'tail_spacing')
    assert result['counts'][p_role]['tail_spacing'] == 1 and result['counts'][p_role]['shortened'] == 0


@pytest.mark.parametrize('mode', MODES_)
def test_rule2_secondary_hold_before_a_primary_head_yields(mode):
    result, p_role, s_role = go(mode, [('p', 2000, 1)], [('h', 1000, 1, 1990)])
    hold = event(result, sid(s_role, 'h'))
    (row,) = result['decisions']
    if mode == 'relane':  # relaned with its whole span rather than shortened
        assert lane_of(result, sid(s_role, 'h')) == 0 and hold['end_ms'] == 1990.
        assert (row['role'], row['action'], row['reason']) == (s_role, 'relaned', 'tail_spacing_relane')
        assert result['counts'][s_role]['tail_spacing_relane'] == 1
    else:
        assert hold['lane'] == 1 and hold['end_ms'] == 2000. - GAP
        assert (row['role'], row['action'], row['reason']) == (s_role, 'shortened', 'tail_spacing')
        assert (row['from_end_ms'], row['to_end_ms']) == (1990., 1940.) and row['conflict']['event_id'] == sid(p_role, 'p')
        assert result['counts'][s_role]['tail_spacing'] == 1 and result['counts'][s_role]['shortened'] == 0
    assert event(result, sid(p_role, 'p'))['start_ms'] == 2000.
    exact = go(mode, [('p', 2000, 1)], [('h', 1000, 1, 1940)])[0]  # already exactly gap ms away: untouched
    assert exact['decisions'] == []


def test_rule2_relane_shortens_when_no_lane_fits_the_whole_span():
    p = [('p', 2000, 1)] + [('b%d' % lane, 1500, lane) for lane in (0, 2, 3)]
    result, p_role, s_role = go('relane', p, [('h', 1000, 1, 1990)])
    assert lane_of(result, sid(s_role, 'h')) == 1 and event(result, sid(s_role, 'h'))['end_ms'] == 1940.


@pytest.mark.parametrize('mode', MODES_)
def test_rule3_same_side_holds_are_shortened(mode):
    result, p_role, s_role = go(mode, [('x', 5000, 0)], [('h', 1000, 3, 2000), ('t', 2020, 3)])
    assert event(result, sid(s_role, 'h'))['end_ms'] == 1960. and lane_of(result, sid(s_role, 't')) == 3
    assert result['counts'][s_role]['tail_spacing'] == 1 and 'tail_spacing_relane' not in result['counts'][s_role]
    result, p_role, s_role = go(mode, [('h', 1000, 3, 2000), ('t', 2020, 3)], [('x', 5000, 0)])
    assert event(result, sid(p_role, 'h'))['end_ms'] == 1960. and lane_of(result, sid(p_role, 't')) == 3
    assert result['counts'][p_role]['tail_spacing'] == 1
    assert [d['same_stem'] for d in result['decisions']] == [True]


@pytest.mark.parametrize('mode', MODES_)
def test_rule4_shortening_goes_down_to_60_ms_then_becomes_a_tap_and_never_drops_a_head(mode):
    for next_start, expected in ((1130, 1070.), (1120, 1060.), (1119, None), (1100, None)):
        result, p_role, s_role = go(mode, [('x', 9000, 0)], [('h', 1000, 1, 1090), ('t', next_start, 1)], min_hold_ms=60.)
        hold = event(result, sid(s_role, 'h'))
        assert hold['start_ms'] == 1000. and hold['end_ms'] == expected
        assert event(result, sid(s_role, 't')) is not None and result['counts'][s_role]['dropped'] == 0
        (row,) = result['decisions']
        assert (row['action'], row['reason']) == ('shortened' if expected else 'to_tap', 'tail_spacing')


@pytest.mark.parametrize('mode', PRIORITY_MODES)
def test_rule5_cross_stem_heads_closer_than_gap_drop_the_secondary_note(mode):
    result, p_role, s_role = go(mode, [('p', 1000, 1)], [('s', 1030, 1)], attack_window_ms=0.)
    assert event(result, sid(s_role, 's')) is None and event(result, sid(p_role, 'p')) is not None
    (row,) = result['decisions']
    assert (row['role'], row['action'], row['reason']) == (s_role, 'dropped', 'lane_gap')
    assert result['counts'][s_role]['dropped'] == 1 and result['counts'][s_role]['lane_gap_dropped'] == 1
    assert result['counts'][s_role]['kept'] == 0
    # the secondary note right BEFORE a primary hold start
    result, p_role, s_role = go(mode, [('h', 1030, 1, 1500)], [('s', 1000, 1)], attack_window_ms=0.)
    assert event(result, sid(s_role, 's')) is None and event(result, sid(p_role, 'h'))['end_ms'] == 1500.
    # exactly gap apart is fine
    assert go(mode, [('p', 1000, 1)], [('s', 1060, 1)], attack_window_ms=0.)[0]['decisions'] == []


def test_rule5_relane_moves_the_secondary_note_before_it_drops_it():
    result, p_role, s_role = go('relane', [('p', 1000, 1)], [('s', 1030, 1)], attack_window_ms=0.)
    assert lane_of(result, sid(s_role, 's')) == 0 and event(result, sid(s_role, 's'))['start_ms'] == 1030.
    (row,) = result['decisions']
    assert (row['action'], row['reason'], row['from_lane'], row['to_lane']) == ('relaned', 'lane_gap', 1, 0)
    assert result['counts'][s_role]['lane_gap_relane'] == 1 and result['counts'][s_role]['relaned'] == 0
    assert result['counts'][s_role]['dropped'] == 0
    # the secondary note right before a primary hold start moves, the hold is untouched
    result, p_role, s_role = go('relane', [('h', 1030, 1, 1500)], [('s', 1000, 1)], attack_window_ms=0.)
    assert lane_of(result, sid(s_role, 's')) == 0 and event(result, sid(p_role, 'h'))['end_ms'] == 1500.
    # every lane busy within the gap: dropped
    blockers = [('b%d' % lane, 1010, lane) for lane in (0, 2, 3)]
    result, p_role, s_role = go('relane', [('p', 1000, 1)] + blockers, [('s', 1030, 1)], attack_window_ms=0.)
    assert event(result, sid(s_role, 's')) is None
    assert [(d['action'], d['reason']) for d in result['decisions']] == [('dropped', 'lane_gap')]


def test_rule5_same_stem_notes_the_model_wrote_close_are_left_alone_and_counted():
    for mode in MODES_:
        result, p_role, s_role = go(mode, [('a', 1000, 2), ('b', 1030, 2)], [('c', 5000, 0), ('d', 5020, 0), ('e', 5400, 3)])
        assert len(result['events']) == 5 and result['decisions'] == []
        assert result['residual_same_stem_lane_gap'] == 2
        assert result['residual_same_stem_lane_gap_by_role'] == {p_role: 1, s_role: 1}
        assert assert_spacing(result) == 2
    clean = go('relane', [('a', 1000, 2)], [('c', 5000, 0)])[0]
    assert clean['residual_same_stem_lane_gap'] == 0 and clean['residual_same_stem_lane_gap_by_role'] == {'vocals': 0, 'accompaniment': 0}


def test_rule5_same_stem_pair_created_by_a_relane_is_repaired_by_moving_the_relaned_note_again():
    # s1 conflicts with the primary note and is relaned to lane 0, where the secondary stem's own s2 sits 30 ms later.
    result, p_role, s_role = go('relane', [('p', 1000, 1)], [('s1', 1000, 1), ('s2', 1030, 0)], attack_window_ms=0.)
    assert lane_of(result, sid(s_role, 's1')) == 2 and lane_of(result, sid(s_role, 's2')) == 0
    assert [(d['action'], d['reason']) for d in result['decisions'] if d['event_id'] == sid(s_role, 's1')] == [
        ('relaned', 'same_time'), ('relaned', 'lane_gap')]
    assert assert_spacing(result) == 0 and result['residual_same_stem_lane_gap'] == 0
    assert result['counts'][s_role]['relaned'] == 1 and result['counts'][s_role]['lane_gap_relane'] == 1
    # ... or dropped when no lane can take it
    blockers = [('b2', 1005, 2), ('b3', 1005, 3)]
    result, p_role, s_role = go('relane', [('p', 1000, 1)] + blockers, [('s1', 1000, 1), ('s2', 1030, 0)], attack_window_ms=0.)
    assert event(result, sid(s_role, 's1')) is None and event(result, sid(s_role, 's2')) is not None
    assert result['counts'][s_role]['lane_gap_dropped'] == 1


@pytest.mark.parametrize('mode', MODES_)
def test_spacing_pass_is_deterministic_and_order_independent(mode):
    rng = random.Random(7)
    vocals = random_stem('vocals', rng, 120, span=300)
    accompaniment = random_stem('accompaniment', rng, 120, span=300)
    first = run(vocals, accompaniment, mode, gap=45.)
    again = run(copy.deepcopy(vocals), copy.deepcopy(accompaniment), mode, gap=45.)
    assert again == first
    shuffled = run(list(reversed(vocals)), list(reversed(accompaniment)), mode, gap=45.)
    assert shuffled['events'] == first['events'] and shuffled['decisions'] == first['decisions']


def test_audit_accepts_counted_residuals_and_rejects_a_missed_or_forged_spacing_invariant():
    v = [ev('vocals', 'a', 1000, 2), ev('vocals', 'b', 1030, 2), ev('vocals', 'h', 3000, 1, 3500)]
    a = [ev('accompaniment', 't', 3520, 1)]
    result = stem_merge.merge_stems(v, a, mode='vocals_priority', gap_ms=GAP)
    assert stem_merge.audit(v, a, result) is True and result['residual_same_stem_lane_gap'] == 1
    for mutate in (lambda r: r.__setitem__('residual_same_stem_lane_gap', 0),
                   lambda r: r.__setitem__('residual_same_stem_lane_gap_by_role', {'vocals': 0, 'accompaniment': 1}),
                   lambda r: r.__setitem__('gap_ms', 600.),  # tails / heads would then be too close
                   lambda r: r['counts']['accompaniment'].__setitem__('lane_gap_dropped', 1),
                   lambda r: r['decisions'].append({**r['decisions'][0], 'event_id': 'a-t', 'role': 'accompaniment', 'action': 'relaned',
                                                    'reason': 'lane_gap', 'from_lane': 1, 'to_lane': 1})):
        broken = copy.deepcopy(result)
        mutate(broken)
        with pytest.raises(ValueError):
            stem_merge.audit(v, a, broken)
    # a moved note without a decision, and a decision chain that does not start at the input lane, are rejected
    moved = stem_merge.merge_stems([ev('vocals', 'p', 1000, 1)], [ev('accompaniment', 's', 1030, 1)], mode='relane', gap_ms=GAP,
                                   attack_window_ms=0.)
    assert stem_merge.audit([ev('vocals', 'p', 1000, 1)], [ev('accompaniment', 's', 1030, 1)], moved) is True
    for mutate in (lambda r: r['decisions'].clear(), lambda r: r['decisions'][0].__setitem__('from_lane', 3)):
        broken = copy.deepcopy(moved)
        mutate(broken)
        with pytest.raises(ValueError):
            stem_merge.audit([ev('vocals', 'p', 1000, 1)], [ev('accompaniment', 's', 1030, 1)], broken)
    # a dropped note that is also still in the chart
    kept = copy.deepcopy(result)
    kept['decisions'].append({'event_id': 'v-a', 'role': 'vocals', 'action': 'dropped', 'reason': 'lane_gap', 'conflict': None})
    with pytest.raises(ValueError):
        stem_merge.audit(v, a, kept)

def test_audit_rejects_unrecorded_tail_changes_and_miscounted_tail_spacing():
    v = [ev('vocals', 'h', 1000, 3, 2000), ev('vocals', 't', 2020, 3)]
    result = stem_merge.merge_stems(v, [], mode='relane', gap_ms=GAP, min_hold_ms=MIN_HOLD)
    assert stem_merge.audit(v, [], result) is True
    for mutate in (lambda r: r['decisions'].clear(),
                   lambda r: r['counts']['vocals'].__setitem__('tail_spacing', 0),
                   lambda r: r['counts']['vocals'].__setitem__('shortened', 1),
                   lambda r: next(e for e in r['events'] if e['id'] == 'v-h').__setitem__('end_ms', 1500.)):
        broken = copy.deepcopy(result)
        mutate(broken)
        with pytest.raises(ValueError):
            stem_merge.audit(v, [], broken)


def test_randomised_alignment_and_tail_spacing_keep_structure_accounting_and_the_chord_cap():
    for seed in range(120):
        rng = random.Random(500 + seed)
        vocals = random_stem('vocals', rng, rng.randrange(0, 160), span=700, step=5)
        accompaniment = random_stem('accompaniment', rng, rng.randrange(0, 160), span=700, step=5)
        for mode, primary in itertools.product(stem_merge.MODES, stem_merge.PRIMARIES):
            gap = rng.choice([35., 60.])
            cap = rng.choice([2, 3, 4])
            result = stem_merge.merge_stems(vocals, accompaniment, mode=mode, primary=primary, gap_ms=gap,
                                            min_hold_ms=MIN_HOLD, chord_cap=cap)
            stem_merge.audit(vocals, accompaniment, result)
            valid_events(result['events'], 0, END, END + 1000)
            # no hold tail within gap ms of the next head; heads closer than gap are exactly the counted same-stem originals
            assert assert_spacing(result, gap) == result['residual_same_stem_lane_gap']
            assert all(d['reason'] != 'tail_spacing' or d['action'] in ('shortened', 'to_tap') for d in result['decisions'])
            p_role = result['primary_role']
            by_time = {}
            for e in result['events']:
                by_time.setdefault(e['start_ms'], []).append(e)
            for rows in by_time.values():
                p_count = sum(r['origins'][0]['stem_role'] == p_role for r in rows)
                if p_count and len(rows) > p_count:  # secondary notes joined a primary instant: never above the cap
                    assert len(rows) <= max(cap, p_count)
