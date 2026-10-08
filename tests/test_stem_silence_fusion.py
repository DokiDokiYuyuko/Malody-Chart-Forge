"""Silent-stem handling at fusion: rule B inside stem_generation and empty contributions."""
import copy
import os

import numpy as np
import pytest
import soundfile as sf

from malody_studio import advanced, advanced_generation, stem_generation
from tests.test_direct_stem_generation import _conflicting_generate, direct_stem_case  # noqa: F401
from tests.test_stem_silence_handling import RUN, SR, ev, stem_pcm

FUSE_BOUNDS = [0, 40 * SR]


def make_pair(vocals, accompaniment, bounds):
    pair = {}
    for role, events in (('vocals', vocals), ('accompaniment', accompaniment)):
        pair[role] = {'id': role[0] * 32, 'kind': 'stem_raw', 'variant': 'balanced--expert', 'range': bounds,
                      'events': [{k: v for k, v in e.items() if k != 'origins'} for e in events],
                      'provenance': {'source_role': role, 'stem_set_id': 's', 'parent_source_id': 'p', 'source_id': role}}
    return pair


def fuse(vocals, accompaniment, silence, **settings):
    pair = make_pair(vocals, accompaniment, FUSE_BOUNDS)
    return stem_generation._direct_fuse_revisions(pair, {**advanced.defaults(), **settings}, {'version': 'p'},
                                                  FUSE_BOUNDS, 40000., silence)


def silence_for(**roles):
    return {role: ({'runs': runs, 'duration_ms': 40000., 'file': role + '.wav'} if runs is not None else {'skipped': 'gone'})
            for role, runs in roles.items()}


def test_fusion_drops_silent_heads_records_them_and_provenance_carries_the_policy():
    vocals = [ev('vocals', 'a', 5000, 0), ev('vocals', 'b', 15000, 1), ev('vocals', 'c', 15500, 2, 16500),
              ev('vocals', 'd', 25000, 0)]
    accompaniment = [ev('accompaniment', 'a', 15000, 3), ev('accompaniment', 'b', 30000, 3)]
    silence = silence_for(vocals=[RUN], accompaniment=[])
    result = fuse(vocals, accompaniment, silence, fusion_mode='relane')
    provenance = result['provenance']
    assert len(result['events']) == 4  # 6 inputs - 2 silent vocal heads
    dropped = [d for d in provenance['fusion_decisions'] if d['reason'] == 'stem_silent_span']
    assert {d['origin']['note_id'] for d in dropped} == {'v-b', 'v-c'} and all(d['role'] == 'vocals' for d in dropped)
    assert provenance['fusion_counts']['vocals'] == {'input': 4, 'kept': 2, 'dropped': 2, 'relaned': 0, 'shortened': 0,
                                                     'to_tap': 0, 'stem_silent': 2}
    assert provenance['raw_model_heads'] == 6 and provenance['fused_model_heads'] == 4
    assert provenance['accounting_verified'] is True
    record = provenance['stem_silence']
    assert record['policy'] == 'stem-silence-v1' and record['threshold_db_rel_file_peak'] == -50
    assert record['run_min_ms'] == 5000 and record['run_guard_ms'] == 500 and record['frame_ms'] == 100
    assert record['roles']['vocals']['declared_runs'] == [RUN] and record['roles']['accompaniment']['declared_runs'] == []
    assert provenance['fusion_summary']['stem_silent'] == {'vocals': 2, 'accompaniment': 0}
    assert provenance['fusion_summary']['counts']['vocals']['stem_silent'] == 2
    # the declared runs are part of the recipe identity
    plain = fuse(vocals, accompaniment, silence_for(vocals=[], accompaniment=[]), fusion_mode='relane')
    assert plain['provenance']['recipe_hash'] != provenance['recipe_hash'] and len(plain['events']) == 6


def test_missing_stem_audio_skips_the_rule_for_that_stem_and_records_it():
    vocals = [ev('vocals', 'b', 15000, 1)]
    accompaniment = [ev('accompaniment', 'x', 15000, 2)]
    result = fuse(vocals, accompaniment, silence_for(vocals=None, accompaniment=[RUN]), fusion_mode='relane')
    provenance = result['provenance']
    assert [e['origins'][0]['stem_role'] for e in result['events']] == ['vocals']  # accompaniment's own run applied
    assert provenance['stem_silence']['roles']['vocals'] == {'skipped': 'gone'}
    assert provenance['fusion_summary']['stem_silence_skipped'] == ['vocals']
    assert provenance['fusion_summary']['stem_silent'] == {'vocals': 0, 'accompaniment': 1}


def test_provenance_always_says_per_role_declared_runs_or_skipped_with_a_reason():
    vocals = [ev('vocals', 'b', 15000, 1)]
    accompaniment = [ev('accompaniment', 'x', 15000, 2)]
    for silence in (None, {}, silence_for(vocals=[RUN])):  # no analysis at all / a role missing from the analysis
        provenance = fuse(vocals, accompaniment, silence, fusion_mode='relane')['provenance']
        roles = provenance['stem_silence']['roles']
        assert set(roles) == {'vocals', 'accompaniment'}
        for role, row in roles.items():
            assert row is not None and (isinstance(row.get('declared_runs'), list) or (row.get('skipped') and isinstance(row['skipped'], str)))
        skipped = provenance['fusion_summary']['stem_silence_skipped']
        assert skipped == [role for role in ('vocals', 'accompaniment') if 'skipped' in roles[role]]
    assert skipped == ['accompaniment']


def test_legacy_strict_union_ignores_silence_completely():
    vocals = [ev('vocals', 'b', 15000, 1)]
    accompaniment = [ev('accompaniment', 'x', 16000, 2)]
    settings = {k: v for k, v in advanced.defaults().items() if k not in ('fusion_mode', 'fusion_primary')}
    pair = make_pair(vocals, accompaniment, FUSE_BOUNDS)
    result = stem_generation._direct_fuse_revisions(pair, settings, {'version': 'p'}, FUSE_BOUNDS, 40000.,
                                                    silence_for(vocals=[RUN], accompaniment=[RUN]))
    assert len(result['events']) == 2 and result['provenance']['fusion_policy'] == 'preserve_all_model_heads'
    assert 'stem_silence' not in result['provenance']


def test_all_notes_removed_by_silence_is_a_fusion_failure_not_an_empty_chart():
    with pytest.raises(ValueError, match='静音段'):
        fuse([ev('vocals', 'b', 15000, 1)], [ev('accompaniment', 'x', 15000, 2)],
             silence_for(vocals=[RUN], accompaniment=[RUN]), fusion_mode='relane')


def test_stem_silence_runs_reads_the_actual_stem_file(tmp_path):
    path = tmp_path / 'vocals.wav'
    sf.write(path, stem_pcm((1, 3), (0, 8), (1, 2), (0, 4)), SR, subtype='FLOAT')
    row = stem_generation._stem_silence_runs(path)
    runs = [(r['start_ms'], r['end_ms'], r['declared_start_ms'], r['declared_end_ms']) for r in row['runs']]
    assert runs == [(3000, 11000, 3500, 10500)]  # the 4 s tail is shorter than the 5 s minimum
    assert row['duration_ms'] == pytest.approx(17000) and row['sample_rate'] == SR
    missing = stem_generation._stem_silence_runs(tmp_path / 'nope.wav')
    assert 'runs' not in missing and '无法读取' in missing['skipped']


def _generate_with(role_events, warnings_for=()):
    def generate(_source, _directory, local_options, _progress):
        snapshot = local_options['_advanced']
        role = snapshot['source']['source_role']
        rows = []
        for variant in snapshot['variants']:
            events = role_events[role]
            status = 'generated' if events else 'empty_silent_stem'
            condition = {'key': variant['key'] + '__direct0', 'status': status, 'core': [0, 10]}
            if not events:
                condition['silence'] = {'silent_fraction': 1.0}
            rows.append({'variant': variant['key'], 'events': copy.deepcopy(events), 'settings': snapshot['settings'],
                         'kind': 'stem_raw', 'provenance': {'core_conditions': [condition]}})
        return {'advanced_result': rows, 'errors': [],
                'warnings': [{'variant': v['key'], 'kind': 'empty_silent_stem', 'non_fatal': True}
                             for v in snapshot['variants'] if role in warnings_for]}
    return generate


def test_empty_silent_stem_still_contributes_its_row_and_the_variant_fuses(direct_stem_case, monkeypatch):
    tmp_path, original, options, _manifest, variants = direct_stem_case
    options['_advanced']['variants'] = [variants[1]]
    options['_advanced']['settings'].update(fusion_mode='relane')
    events = {'vocals': [], 'accompaniment': [{'id': 'a1', 'start_ms': 100., 'end_ms': None, 'lane': 1}]}
    monkeypatch.setattr(advanced_generation, 'run', _generate_with(events, warnings_for=('vocals',)))
    directory = tmp_path / 'silent-job'
    directory.mkdir()
    result = stem_generation.run(original, directory, options, lambda *_args: None)
    assert result['errors'] == []
    assert [w['stage'] for w in result['warnings']] == ['vocals'] and result['warnings'][0]['non_fatal']
    assert sorted(row['kind'] for row in result['advanced_result']) == ['fusion', 'stem_raw', 'stem_raw']
    fusion = next(row for row in result['advanced_result'] if row['kind'] == 'fusion')
    assert len(fusion['events']) == 1
    assert fusion['provenance']['stem_empty_silent_requests'] == [
        {'role': 'vocals', 'request_key': 'balanced--expert__direct0', 'core': [0, 10], 'silent_fraction': 1.0}]


def test_run_passes_each_stems_own_audio_to_the_rule_and_a_missing_file_is_only_skipped(direct_stem_case, monkeypatch):
    tmp_path, original, options, manifest, variants = direct_stem_case
    options['_advanced']['variants'] = [variants[1]]
    options['_advanced']['settings'].update(fusion_mode='vocals_priority')
    monkeypatch.setattr(advanced_generation, 'run', lambda _s, _d, local, _p: _conflicting_generate(local))
    seen = []
    real = stem_generation._stem_silence_runs

    def spy(path):
        seen.append(path)
        return real(path)

    monkeypatch.setattr(stem_generation, '_stem_silence_runs', spy)
    os.remove(next(r for r in manifest['stems'] if r['role'] == 'vocals')['path'])  # not locatable at fusion time
    directory = tmp_path / 'missing-audio'
    directory.mkdir()
    result = stem_generation.run(original, directory, options, lambda *_args: None)
    assert sorted(os.path.basename(str(p)) for p in seen) == ['accompaniment.wav', 'vocals.wav']
    assert result['errors'] == []
    fusion = next(row for row in result['advanced_result'] if row['kind'] == 'fusion')
    assert fusion['provenance']['fusion_summary']['stem_silence_skipped'] == ['vocals']
    assert 'skipped' in fusion['provenance']['stem_silence']['roles']['vocals']
    assert fusion['provenance']['stem_silence']['roles']['accompaniment']['declared_runs'] == []
