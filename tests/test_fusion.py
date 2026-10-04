import copy
import pytest

from malody_studio.fusion import fuse_revisions, _budget_windows


def note(t, lane=0, tail=None, identity='n', shared=None):
    event = {'id': identity, 'start_ms': float(t), 'lane': lane, 'end_ms': tail}
    if shared: event['audio_evidence'] = {'shared_event_id': shared}
    return event


def revision(role, events):
    return {'id': role + '-rev', 'variant': 'speed--medium', 'range': [0, 44100 * 4],
            'settings': {'seed': 42, 'difficulty_rules': {}}, 'events': events,
            'provenance': {'source_role': role, 'source_id': 'set:' + role, 'stem_set_id': 'set', 'parent_source_id': 'pcm'}}


def plan(target=20, chord=2, gap=120, peak=8):
    return {'id': 'plan', 'content_hash': 'plan-hash', 'source_pcm_sha': 'pcm', 'sections': [
        {'id': 's', 'core': [0, 44100 * 4], 'perDifficulty': {'medium': {'target_heads': target,
         'hard_caps': {'chord': chord, 'peak_1s': peak, 'min_lane_gap_ms': gap, 'hold_max_ms': 900}}}}]}


def test_close_different_voices_survive_and_share_all_lanes():
    v = revision('vocals', [note(100), note(130, identity='v2')])
    a = revision('accompaniment', [note(100, identity='a1'), note(150, identity='a2')])
    before = copy.deepcopy([v, a])
    result = fuse_revisions(v, a, plan())
    assert len(result['events']) == 4
    assert set(e['start_ms'] for e in result['events']) == {100, 130, 150}
    assert len(set(e['lane'] for e in result['events'])) == 4
    assert [v, a] == before
    assert result == fuse_revisions(v, a, plan())
    assert result['provenance']['global_budget_applied'] is True


def test_only_shared_acoustic_evidence_deduplicates():
    v = revision('vocals', [note(100, shared='same')]); a = revision('accompaniment', [note(105, identity='a', shared='same')])
    result = fuse_revisions(v, a, plan())
    assert len(result['events']) == 1
    assert len(result['events'][0]['origins']) == 2
    assert result['decisions'][0]['type'] == 'duplicate_acoustic_event'


def test_dedup_preserves_model_chord_multiplicity_within_one_stem():
    v = revision('vocals', [note(100, 0, identity='v0', shared='same'), note(100, 1, identity='v1', shared='same')])
    a = revision('accompaniment', [note(100, 0, identity='a0', shared='same'), note(100, 1, identity='a1', shared='same')])
    result = fuse_revisions(v, a, plan())
    assert len(result['events']) == 2
    assert all(len(e['origins']) == 2 for e in result['events'])


def test_global_chord_peak_lane_and_hold_constraints():
    v = revision('vocals', [note(100, tail=850), note(101, identity='v2'), note(102, identity='v3')])
    a = revision('accompaniment', [note(100, identity='a1'), note(250, identity='a2'), note(500, identity='a3')])
    result = fuse_revisions(v, a, plan(chord=1, peak=3))
    assert sum(e['start_ms'] == 100 for e in result['events']) <= 1
    assert len(result['events']) <= 3
    for i, event in enumerate(result['events']):
        for later in result['events'][i + 1:]:
            if later['lane'] == event['lane']:
                assert later['start_ms'] - event['start_ms'] >= 120
                assert later['start_ms'] >= (event['end_ms'] or event['start_ms']) + 35


def test_no_doubled_budget_empty_is_legal_and_missing_role_is_error():
    rows = [note(t, lane=i % 4, identity=str(i)) for i, t in enumerate(range(100, 3800, 100))]
    result = fuse_revisions(revision('vocals', rows), revision('accompaniment', rows), plan(target=10))
    assert len(result['events']) <= 12
    assert fuse_revisions(revision('vocals', []), revision('accompaniment', []), plan())['events'] == []
    with pytest.raises(ValueError): fuse_revisions(revision('vocals', []), revision('vocals', []), plan())
    changed = revision('accompaniment', []); changed['provenance']['stem_set_id'] = 'other'
    with pytest.raises(ValueError): fuse_revisions(revision('vocals', []), changed, plan())


def test_hold_occupancy_and_rolling_peak_continue_across_sections():
    p = plan(10, peak=2)
    first = p['sections'][0]; first['core'] = [0, 44100 * 2]
    second = copy.deepcopy(first); second['id'] = 's2'; second['core'] = [44100 * 2, 44100 * 4]
    p['sections'].append(second)
    v = revision('vocals', [note(1900, tail=2700)])
    a = revision('accompaniment', [note(2000, identity='a'), note(2100, identity='b')])
    result = fuse_revisions(v, a, p)
    assert len(result['events']) <= 2
    if len(result['events']) == 2: assert result['events'][0]['lane'] != result['events'][1]['lane']


def test_silent_stem_model_notes_do_not_force_density():
    event = note(100); event['audio_evidence'] = {'audible': False, 'rms': 0}
    result = fuse_revisions(revision('vocals', [event]), revision('accompaniment', []), plan())
    assert result['events'] == []
    assert result['decisions'][0]['type'] == 'omitted'


@pytest.mark.parametrize('strong_role', ['vocals', 'accompaniment'])
def test_confirmed_leak_uses_stronger_complete_original_event(strong_role):
    strong = note(105, 2, tail=505, identity='strong', shared='leak')
    strong['audio_evidence']['salience'] = 1.2
    weak = note(100, 0, tail=850, identity='weak', shared='leak')
    weak['audio_evidence']['salience'] = .05
    v = revision('vocals', [strong if strong_role == 'vocals' else weak])
    a = revision('accompaniment', [strong if strong_role == 'accompaniment' else weak])
    before = copy.deepcopy([v, a])
    result = fuse_revisions(v, a, plan())
    assert len(result['events']) == 1
    event = result['events'][0]
    assert (event['start_ms'], event['end_ms'], event['lane']) == (105, 505, 2)
    assert event['source_id'] == 'set:' + strong_role
    assert event['origins'][0]['note_id'] == 'strong'
    assert result['decisions'][0]['origins'][0]['note_id'] == 'weak'
    assert result['decisions'][0]['representative']['note_id'] == 'strong'
    assert [v, a] == before


def test_stronger_duplicate_owns_actual_core_and_is_resorted():
    weak = note(1995, identity='weak', shared='leak')
    weak['audio_evidence']['salience'] = .05
    strong = note(2005, identity='strong', shared='leak')
    strong['audio_evidence']['salience'] = 1.2
    p = plan()
    p['sections'][0]['core'] = [0, 44100 * 2]
    second = copy.deepcopy(p['sections'][0]); second['id'] = 's2'; second['core'] = [44100 * 2, 44100 * 4]
    p['sections'].append(second)
    result = fuse_revisions(revision('vocals', [strong]), revision('accompaniment', [weak, note(2000, identity='distinct')]), p)
    assert [row['candidate_heads'] for row in result['stats']] == [0, 2]
    assert [row['start_ms'] for row in result['events']] == [2000, 2005]


def test_unconfirmed_near_heads_are_reported_without_deletion_or_fill():
    v = revision('vocals', [note(100, identity='v')])
    a = revision('accompaniment', [note(105, identity='a')])
    result = fuse_revisions(v, a, plan())
    assert len(result['events']) == 2
    assert result['stats'][0]['unconfirmed_cross_source_overlaps'] == 1
    assert all(e['start_ms'] in (100, 105) for e in result['events'])
    assert not any(d['type'] == 'duplicate_acoustic_event' for d in result['decisions'])


def test_shared_label_outside_tolerance_is_not_a_duplicate():
    result = fuse_revisions(revision('vocals', [note(100, shared='same')]),
                            revision('accompaniment', [note(113, identity='a', shared='same')]), plan())
    assert len(result['events']) == 2
    assert not any(d['type'] == 'duplicate_acoustic_event' for d in result['decisions'])


def test_shared_budget_is_distributed_across_continuously_audible_time():
    v = revision('vocals', [note(t, identity=f'v{t}') for t in range(100, 16000, 125)])
    a = revision('accompaniment', [note(t, 1, identity=f'a{t}') for t in range(100, 16000, 125)])
    v['range'] = a['range'] = [0, 16 * 44100]
    p = plan(target=64); p['sections'][0]['core'] = [0, 16 * 44100]
    before = copy.deepcopy([v, a])
    result = fuse_revisions(v, a, p)
    bins = [sum(start <= e['start_ms'] < start + 4000 for e in result['events']) for start in range(0, 16000, 4000)]
    assert min(bins) >= 15
    assert max(bins) - min(bins) <= 2
    assert len(result['events']) <= 66  # Allowance belongs to the whole core.
    windows = result['stats'][0]['budget_windows']
    assert sum(w['target_heads'] for w in windows) == 64
    assert sum(w['max_heads'] for w in windows) == 66
    assert all(w['selected_heads'] <= w['max_heads'] for w in windows)
    assert all(any(o['note_id'] == e['id'] for e in v['events'] + a['events']) for out in result['events'] for o in out['origins'])
    assert [v, a] == before


def test_audio_profile_reserves_budget_without_filling_empty_windows():
    v = revision('vocals', [note(t, identity=str(t)) for t in range(100, 4000, 125)])
    a = revision('accompaniment', [])
    p = plan(target=16)
    p['sections'][0].update(active_seconds=2, profile=[
        {'start_sample': 0, 'end_sample': 2 * 44100, 'active_fraction': 0},
        {'start_sample': 2 * 44100, 'end_sample': 4 * 44100, 'active_fraction': 1}])
    result = fuse_revisions(v, a, p)
    windows = result['stats'][0]['budget_windows']
    assert [w['target_heads'] for w in windows] == [0, 16]
    assert all(e['start_ms'] >= 2000 for e in result['events'])
    # No inferred replacement when a reserved audible window has no raw heads.
    v['events'] = [e for e in v['events'] if e['start_ms'] < 2000]
    assert fuse_revisions(v, a, p)['events'] == []


def test_holds_and_rolling_peaks_continue_across_budget_windows():
    v = revision('vocals', [note(1900, 0, tail=2700)])
    a = revision('accompaniment', [note(2000, 0, identity='a'), note(2100, 0, identity='b')])
    result = fuse_revisions(v, a, plan(target=10, peak=2))
    assert len(result['events']) <= 2
    if len(result['events']) == 2:
        assert result['events'][0]['lane'] != result['events'][1]['lane']


def test_sparse_integer_budget_ties_are_spread_through_time():
    windows = _budget_windows(0, 16 * 44100, 2, [])
    assert [w['max_heads'] for w in windows] == [0, 1, 0, 1, 0, 1, 0, 1]
    assert sum(w['target_heads'] for w in windows) == 2
    assert sum(w['max_heads'] for w in windows) == 4
