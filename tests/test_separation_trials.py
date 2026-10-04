import copy
import io
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient

from malody_studio import advanced, advanced_api, separation, separation_trials, server


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(advanced, 'audio_metadata', lambda _: ([], {'bpm': 120, 'beat_times': [], 'uncertain': True}))
    store = advanced.ProjectStore(tmp_path / 'outputs' / 'advanced')
    source = tmp_path / 'input.wav'
    clock = np.arange(advanced.SR * 24 + 37, dtype=np.float64)
    data = np.column_stack((.3 * np.sin(clock * .007), .2 * np.cos(clock * .013))).astype(np.float32)
    sf.write(source, data, advanced.SR, subtype='FLOAT')
    p = store.create(source, '局部测试', '')
    monkeypatch.setattr(advanced_api, 'store', store)
    monkeypatch.setattr(separation_trials, 'ROOT', tmp_path)
    monkeypatch.setattr(separation, 'deployment', lambda *_: {})
    queued = []
    monkeypatch.setattr(server, 'enqueue_job', lambda options, ref: queued.append((copy.deepcopy(options), copy.deepcopy(ref))) or {'id': 'a' * 32})
    monkeypatch.setattr(server, 'jobs', {})
    app = FastAPI(); app.include_router(advanced_api.router)
    return store, p, queued, TestClient(app)


def infer(monkeypatch, captures):
    def fake(source, directory, settings, progress, *, cache_scope):
        data, rate = sf.read(source, dtype='float32', always_2d=True)
        captures.append((Path(source), Path(directory), copy.deepcopy(settings), copy.deepcopy(cache_scope), data.copy()))
        rows = []
        for role, gain in [('vocals', 6.), ('accompaniment', .75)]:
            path = Path(directory) / (role + '-context.wav')
            sf.write(path, data * gain, rate, subtype='FLOAT')
            rows.append({'role': role, 'path': str(path)})
        return {'id': 'b' * 64, 'recipe_hash': 'b' * 64, 'stems': rows}
    monkeypatch.setattr(separation_trials, 'ensure_stems', fake)


@pytest.mark.parametrize('start,end', [(0, 11025), (9 * advanced.SR + 13, 10 * advanced.SR + 29), (23 * advanced.SR + 7, 24 * advanced.SR + 37)])
def test_trial_context_integer_offsets_and_exact_core_without_normalization(setup, monkeypatch, tmp_path, start, end):
    store, p, queued, client = setup; captures = []; infer(monkeypatch, captures)
    before = store.load(p['id']); original, _ = sf.read(store.directory(p['id']) / 'source.wav', dtype='float32', always_2d=True)
    prefix = '/api/advanced/projects/' + p['id']
    response = client.post(prefix + '/separation-trials', json={'start_sample': start, 'end_sample': end})
    assert response.status_code == 200, response.text
    state = queued[0][0]['_advanced']; trial_id = response.json()['trial_id']
    assert state['task_type'] == 'separation_trial' and 'segment' not in state
    assert queued[0][1] == {'type': 'project_file', 'path': f"outputs/advanced/{p['id']}/source.wav"}
    result = separation_trials.run(store.directory(p['id']) / 'source.wav', tmp_path / 'job', queued[0][0], lambda *_: None)
    assert set(result) == {'trial_id', 'trial_manifest'} and result['trial_id'] == trial_id
    manifest = result['trial_manifest']; context = [max(0, start - 8 * advanced.SR), min(p['samples'], end + 8 * advanced.SR)]
    assert manifest['core'] == [start, end] and manifest['context'] == context
    assert manifest['origin_source_sample'] == start and manifest['context_origin_source_sample'] == context[0]
    assert manifest['original_frame_count'] == p['samples'] and manifest['parent_source_pcm_sha256'] == p['source_pcm_sha256']
    assert manifest['preview_only'] is True and manifest['scope'] == 'preview_only'
    assert len(captures) == 1 and captures[0][3]['origin_source_sample'] == context[0]
    assert captures[0][0].parent == store.directory(p['id']) / 'separation-trials' / trial_id / 'cache'
    np.testing.assert_array_equal(captures[0][4], original[context[0]:context[1]])
    for row in manifest['audio']:
        path = store.directory(p['id']) / 'separation-trials' / trial_id / row['file']
        data, rate = sf.read(path, dtype='float32', always_2d=True)
        gain = {'original': 1., 'vocals': 6., 'accompaniment': .75}[row['role']]
        np.testing.assert_array_equal(data, original[start:end] * gain)
        assert rate == advanced.SR and len(data) == end - start and row['frames'] == end - start
        assert row['rms'] == pytest.approx(float(np.sqrt(np.mean(data.astype(np.float64) ** 2))))
        assert row['rms_ratio'] == pytest.approx(gain, rel=1e-6)
        assert 'path' not in row and 'source_id' not in row
    assert not (store.directory(p['id']) / 'stems').exists()
    assert store.load(p['id']) == before
    assert client.get(prefix + '/separations').json()['stem_sets'] == []
    assert client.get(prefix + '/separation-trials').json()['trials'][0]['id'] == trial_id
    assert client.get(prefix + '/separation-trials/' + trial_id).json()['core'] == [start, end]
    for row in manifest['audio']:
        response = client.get(row['audio_url']); assert response.status_code == 200
        played, rate = sf.read(io.BytesIO(response.content), dtype='float32', always_2d=True)
        assert rate == advanced.SR and len(played) == end - start
        wave = client.get(row['waveform_url'], params={'points': 32})
        assert wave.status_code == 200 and len(wave.json()['peaks']) == 32
        assert wave.json()['end_ms'] == pytest.approx((end - start) * 1000 / advanced.SR)
    with pytest.raises(ValueError): separation.resolve_source(store, p['id'], 'trial:' + trial_id + ':vocals')


@pytest.mark.parametrize('payload', [{}, {'start_sample': True, 'end_sample': 44100}, {'start_sample': 0., 'end_sample': 44100},
    {'start_sample': -1, 'end_sample': 44100}, {'start_sample': 0, 'end_sample': 11024}, {'start_sample': 44100, 'end_sample': 44100},
    {'start_sample': 0, 'end_sample': 999999999}, {'start_sample': 0, 'end_sample': 44100, 'settings': {'model': 'unknown'}},
    {'start_sample': 0, 'end_sample': 44100, 'settings': {'overlap': 4}}])
def test_invalid_trial_never_queues_or_checks_deployment(setup, monkeypatch, payload):
    store, p, queued, client = setup
    monkeypatch.setattr(separation, 'deployment', lambda *_: pytest.fail('invalid trial checked model'))
    response = client.post(f"/api/advanced/projects/{p['id']}/separation-trials", json=payload)
    assert response.status_code == 400 and not queued


def test_stale_revision_unavailable_model_and_corrupted_parent_do_not_queue(setup, monkeypatch):
    store, p, queued, client = setup; prefix = '/api/advanced/projects/' + p['id']
    payload = {'start_sample': 0, 'end_sample': advanced.SR}
    assert client.post(prefix + '/separation-trials', json={**payload, 'expected_revision': 999}).status_code in (400, 409)
    monkeypatch.setattr(separation, 'deployment', lambda *_: (_ for _ in ()).throw(RuntimeError('分离模型尚未部署')))
    assert client.post(prefix + '/separation-trials', json=payload).status_code == 409
    sf.write(store.directory(p['id']) / 'source.wav', np.zeros((p['samples'], 2), np.float32), advanced.SR, subtype='FLOAT')
    assert client.post(prefix + '/separation-trials', json=payload).status_code == 400
    assert not queued


def test_snapshot_freeze_cached_retry_is_immutable_and_trial_tasks_stay_separate(setup, monkeypatch, tmp_path):
    store, p, queued, client = setup; prefix = '/api/advanced/projects/' + p['id']; captures = []; infer(monkeypatch, captures)
    payload = {'start_sample': advanced.SR, 'end_sample': 2 * advanced.SR, 'settings': {'overlap': .5}}
    response = client.post(prefix + '/separation-trials', json=payload); trial_id = response.json()['trial_id']
    options = queued[0][0]; frozen = copy.deepcopy(options)
    store.add_segment(p['id'], {'start_sample': 0, 'end_sample': advanced.SR})
    result = separation_trials.run(store.directory(p['id']) / 'source.wav', tmp_path / 'job', options, lambda *_: None)
    folder = store.directory(p['id']) / 'separation-trials' / trial_id
    unchanged = {path.name: path.read_bytes() for path in folder.glob('*.wav')}; unchanged['manifest.json'] = (folder / 'manifest.json').read_bytes()
    monkeypatch.setattr(separation_trials, 'ensure_stems', lambda *_args, **_kwargs: pytest.fail('cached trial repeated inference'))
    assert separation_trials.run(store.directory(p['id']) / 'source.wav', tmp_path / 'retry', options, lambda *_: None) == result
    assert all((folder / name).read_bytes() == value for name, value in unchanged.items())
    assert options == frozen and store.load(p['id'])['segments']
    monkeypatch.setattr(server, 'jobs', {'a' * 32: {'id': 'a' * 32, 'status': 'completed', 'options': options},
                                      'c' * 32: {'id': 'c' * 32, 'status': 'queued', 'options': {'_advanced': {'task_type': 'separation', 'project': {'id': p['id']}}}}})
    assert [row['id'] for row in client.get(prefix + '/separation-trials').json()['tasks']] == ['a' * 32]
    assert [row['id'] for row in client.get(prefix + '/separations').json()['tasks']] == ['c' * 32]
    assert client.get(prefix + '/separation-trials/' + 'd' * 32).status_code == 404
    assert client.get(prefix + '/separation-trials/' + trial_id + '/audio/mix').status_code == 400
    assert client.get(prefix + '/separation-trials/' + trial_id + '/waveform?start_ms=nan').status_code == 400


def test_worker_rejects_changed_parent_and_context_without_inference(setup, monkeypatch, tmp_path):
    store, p, queued, client = setup; prefix = '/api/advanced/projects/' + p['id']
    client.post(prefix + '/separation-trials', json={'start_sample': 0, 'end_sample': advanced.SR})
    monkeypatch.setattr(separation_trials, 'ensure_stems', lambda *_args, **_kwargs: pytest.fail('invalid snapshot started inference'))
    options = copy.deepcopy(queued[0][0]); options['_advanced']['context_range']['start_sample'] = 1
    with pytest.raises(ValueError, match='上下文范围'): separation_trials.run(store.directory(p['id']) / 'source.wav', tmp_path / 'job', options, lambda *_: None)
    sf.write(store.directory(p['id']) / 'source.wav', np.zeros((p['samples'], 2), np.float32), advanced.SR, subtype='FLOAT')
    with pytest.raises(ValueError, match='提交快照'): separation_trials.run(store.directory(p['id']) / 'source.wav', tmp_path / 'job', queued[0][0], lambda *_: None)


def test_full_source_and_trial_equal_pcm_are_isolated_by_trial_scope(setup, monkeypatch, tmp_path):
    store, p, queued, client = setup; captures = []; infer(monkeypatch, captures)
    client.post(f"/api/advanced/projects/{p['id']}/separation-trials", json={'start_sample': 0, 'end_sample': p['samples']})
    separation_trials.run(store.directory(p['id']) / 'source.wav', tmp_path / 'job', queued[0][0], lambda *_: None)
    assert captures[0][3]['type'] == 'separation_trial' and captures[0][3]['original_frame_count'] == p['samples']
    assert captures[0][1].is_relative_to(store.directory(p['id']) / 'separation-trials')
    assert not (tmp_path / 'cache' / 'separation').exists()
