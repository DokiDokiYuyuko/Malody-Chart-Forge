"""Acceptance regressions for existing model heads, never density padding."""
import copy
import itertools

from malody_studio.chart_quality import apply, contract


def attack(time, uncertainty=2., strength=2., role=None):
    return dict(time_ms=time, uncertainty_ms=uncertainty, strength=strength,
                multiscale_agreement=True, source_role=role)


def test_common_attack_result_does_not_depend_on_same_time_head_order():
    heads = [dict(id='a', start_ms=1000., end_ms=None, lane=0),
             dict(id='b', start_ms=1000., end_ms=None, lane=1),
             dict(id='c', start_ms=1010., end_ms=None, lane=2)]
    sound = {'heads': {'a': {'onsets': [attack(1000.5, 1.)]},
                       'b': {'onsets': [attack(1002., 2.)]},
                       'c': {'onsets': [attack(1002., 2.)]}}}
    original = copy.deepcopy(heads)
    results = []
    for order in itertools.permutations(heads):
        result = apply(list(order), {}, sound, 'expert', 'balanced')
        results.append({e['id']: e['start_ms'] for e in result['events']})
        assert result['summary']['chord_corrected'] == 1
    assert all(r == results[0] for r in results)
    assert heads == original


def test_known_rule_clipped_tail_is_recomputed_after_head_alignment():
    # Real failing group at game 00:35.338; source time excludes 1.5 s lead-in.
    heads = [dict(id='a', start_ms=33838., end_ms=None, lane=1),
             dict(id='b', start_ms=33838., end_ms=None, lane=3),
             dict(id='c', start_ms=33848., end_ms=34248., lane=2,
                  tail_policy={'kind': 'rule_cap', 'model_end_ms': 34368.,
                               'cap_ms': 400., 'uncapped_start_ms': 33848.})]
    sound = {'heads': {e['id']: {'onsets': [attack(33839.09297052154)],
                               'important_sustain': True} for e in heads}}
    original = copy.deepcopy(heads)
    result = apply(heads, {}, sound, 'expert', 'balanced')
    assert result['summary']['chord_corrected'] == 1
    assert len(result['events']) == 3
    assert len({e['start_ms'] for e in result['events']}) == 1
    hold = next(e for e in result['events'] if e['id'] == 'c')
    assert abs(hold['end_ms']-hold['start_ms']-400.) < 1e-6
    assert hold['end_ms'] <= 34368.
    assert heads == original


def test_true_model_release_is_not_moved_to_force_alignment():
    heads = [dict(id='a', start_ms=1003., end_ms=1403., lane=0),
             dict(id='b', start_ms=1006., end_ms=None, lane=1)]
    sound = {'heads': {e['id']: {'onsets': [attack(1000.)],
                               'important_sustain': True} for e in heads}}
    result = apply(heads, {}, sound, 'expert', 'balanced')
    assert result['events'] == heads
    assert result['decisions'][-1]['reason'] == 'hold_duration_cap'


def test_unknown_attack_is_visible_as_unresolved_not_quality_pass():
    heads = [dict(id='a', start_ms=1000., end_ms=None, lane=0),
             dict(id='b', start_ms=1010., end_ms=None, lane=1)]
    result = apply(heads, {}, {}, 'expert', 'balanced')
    assert result['events'] == heads
    assert result['summary']['alignment_status'] == 'needs_review'
    assert result['summary']['chord_unresolved'] == 1


def rhythm(time, divisor=1, beat=500., grid=1000.):
    return dict(version='v32-rhythm-v1', available=True, token_step_ms=10,
                snap_divisor=divisor, model_time_ms=time, grid_time_ms=grid,
                grid_error_ms=time-grid, beat_length_ms=beat,
                timing_fingerprint='frozen-model-clock')


def test_model_tick_and_group_sound_support_soft_part_without_padding():
    heads = [dict(id='a', start_ms=1000., end_ms=None, lane=0,
                  model_rhythm=rhythm(1000.), origins=[{'source_id':'voice'}]),
             dict(id='b', start_ms=1010., end_ms=1350., lane=1,
                  model_rhythm=rhythm(1010.), origins=[{'source_id':'voice'}])]
    sound = {'heads': {'a': {'onsets': [attack(1005., strength=.8, role='original')]},
                       'b': {'onsets': [], 'important_sustain':True}}}
    result = apply(heads, {}, sound, 'expert', 'balanced')
    assert [e['start_ms'] for e in result['events']] == [1005.,1005.]
    assert result['events'][1]['end_ms'] == 1350.
    assert {e['id'] for e in result['events']} == {'a','b'}
    assert result['decisions'][-1]['qualification']['certified_global_timing'] is False


def test_native_rhythm_without_sound_cannot_force_a_chord():
    heads = [dict(id=str(i), start_ms=t, end_ms=None, lane=i,
                  model_rhythm=rhythm(t), origins=[{'source_id':'voice'}])
             for i,t in enumerate((1000.,1010.))]
    result = apply(heads, {}, {}, 'expert', 'balanced')
    assert result['events'] == heads
    assert result['summary']['alignment_status'] == 'needs_review'


def test_fine_native_subdivision_protects_real_micro_stagger():
    heads = [dict(id=str(i), start_ms=t, end_ms=None, lane=i,
                  model_rhythm=rhythm(t, divisor=32, beat=400.),
                  origins=[{'source_id':'voice'}]) for i,t in enumerate((1000.,1010.))]
    sound = {'heads': {e['id']: {'onsets':[attack(1005.,1.)]} for e in heads}}
    result = apply(heads, {}, sound, 'lunatic', 'technical')
    assert result['events'] == heads


def test_known_independent_attacks_survive_native_same_tick_hint():
    heads = [dict(id=str(i), start_ms=t, end_ms=None, lane=i,
                  model_rhythm=rhythm(t), origins=[{'source_id':'voice'}])
             for i,t in enumerate((1000.,1010.))]
    sound = {'heads': {'0': {'onsets':[attack(1005.)]},
                       '1': {'onsets':[attack(1005.), attack(1011.,.5)],
                             'independent_onset':True}}}
    result = apply(heads, {}, sound, 'expert', 'balanced')
    assert result['events'] == heads
    assert result['decisions'][-1]['reason'] == 'confirmed_independent_attacks'
    assert result['summary']['chord_unresolved'] == 0


def test_frozen_v2_contract_executes_old_policy_instead_of_silent_upgrade():
    heads = [dict(id='a', start_ms=1000., end_ms=None, lane=0),
             dict(id='b', start_ms=1010., end_ms=None, lane=1)]
    sound = {'heads': {'a': {'onsets':[]}, 'b': {'onsets':[attack(1005.)]}}}
    result = apply(heads, {}, sound, 'expert', 'balanced', policy=contract('chart-quality-v2'))
    assert result['events'] == heads
    assert result['version'] == result['contract']['version'] == 'chart-quality-v2'


def test_corrupt_native_evidence_is_unresolved_without_an_exception():
    heads = [dict(id=str(i), start_ms=t, end_ms=None, lane=i,
                  model_rhythm={**rhythm(t),'grid_error_ms':None})
             for i,t in enumerate((1000.,1010.))]
    result = apply(heads, {}, {}, 'expert', 'balanced')
    assert result['events'] == heads
    assert result['summary']['chord_unresolved'] == 1


def test_frozen_v3_keeps_pre_neighbour_guard_semantics():
    heads = [dict(id=str(i), start_ms=t, end_ms=None, lane=i,
                  model_rhythm=rhythm(t,grid=1008.),origins=[{'source_id':'voice'}])
             for i,t in enumerate((1000.,1010.,1020.))]
    sound = {'heads':{e['id']:{'onsets':[attack(1008.)] if i<2 else []}
                      for i,e in enumerate(heads)}}
    old = apply(heads,{},sound,'expert','balanced',policy=contract('chart-quality-v3'))
    current = apply(heads,{},sound,'expert','balanced')
    assert old['version']=='chart-quality-v3'
    assert [e['start_ms'] for e in old['events']]==[1008.,1008.,1020.]
    assert current['events']==heads
    assert current['version']=='chart-quality-v4'


def test_neighbour_guard_is_invariant_to_input_order_and_preserves_all_heads():
    heads = [dict(id=str(i), start_ms=t, end_ms=None, lane=i,
                  model_rhythm=rhythm(t,grid=1008.),origins=[{'source_id':'voice'}])
             for i,t in enumerate((1000.,1010.,1020.))]
    sound = {'heads':{e['id']:{'onsets':[attack(1008.)] if i<2 else []}
                      for i,e in enumerate(heads)}}
    expected = {e['id']:e for e in heads}
    for permutation in itertools.permutations(heads):
        result=apply(list(permutation),{},sound,'expert','balanced')
        assert {e['id']:e for e in result['events']}==expected
        decision = result['decisions'][-1]
        assert decision['reason']=='new_neighbor_group_needs_review'
        assert decision['blocked_neighbors'][0]['before_full_span_ms']==20.
        assert decision['blocked_neighbors'][0]['attempted_full_span_ms']==12.
