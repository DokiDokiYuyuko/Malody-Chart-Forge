import copy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from malody_studio import separation as separation
from malody_studio import separation_models as registry
from tools import roformer_worker as worker


def test_registry_keeps_demucs_exact_keys_and_separates_roformer_parameters():
    assert separation.validated_settings() == {'model': 'htdemucs', 'segment': 7.8, 'overlap': .25, 'shifts': 1, 'seed': 20261003}
    assert separation.validated_settings({'model': 'htdemucs_ft', 'unrelated': 1}) == {**separation.DEFAULTS, 'model': 'htdemucs_ft'}
    options = separation.validated_settings({'model': registry.ROFORMER_MODEL})
    assert options == registry.ROFORMER_DEFAULTS
    for count in (2, 4, 8):
        assert separation.validated_settings({'model': registry.ROFORMER_MODEL, 'overlap_count': count})['overlap_count'] == count
    for value in ({'segment': 7.8}, {'overlap': .25}, {'shifts': 1}, {'overlap_count': 3}, {'overlap_count': True},
                  {'seed': 1.1}, {'seed': True}, {'amp': False}, {'amp': 1}, {'tta': True}, {'batch_size': 2}):
        with pytest.raises(ValueError):
            separation.validated_settings({'model': registry.ROFORMER_MODEL, **value})
    catalog = separation.get_separation_models()
    assert set(catalog) == {'htdemucs', 'htdemucs_ft', registry.ROFORMER_MODEL}
    assert catalog[registry.ROFORMER_MODEL]['quality'] == 'experimental'
    parameters = {row['key']: row for row in catalog[registry.ROFORMER_MODEL]['parameters']}
    assert parameters['overlap_count']['choices'] == [2, 4, 8]
    assert 'overlap' not in parameters
    catalog[registry.ROFORMER_MODEL]['defaults']['seed'] = 1
    assert separation.get_separation_models()[registry.ROFORMER_MODEL]['defaults']['seed'] == 20261003


def test_cuda_failure_is_explicit_before_checkpoint_load():
    with pytest.raises(RuntimeError, match='CUDA'):
        worker.require_cuda(SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))


@pytest.mark.parametrize('frames', [29, 180, 500])
@pytest.mark.parametrize('overlap', [2, 4, 8])
def test_overlap_add_keeps_source_clock_and_cpu_accumulation(monkeypatch, frames, overlap):
    torch = pytest.importorskip('torch')
    monkeypatch.setattr(worker, 'CHUNK_SIZE', 80)
    mix = torch.from_numpy(np.random.default_rng(1).normal(size=(2, frames)).astype(np.float32))
    progress = []
    result, effective = worker.demix_vocals(lambda chunk: chunk * .25, mix, overlap,
                                           device='cpu', amp=False, progress=lambda *event: progress.append(event))
    assert result.shape == (frames, 2)
    assert result.dtype == np.float32
    np.testing.assert_allclose(result, mix.numpy().T * .25, rtol=1e-6, atol=1e-7)
    assert effective['step_samples'] == 80 // overlap
    assert effective['accumulation_device'] == 'cpu'
    assert progress[-1][0] == 1
    assert len(progress) == effective['chunk_count']


def test_malformed_or_nonfinite_model_output_fails_without_repairs(monkeypatch):
    torch = pytest.importorskip('torch')
    monkeypatch.setattr(worker, 'CHUNK_SIZE', 80)
    mix = torch.ones((2, 140))
    with pytest.raises(ValueError, match='形状'):
        worker.demix_vocals(lambda chunk: chunk[..., :-1], mix, device='cpu', amp=False)
    with pytest.raises(ValueError, match='非有限'):
        worker.demix_vocals(lambda chunk: chunk * float('nan'), mix, device='cpu', amp=False)


def test_float_stems_are_raw_and_residual_before_gains(tmp_path):
    source = np.random.default_rng(2).normal(size=(113, 2)).astype(np.float32) * 2
    vocals = source * np.float32(.75)
    rows, alignment = worker.write_stems(tmp_path, source, vocals, {'recipe_hash': 'trial'})
    read_vocals, rate = sf.read(tmp_path / 'vocals.wav', dtype='float32', always_2d=True)
    accompaniment, _ = sf.read(tmp_path / 'accompaniment.wav', dtype='float32', always_2d=True)
    assert rate == 44100
    assert all(row['gain'] == 1 for row in rows)
    assert sf.info(tmp_path / 'vocals.wav').subtype == 'FLOAT'
    assert np.max(np.abs(read_vocals)) > 1
    np.testing.assert_array_equal(read_vocals, vocals)
    np.testing.assert_array_equal(accompaniment, source - vocals)
    np.testing.assert_allclose(read_vocals.astype(np.float64) + accompaniment, source, rtol=1e-7, atol=1e-7)
    assert alignment['accompaniment_method'] == 'source_minus_vocals'
    assert alignment['offset_samples'] == 0
    assert alignment['gain_applied'] is False


def test_new_and_old_adapter_manifests_are_independently_valid(tmp_path):
    source = np.zeros((113, 2), dtype=np.float32)
    rows, _ = worker.write_stems(tmp_path, source, source.copy(), {'recipe_hash': 'recipe'})
    manifest = dict(adapter_version=registry.ROFORMER_VERSION, sample_rate=44100, origin_sample=0,
                    frame_count=113, settings=registry.ROFORMER_DEFAULTS, stems=rows)
    assert separation.validate_manifest(tmp_path, manifest)['frame_count'] == 113
    old = {**manifest, 'adapter_version': separation.VERSION, 'settings': separation.DEFAULTS}
    assert separation.validate_manifest(tmp_path, old)['frame_count'] == 113
    wrong = {**manifest, 'adapter_version': separation.VERSION}
    with pytest.raises(ValueError):
        separation.validate_manifest(tmp_path, wrong)


def test_trial_cache_isolation_and_original_offset_are_in_recipe(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from malody_studio import resident
    # FakeProcess below is a pure cache test; it must not acquire the user's GPU
    # lease or shut down an unrelated real resident between live job requests.
    monkeypatch.setattr(resident,'external_gpu',lambda *args,**kwargs:nullcontext())
    source = tmp_path / 'source.wav'
    sf.write(source, np.zeros((113, 2), dtype=np.float32), 44100, subtype='FLOAT')
    monkeypatch.setattr(separation, 'ROOT', tmp_path)
    (tmp_path / 'logs').mkdir()
    deployment = dict(adapter_version=separation.VERSION, code_version='v4.0.1',
                      models={'htdemucs': ['weight']}, files={'weight': {'sha256': 'sha'}},
                      environment={}, dependency_lock_sha256='lock')
    monkeypatch.setattr(separation, 'deployment', lambda *_: deployment)
    monkeypatch.setattr(separation, '_record_model_inference', lambda *_: True)
    requests = []

    class FakeProcess:
        def __init__(self, args, **_):
            self.returncode = 0; self.pid = 1
            request = separation.read(args[-1]); requests.append(request)
            directory = Path(request['directory'])
            data, _ = sf.read(request['source'], dtype='float32', always_2d=True)
            rows, _ = worker.write_stems(directory, data, data * .5, request)
            fields = ('adapter_version', 'source_pcm_sha', 'sample_rate', 'frame_count', 'origin_sample',
                      'settings', 'deployment_hash', 'recipe_hash')
            manifest = {key: request[key] for key in fields}
            manifest.update(id=request['recipe_hash'], stems=rows)
            if 'cache_scope' in request:
                manifest.update(cache_scope=request['cache_scope'], scope='preview_only')
            separation.atomic(directory / 'manifest.json', manifest)

        def __enter__(self): return self
        def __exit__(self, *_): pass
        def poll(self): return 0

    monkeypatch.setattr(separation.subprocess, 'Popen', FakeProcess)
    full = separation.ensure_stems(source)
    expected_recipe = {key: full[key] for key in ('adapter_version', 'source_pcm_sha', 'sample_rate', 'frame_count',
                                                'origin_sample', 'settings', 'deployment_hash')}
    assert full['id'] == separation.canonical_hash(expected_recipe)
    scope = {'type': 'separation_trial', 'parent_source_pcm_sha256': 'original', 'original_frame_count': 10000,
             'origin_source_sample': 100, 'core': [100, 113], 'context': [100, 213]}
    trial = separation.ensure_stems(source, tmp_path / 'trial', cache_scope=scope)
    assert trial['id'] != full['id']
    assert trial['scope'] == 'preview_only'
    assert trial['cache_scope']['origin_source_sample'] == 100
    assert Path(requests[-1]['directory']).is_relative_to(tmp_path / 'trial' / 'separation')
    assert separation.ensure_stems(source, tmp_path / 'trial', cache_scope=scope)['id'] == trial['id']
    assert len(requests) == 2
    other = separation.ensure_stems(source, tmp_path / 'trial', cache_scope={**scope, 'origin_source_sample': 101})
    assert other['id'] != trial['id']


def test_deployment_accepts_catalog_edits_but_rejects_inference_and_worker_edits(tmp_path, monkeypatch):
    from malody_studio.deployment_integrity import registry_inference_hash
    root = tmp_path
    model_root = root / 'models'
    model_root.mkdir()
    registry_path = root / 'malody_studio' / 'separation_models.py'
    registry_path.parent.mkdir()
    source = Path(registry.__file__).read_text(encoding='utf-8')
    registry_path.write_text(source, encoding='utf-8')
    worker_path = root / 'worker.py'
    worker_path.write_text('worker')
    python = root / 'python.exe'
    python.write_text('runtime')
    lock = root / 'runtime' / 'roformer-requirements-lock.txt'
    lock.parent.mkdir()
    lock.write_text('dependencies')
    weight = model_root / 'weight'
    weight.write_bytes(b'checkpoint')
    manifest = {'adapter_version': registry.ROFORMER_VERSION,
        'models': {registry.ROFORMER_MODEL: ['weight']},
        'files': {'weight': {'bytes': weight.stat().st_size, 'sha256': separation.file_hash(weight)}},
        'dependency_lock_sha256': separation.file_hash(lock),
        'registry_inference_sha256': registry_inference_hash(registry_path),
        'code_files': {path.relative_to(root).as_posix(): {'sha256': separation.file_hash(path)} for path in (registry_path, worker_path)}}
    separation.atomic(model_root / 'manifest.json', manifest)
    monkeypatch.setattr(separation, 'ROOT', root)
    monkeypatch.setattr(separation, '_model_paths', lambda _: (model_root, python, worker_path))
    registry_path.write_text(source.replace("label='Kim MelBand RoFormer'", "label='Updated display label'"), encoding='utf-8')
    separation.deployment(registry.ROFORMER_MODEL)
    registry_path.write_text(source.replace("'overlap_count': 4", "'overlap_count': 2"), encoding='utf-8')
    with pytest.raises(RuntimeError, match='固定代码校验失败'):
        separation.deployment(registry.ROFORMER_MODEL)
    registry_path.write_text(source, encoding='utf-8')
    worker_path.write_text('changed worker')
    with pytest.raises(RuntimeError, match='固定代码校验失败'):
        separation.deployment(registry.ROFORMER_MODEL)
