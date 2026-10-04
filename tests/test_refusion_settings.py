"""Re-fusion changes only selection; frozen source revisions remain immutable."""
import copy

import numpy as np
import pytest
import soundfile as sf

from malody_studio import advanced, section_plan as sp, separation, stem_generation as sg


@pytest.fixture
def refusion(tmp_path, monkeypatch):
    monkeypatch.setattr(advanced, 'audio_metadata', lambda _: ([], {'bpm': 120, 'beat_times': [], 'uncertain': True}))
    source = tmp_path / 'input.wav'
    sf.write(source, np.full((4 * sp.SR, 2), .2, np.float32), sp.SR, subtype='FLOAT')
    store = advanced.ProjectStore(tmp_path / 'projects')
    project = store.create(source, 'Refusion')
    project = store.add_segment(project['id'], {'start_sample': 0, 'end_sample': project['samples']})
    segment = project['segments'][0]
    settings = advanced.defaults(); settings['dynamic_enabled'] = False
    settings['difficulty_rules']['easy'].update(rate=15, chord=4, gap=20, peak=56)
    project = store.update(project['id'], {'settings': settings})
    def features(data):
        profile = [{'start_sample': i * sp.SR, 'end_sample': (i + 1) * sp.SR,
                    'rms': .1, 'onset_rate': 8., 'active_fraction': .5} for i in range(4)]
        return profile, np.zeros(400), [], .01
    monkeypatch.setattr(sp, 'rhythm_features', features)
    plan = sp.build_plan(store.directory(project['id']) / 'source.wav', settings, project['tempo'])
    advanced.atomic(store.directory(project['id']) / 'section-plans' / (plan['id'] + '.json'), plan)
    stem_id = 'b' * 64
    folder = store.directory(project['id']) / 'stems' / stem_id; folder.mkdir(parents=True)
    rows = []; parents = []
    for role in ('vocals', 'accompaniment'):
        data = np.full((project['samples'], 2), .1, np.float32)
        path = folder / (role + '.wav'); sf.write(path, data, sp.SR, subtype='FLOAT')
        source_id = stem_id + ':' + role
        rows.append({'role': role, 'source_id': source_id, 'file': path.name, 'path': str(path),
                     'pcm_sha': separation.pcm_hash(data), 'file_sha256': separation.file_hash(path), 'frames': len(data)})
        events = [{'id': role + '-' + str(i), 'start_ms': float(100 + i * 100),
                   'end_ms': None, 'lane': i % 4} for i in range(38)]
        parents.append(store.add_revision(project['id'], segment['id'], 'balanced--easy', events,
                                          settings, 'stem_raw', {'source_role': role, 'source_id': source_id,
                                          'stem_set_id': stem_id, 'parent_source_id': project['source_pcm_sha256']},
                                          activate_initial=False))
    manifest = {'id': stem_id, 'recipe_hash': stem_id, 'adapter_version': separation.VERSION,
                'source_pcm_sha': project['source_pcm_sha256'], 'frame_count': project['samples'],
                'origin_sample': 0, 'sample_rate': sp.SR, 'stems': rows, 'settings': separation.validated_settings()}
    advanced.atomic(folder / 'manifest.json', manifest)
    # Any new analysis or inference would violate re-fusion's contract.
    monkeypatch.setattr(sp, 'rhythm_features', lambda *_: pytest.fail('re-fusion re-analyzed audio'))
    monkeypatch.setattr(sg, 'ensure_stems', lambda *_: pytest.fail('re-fusion separated audio'))
    return store, project, segment, plan, parents


def fuse(state, rules=None):
    store, project, segment, plan, parents = state
    payload = {'vocal_revision': parents[0]['id'], 'accompaniment_revision': parents[1]['id'], 'plan_id': plan['id']}
    if rules is not None: payload['settings'] = {'difficulty_rules': {'easy': rules}}
    return sg.fuse_selected(store, project['id'], segment['id'], payload)


def test_refusion_rate_changes_actual_shared_budget_without_rewriting_parents(refusion):
    store, project, segment, plan, parents = refusion
    before = [store.revision(project['id'], row['id']) for row in parents]
    source_before = (store.directory(project['id']) / 'source.wav').read_bytes()
    low = fuse(refusion, {'rate': .5})
    high = fuse(refusion, {'rate': 2.5})
    assert low['provenance']['section_stats'][0]['target_heads'] == 1.
    assert high['provenance']['section_stats'][0]['target_heads'] == 5.
    assert len(low['events']) < len(high['events'])
    assert low['provenance']['applied_plan_hash'] != high['provenance']['applied_plan_hash']
    assert low['provenance']['parent_section_plan_id'] == plan['id']
    saved = advanced.read(store.directory(project['id']) / 'section-plans' / (low['provenance']['section_plan_id'] + '.json'))
    assert sp.validate_plan(saved) is saved and saved['fusion_only']
    assert saved['sections'][0]['profile'] == plan['sections'][0]['profile']
    assert [store.revision(project['id'], row['id']) for row in parents] == before
    assert (store.directory(project['id']) / 'source.wav').read_bytes() == source_before
    assert store.segment(store.load(project['id']), segment['id'])['active'] == {}
    assert advanced.read(store.directory(project['id']) / 'section-plans' / (plan['id'] + '.json')) == plan


def test_refusion_applies_segment_and_request_hard_caps(refusion):
    store, project, segment, _, _ = refusion
    store.edit_segment(project['id'], segment['id'], {'overrides': {'difficulty_rules': {'easy': {
        'rate': 20, 'chord': 1, 'gap': 500, 'peak': 2, 'hold_ms': 250}}}})
    revision = fuse(refusion, {'gap': 400})
    caps = revision['provenance']['section_stats'][0]['hard_caps']
    assert caps == {'target': 40., 'peak': 2, 'chord': 1, 'gap': 400., 'release': 35., 'hold': 250.}
    notes = revision['events']
    assert len({event['start_ms'] for event in notes}) == len(notes)
    assert all(sum(event['start_ms'] - 1000 < other['start_ms'] <= event['start_ms'] for other in notes) <= 2 for event in notes)
    for lane in range(4):
        times = [event['start_ms'] for event in notes if event['lane'] == lane]
        assert all(right - left >= 400 for left, right in zip(times, times[1:]))


def test_fusion_retuning_preserves_activity_tempo_and_zero_silence_budget(refusion):
    _, project, _, plan, _ = refusion
    original = copy.deepcopy(plan)
    original['sections'][0].update(rhythm_activity=.5, tempo_confidence={'eligible_boost': True, 'bpm': 240})
    settings = {**project['settings'], 'dynamic_enabled': True, 'dynamic_strength': .5}
    effective = sg._effective_fusion_plan(original, settings)
    detail = effective['sections'][0]['per_difficulty']['easy']
    assert detail['activity_boost'] > 0 and detail['tempo_boost'] > 0
    assert effective['sections'][0]['tempo_confidence'] == original['sections'][0]['tempo_confidence']
    assert effective['sections'][0]['core'] == original['sections'][0]['core']
    original['sections'][0]['active_seconds'] = 0
    silent = sg._effective_fusion_plan(original, settings)
    assert silent['sections'][0]['per_difficulty']['easy']['target_heads_soft'] == 0
    assert silent['sections'][0]['per_difficulty']['easy']['tempo_boost'] == 0


@pytest.mark.parametrize('mismatch', ['source', 'samples', 'sample_rate'])
def test_refusion_rejects_explicit_plan_from_incompatible_source_or_clock(refusion, mismatch):
    store, project, segment, plan, parents = refusion
    bad = copy.deepcopy(plan)
    if mismatch == 'source': bad['source_pcm_sha'] = 'other-source'
    elif mismatch == 'samples': bad['samples'] += sp.SR; bad['sections'][-1]['core'][1] += sp.SR
    else: bad['sample_rate'] = 22050
    digest = sp.canonical_hash({key: value for key, value in bad.items() if key not in ('id', 'content_hash')})
    bad.update(id=digest, content_hash=digest)
    with pytest.raises(ValueError, match='PCM|时钟'):
        sg.fuse_selected(store, project['id'], segment['id'], {'vocal_revision': parents[0]['id'],
                         'accompaniment_revision': parents[1]['id'], 'section_plan': bad})
