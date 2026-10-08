import copy
import json

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from malody_studio import advanced, advanced_api, advanced_plans, audio_bounds, music, nps_star_calibration, quality_workflow, server


def test_server_settings_normalizes_six_ranges_and_legacy_rate_midpoints():
    options = server.settings('song', 'artist', '["hard"]', .15, 50, 42, None, 'v32')

    assert set(options['nps_ranges']) == set(advanced.PRESETS)
    for difficulty, preset in advanced.PRESETS.items():
        bounds = options['nps_ranges'][difficulty]
        assert bounds == {'min': pytest.approx(preset['rate'] * .8),
                          'max': pytest.approx(preset['rate'] * 1.2)}
        assert options['difficulty_rules'].get(difficulty, {}).get('rate', preset['rate']) == pytest.approx(preset['rate'])


def test_server_settings_merges_partial_range_override_and_uses_midpoint():
    options = server.settings(
        'song', 'artist', '["expert"]', .15, 50, 42, None, 'v32',
        difficulty_rules=json.dumps({'hard': {'rate': 10}}),
        nps_ranges=json.dumps({'expert': {'min': 12, 'max': 16}}),
    )

    assert set(options['nps_ranges']) == set(advanced.PRESETS)
    assert options['nps_ranges']['hard'] == {'min': 8.0, 'max': 12.0}
    assert options['nps_ranges']['expert'] == {'min': 12.0, 'max': 16.0}
    assert options['difficulty_rules']['hard']['rate'] == 10.0
    assert options['difficulty_rules']['expert']['rate'] == 14.0


@pytest.mark.parametrize('ranges', [
    {'hard': {'min': True, 'max': 10}},
    {'hard': {'min': float('nan'), 'max': 10}},
    {'hard': {'min': 10, 'max': float('inf')}},
    {'hard': {'min': 12, 'max': 10}},
    {'hard': {'min': .49, 'max': 10}},
    {'unknown': {'min': 1, 'max': 2}},
])
def test_server_rejects_invalid_nps_ranges(ranges):
    with pytest.raises(server.HTTPException) as error:
        server.settings('song', 'artist', '["hard"]', .15, 50, 42, None, 'v32',
                        nps_ranges=json.dumps(ranges, allow_nan=True))
    assert error.value.status_code == 400


def test_legacy_advanced_settings_reconstruct_ranges_from_saved_rates():
    legacy = advanced.defaults()
    legacy.pop('nps_ranges')
    legacy['difficulty_rules']['hard']['rate'] = 11

    normalized = advanced.validate_settings(legacy)

    assert normalized['nps_ranges']['hard'] == {'min': 8.8, 'max': 13.2}
    assert normalized['difficulty_rules']['hard']['rate'] == 11
    assert 'direct_v32_policy' not in normalized


def test_upload_form_passes_range_override_to_options(tmp_path, monkeypatch):
    upload_dir = tmp_path / 'uploads'
    upload_dir.mkdir()
    tags_path = tmp_path / 'vendor' / 'Mapperatorinator' / 'datasets' / 'tags_2026.json'
    tags_path.parent.mkdir(parents=True)
    tags_path.write_text(json.dumps({'tags': []}), encoding='utf-8')
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    monkeypatch.setattr(server, 'ensure_engine', lambda _options: None)
    captured = []
    monkeypatch.setattr(server, 'new_job', lambda source, options: captured.append(copy.deepcopy(options)) or {'id': 'upload'})
    ranges = {'hard': {'min': 7, 'max': 11}}

    with TestClient(server.app) as client:
        response = client.post('/api/jobs', files={'file': ('track.wav', b'RIFFtest')}, data={
            'title': 'song', 'engine': 'v32', 'nps_ranges': json.dumps(ranges), 'parallel_streams': '8',
        })

    assert response.status_code == 200, response.text
    assert captured[0]['nps_ranges']['hard'] == {'min': 7.0, 'max': 11.0}
    assert captured[0]['difficulty_rules']['hard']['rate'] == 9.0
    assert captured[0]['parallel_streams'] == 8


def test_reference_form_passes_range_override_to_options(tmp_path, monkeypatch):
    upload_dir = tmp_path / 'uploads'
    upload_dir.mkdir()
    (upload_dir / 'reference-sirius.m4a').write_bytes(b'audio')
    tags_path = tmp_path / 'vendor' / 'Mapperatorinator' / 'datasets' / 'tags_2026.json'
    tags_path.parent.mkdir(parents=True)
    tags_path.write_text(json.dumps({'tags': []}), encoding='utf-8')
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    monkeypatch.setattr(server, 'ensure_engine', lambda _options: None)
    captured = []
    monkeypatch.setattr(server, 'new_job', lambda source, options: captured.append(copy.deepcopy(options)) or {'id': 'reference'})
    ranges = {'master': {'min': 15, 'max': 19}}

    with TestClient(server.app) as client:
        response = client.post('/api/reference', data={
            'engine': 'v32', 'nps_ranges': json.dumps(ranges), 'parallel_streams': '4',
        })

    assert response.status_code == 200, response.text
    assert captured[0]['nps_ranges']['master'] == {'min': 15.0, 'max': 19.0}
    assert captured[0]['difficulty_rules']['master']['rate'] == 17.0
    assert captured[0]['parallel_streams'] == 4


def test_local_music_form_and_json_batch_pass_ranges_to_options(tmp_path, monkeypatch):
    source = tmp_path / 'track.ogg'
    source.write_bytes(b'audio')
    monkeypatch.setattr(server, 'ensure_engine', lambda _options: None)
    monkeypatch.setattr(music, 'ready_audio', lambda _video_id: (source, {'url': 'https://example.test/audio'}))
    submitted = {'expert': {'min': 10, 'max': 14}}
    captured = []
    monkeypatch.setattr(server, 'new_job', lambda _source, options: captured.append(copy.deepcopy(options)) or {'id': 'local'})

    with TestClient(server.app) as client:
        response = client.post('/api/music/abcdefghijk/generate', data={
            'title': 'song', 'engine': 'v32', 'nps_ranges': json.dumps(submitted), 'parallel_streams': '8',
        })
    assert response.status_code == 200, response.text
    assert captured[-1]['nps_ranges']['expert'] == {'min': 10.0, 'max': 14.0}
    assert captured[-1]['parallel_streams'] == 8

    monkeypatch.setattr(music, 'candidates', {'abcdefghijk': {'title': 'song', 'artist': 'artist'}})
    monkeypatch.setattr(server, 'enqueue_job', lambda options, _source_ref: captured.append(copy.deepcopy(options)) or {'id': 'batch'})
    with TestClient(server.app) as client:
        response = client.post('/api/batches', json={
            'video_ids': ['abcdefghijk'],
            'settings': {'engine': 'v32', 'difficulties': ['expert'], 'nps_ranges': submitted, 'parallel_streams': 4},
        })
    assert response.status_code == 200, response.text
    assert captured[-1]['nps_ranges']['expert'] == {'min': 10.0, 'max': 14.0}
    assert captured[-1]['difficulty_rules']['expert']['rate'] == 12.0
    assert captured[-1]['parallel_streams'] == 4


@pytest.fixture
def advanced_generation_case(tmp_path, monkeypatch):
    source = tmp_path / 'source.wav'
    sf.write(source, np.full((advanced.SR * 2, 2), .1, dtype=np.float32), advanced.SR, subtype='FLOAT')

    def decode(source_path, output_path):
        data, rate = sf.read(source_path, dtype='float32', always_2d=True)
        sf.write(output_path, data, rate, subtype='FLOAT')
        return data

    monkeypatch.setattr(advanced, 'decode', decode)
    monkeypatch.setattr(advanced, 'audio_metadata', lambda _: ([], {'bpm': 120, 'uncertain': True}))
    store = advanced.ProjectStore(tmp_path / 'outputs' / 'advanced')
    project = store.create(source, 'advanced')
    project = store.add_segment(project['id'], {'start_sample': 0, 'end_sample': project['samples']})
    segment = project['segments'][0]
    monkeypatch.setattr(advanced_api, 'store', store)
    monkeypatch.setattr(advanced_plans, 'get_or_build_plan', lambda *_args: {'id': 'plan', 'sections': []})
    monkeypatch.setattr(audio_bounds, 'exact_silence', lambda *_args: False)
    monkeypatch.setattr(quality_workflow, 'freeze', lambda *_args: {})
    monkeypatch.setattr(server, 'ensure_engine', lambda _options: None)
    frozen_policy = {'version': nps_star_calibration.VERSION, 'mapping_id': 'frozen-test', 'mapping_sha256': 'a' * 64}
    monkeypatch.setattr(nps_star_calibration, 'freeze_policy', lambda: copy.deepcopy(frozen_policy))
    return store, project, segment, frozen_policy


def test_advanced_prepare_freezes_server_policy_and_independent_strategy(advanced_generation_case):
    store, project, segment, frozen_policy = advanced_generation_case

    options = advanced_api.prepare_generation(project['id'], segment['id'], {'settings': {'strategy': 'fast'}})
    snapshot = options['_advanced']

    assert snapshot['direct_v32_policy'] == frozen_policy
    assert snapshot['settings']['strategy'] == 'independent'
    assert 'direct_v32_policy' not in snapshot['settings']
    assert set(snapshot['settings']['nps_ranges']) == set(advanced.PRESETS)


def test_old_advanced_snapshot_retry_does_not_add_direct_policy(advanced_generation_case, monkeypatch):
    store, project, segment, _ = advanced_generation_case
    options = advanced_api.prepare_generation(project['id'], segment['id'], {})
    snapshot = options['_advanced']
    snapshot.pop('direct_v32_policy')
    original = {'id': 'b' * 32, 'status': 'failed', 'options': options}
    queued = []
    monkeypatch.setattr(server, 'jobs', {})
    monkeypatch.setattr(server, 'enqueue_job', lambda queued_options, _source_ref: queued.append(copy.deepcopy(queued_options)) or {'id': 'retry'})

    result = server.retry_advanced_job(original)

    assert result == {'id': 'retry'}
    assert 'direct_v32_policy' not in queued[0]['_advanced']
    assert 'direct_v32_policy' not in original['options']['_advanced']

@pytest.mark.parametrize('value', [True, -1, 17, 2.5, float('nan'), float('inf'), '8'])
def test_parallel_stream_settings_reject_invalid_values(value):
    with pytest.raises(ValueError):
        advanced.validate_settings({'parallel_streams': value})
    with pytest.raises(server.HTTPException) as error:
        server.settings('song','artist','["hard"]',.15,50,42,None,'v32',parallel_streams=value)
    assert error.value.status_code == 400


def test_parallel_stream_defaults_and_advanced_frozen_retry(advanced_generation_case, monkeypatch):
    store, project, segment, _ = advanced_generation_case
    assert advanced.validate_settings({})['parallel_streams'] == 0
    assert server.settings('song','artist','["hard"]',.15,50,42,None)['parallel_streams'] == 0
    options = advanced_api.prepare_generation(project['id'], segment['id'], {'settings': {'parallel_streams': 8}})
    assert options['_advanced']['settings']['parallel_streams'] == 8
    original = {'id': 'b'*32, 'status':'failed', 'options': options}
    queued=[]
    monkeypatch.setattr(server,'jobs',{})
    monkeypatch.setattr(server,'enqueue_job',lambda opts,_ref: queued.append(copy.deepcopy(opts)) or {'id':'retry'})
    server.retry_advanced_job(original)
    assert queued[0]['_advanced']['settings']['parallel_streams'] == 8
