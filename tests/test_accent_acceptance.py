"""Independent acceptance: preserve supplied heads and positively separate attacks."""
import copy
import itertools

import pytest

from malody_studio.chart_quality import apply


def test_final_neighbour_check_compares_whole_origin_sets_not_count_or_derived_ids():
    from acceptance_helpers import introduced_near_groups
    original = heads((1000., 1010., 1020.))
    for i, event in enumerate(original):
        event['origins'][0]['revision_id'] = 'raw-source-' + str(i)
    derived = copy.deepcopy(original)
    for event in derived:
        event['id'] = 'derived:' + event['id']
    assert introduced_near_groups(original, derived) == []
    derived[0]['start_ms'] = derived[1]['start_ms'] = 1008.
    assert len(introduced_near_groups(original, derived)) == 1
    # Both scans have one nonzero near group; their head sets differ.
    from acceptance_helpers import nearby
    assert len(nearby(original)) == len(nearby(derived)) == 1
    assert introduced_near_groups(original, original) == []


def test_independent_report_links_move_only_group_decisions():
    from acceptance_helpers import decision_head_ids
    decision = {'type': 'chord_aligned', 'moves': [
        {'event_id': 'head-a', 'from_ms': 1000., 'to_ms': 1004.},
        {'event_id': 'head-b', 'from_ms': 1010., 'to_ms': 1004.}]}
    original = copy.deepcopy(decision)
    assert decision_head_ids(decision) == {'head-a', 'head-b'}
    assert decision_head_ids({'event_id': 'head-c', 'event_ids': ['head-a'],
                              'note_ids': ['head-d']}) == {'head-a', 'head-c', 'head-d'}
    assert decision_head_ids({'type': 'unowned_span'}) == set()
    assert decision == original


def heads(times, grid=1004., tails=None):
    return [dict(id=str(i), start_ms=t, lane=i, end_ms=(tails or {}).get(i),
                 origins=[dict(source_id='frozen-stem', note_id=str(i))],
                 model_rhythm=dict(version='v32-rhythm-v1', available=True,
                    grid_time_ms=grid, grid_error_ms=t-grid, beat_length_ms=500.,
                    snap_divisor=4, timing_fingerprint='frozen-clock'))
            for i, t in enumerate(times)]


def peak(time, uncertainty=2.91):
    return dict(time_ms=time, uncertainty_ms=uncertainty, strength=2.,
                multiscale_agreement=True, source_role='original', channel=0)


@pytest.mark.parametrize('difficulty,times', [('expert', (1000., 1010.)),
                                              ('master', (1000., 1008.))])
def test_native_midpoint_cannot_hide_disjoint_measured_attack_intervals(difficulty, times):
    # 2.91 ms is the 256-sample analysis half-window at 44100 Hz, not an
    # artificially precise sub-frame proof. [997.09,1002.91] and
    # [1005.09,1010.91] are disjoint. A grid at 1004 is not attack evidence.
    rows = heads(times)
    sound = {'heads': {'0': {'onsets': [peak(1000.)]},
                       '1': {'onsets': [peak(1008.)]}}}
    for order in itertools.permutations(rows):
        result = apply(list(order), {}, sound, difficulty, 'balanced')
        assert {e['id']: e['start_ms'] for e in result['events']} == {
            e['id']: e['start_ms'] for e in rows}
        assert result['summary']['chord_corrected'] == 0


def test_native_hint_without_measured_sound_retains_original_supply():
    rows = heads((1000., 1010.), tails={1: 1350.})
    before = copy.deepcopy(rows)
    result = apply(rows, {}, {}, 'expert', 'balanced')
    assert result['events'] == before
    assert result['summary']['chord_unresolved'] == 1
    assert rows == before


def test_non_multiscale_transient_cannot_certify_native_grid():
    rows = heads((1000., 1010.))
    single = {**peak(1004.), 'multiscale_agreement': False}
    result = apply(rows, {}, {'heads': {e['id']: {'onsets': [single]} for e in rows}},
                   'expert', 'balanced')
    assert result['events'] == rows
    assert result['summary']['alignment_status'] == 'needs_review'


def test_independent_attack_veto_preserves_absolute_hold_release():
    rows = heads((1000., 1010.), tails={0: 1350.})
    sound = {'heads': {e['id']: {'onsets': [peak(1004.)],
                                'independent_onset': True,
                                'important_sustain': True} for e in rows}}
    result = apply(rows, {}, sound, 'expert', 'balanced')
    assert result['events'] == rows
    assert result['decisions'][-1]['qualification']['protected'] is True


def test_common_attack_order_invariance_includes_ids_origins_and_tail_clock():
    rows = heads((1000., 1000., 1010.), tails={2: 1350.})
    sound = {'heads': {e['id']: {'onsets': [peak(1004.)],
                                'important_sustain': True} for e in rows}}
    before = copy.deepcopy(rows)
    result_maps = []
    group_ids = []
    for order in itertools.permutations(rows):
        result = apply(list(order), {}, sound, 'expert', 'balanced')
        result_maps.append({e['id']: (e['start_ms'], e['end_ms'], e['origins'])
                            for e in result['events']})
        decision = result['decisions'][-1]
        group_ids.append(decision['chord_group_id'])
        assert decision['qualification']['certified_global_timing'] is False
        assert result['summary']['heads_after'] == len(rows)
    assert all(value == result_maps[0] for value in result_maps)
    assert len(set(group_ids)) == 1
    assert rows == before


@pytest.mark.parametrize('difficulty,times,anchor,tails', [
    ('expert', (1000., 1010., 1020.), 1008., {}),
    ('master', (1000., 1004., 1012.), 1004., {}),
    ('expert', (1000., 1010., 1020.), 1008., {0: 1320., 2: 1360.}),
    ('expert', (1000., 1010., 1020.), 1008., {0: 1400., 2: 1360.}),
])
def test_local_alignment_cannot_pull_undecided_outside_head_into_new_full_span_group(
        difficulty, times, anchor, tails):
    # Outside is already close to the LAST old member. The real failing cases
    # require the whole anchored group span, not minimum pair distance.
    rows = heads(times, grid=anchor, tails=tails)
    if tails.get(0) == 1400.:
        rows[0]['tail_policy'] = {'kind': 'rule_cap', 'model_end_ms': 1600.,
                                  'cap_ms': 400., 'uncapped_start_ms': 1000.}
    sound = {'heads': {e['id']: {'onsets': [peak(anchor, 2.)] if i < 2 else [],
                                'important_sustain': True}
                       for i, e in enumerate(rows)}}
    before = copy.deepcopy(rows)
    result = apply(rows, {}, sound, difficulty, 'balanced')
    assert result['events'] == before
    assert result['summary']['chord_corrected'] == 0
    assert any(d.get('reason') == 'new_neighbor_group_needs_review'
               for d in result['decisions'])
    assert rows == before
