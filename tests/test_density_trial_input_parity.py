import copy
import hashlib
import json

import librosa
import numpy as np
import pytest
import soundfile as sf

from malody_studio import density_trials, mapperatorinator
from malody_studio.beat_analysis import mono_audio
from malody_studio.charts import Note
from malody_studio.separation import pcm_hash


@pytest.mark.parametrize('trim_enabled', [True, False])
def test_trial_model_input_matches_advanced_retained_pcm_and_keeps_full_source_identity(tmp_path, monkeypatch, trim_enabled):
    rate, cutoff = 44100, 44101
    signal = np.sin(np.arange(cutoff, dtype=np.float32) * .11).astype(np.float32)
    # Antiphase stereo also exercises the same non-cancelling mono policy.
    data = np.zeros((rate * 2, 2), dtype=np.float32)
    data[:cutoff, 0], data[:cutoff, 1] = signal, -signal
    source = tmp_path / 'source.wav'
    sf.write(source, data, rate, subtype='FLOAT')
    before = source.read_bytes()
    stop = cutoff if trim_enabled else len(data)
    expected = librosa.resample(mono_audio(data[:stop])[0], orig_sr=rate, target_sr=22050)
    trial = {'source': {'path': str(source), 'source_id': 'original', 'source_role': 'mix', 'pcm_sha': pcm_hash(data)},
             'bounds': [0, stop], 'windows': [[0, cutoff // 2], [cutoff // 2, stop]],
             'conditions': [5.9], 'seed': 17, 'active_seconds': 1., 'split': 'development'}
    project = {'id': 'frozen-project', 'title': 'source', 'artist': 'artist', 'samples': len(data),
               'source_pcm_sha256': pcm_hash(data),
               'tail_trim': {'enabled': trim_enabled, 'cutoff_sample': cutoff, 'samples': len(data), 'rule': 'exact-zero-tail-v1'}}
    options = {'_advanced': {'project': project, 'trial': trial, 'settings': {}}}
    frozen = copy.deepcopy(options)
    calls = []

    def generate(wave, directory, local, progress):
        prepared, actual_rate = sf.read(wave, dtype='float32')
        assert actual_rate == 22050
        np.testing.assert_array_equal(prepared, expected)
        calls.append(local)
        charts = {p['key']: ([Note((p['start_time'] + p['end_time']) / 2, 0)], [(0., 120.)], 0)
                  for p in local['_advanced_presets']}
        return charts, {}

    monkeypatch.setattr(mapperatorinator, 'generate', generate)
    directory = tmp_path / 'trial'
    directory.mkdir()
    result = density_trials.run(source, directory, options, lambda *_: None)
    assert len(calls) == 1 and len(calls[0]['_advanced_presets']) == 2
    from malody_studio.advanced_generation import _v32_time_bounds
    assert [(p['start_time'], p['end_time']) for p in calls[0]['_advanced_presets']] == [
        _v32_time_bounds(bounds) for bounds in trial['windows']]
    assert result['observations'][0]['heads'] == 2
    assert options == frozen and source.read_bytes() == before
    execution = json.loads((directory / 'trial-execution.json').read_text(encoding='utf-8'))
    identity = execution['input_pcm_identity']
    assert identity['source']['frames'] == len(data)
    assert identity['source']['pcm_sha256'] == pcm_hash(data)
    assert identity['source']['file_sha256'] == hashlib.sha256(before).hexdigest()
    assert identity['prepared']['source_stop_sample_exclusive'] == stop
    assert identity['prepared']['frames'] == len(expected)
    assert identity['prepared']['pcm_sha256'] == pcm_hash(expected)
    assert identity['prepared']['wav_sha256'] == execution['input_file_sha256']


@pytest.mark.parametrize('invalid', ['source_identity', 'past_retained_end'])
def test_invalid_frozen_trial_input_is_rejected_before_model_dispatch(tmp_path, monkeypatch, invalid):
    data = np.ones((44100 * 2, 1), dtype=np.float32)
    data[44100:] = 0
    source = tmp_path / 'source.wav'
    sf.write(source, data, 44100, subtype='FLOAT')
    trial = {'source': {'path': str(source), 'pcm_sha': pcm_hash(data)},
             'bounds': [0, 44101 if invalid == 'past_retained_end' else 44100],
             'conditions': [5.9], 'seed': 17, 'active_seconds': 1., 'split': 'development'}
    if invalid == 'source_identity':
        trial['source']['pcm_sha'] = 'wrong'
    options = {'_advanced': {'project': {'id': 'project', 'title': 'source', 'artist': 'artist',
                                        'source_pcm_sha256': pcm_hash(data), 'samples': len(data),
                                        'tail_trim': {'enabled': True, 'cutoff_sample': 44100}},
                             'trial': trial, 'settings': {}}}
    monkeypatch.setattr(mapperatorinator, 'generate', lambda *_: pytest.fail('invalid input must not reach model'))
    folder = tmp_path / 'trial'
    folder.mkdir()
    with pytest.raises(ValueError, match='PCM'):
        density_trials.run(source, folder, options, lambda *_: None)
