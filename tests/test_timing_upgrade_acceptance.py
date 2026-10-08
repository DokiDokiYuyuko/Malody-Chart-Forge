"""Independent acceptance checks for the shared original-audio timing contract."""
import hashlib
import json
import numpy as np
import pytest
import soundfile as sf
from malody_studio import beat_analysis as ba, music_timing as mt


def test_multi_beat_fit_recovers_period_despite_frame_quantization_and_missing_beats():
    sr = 44100
    indices = np.arange(160)
    truth = 0.137 * sr + indices * 60 * sr / 145
    observed = np.rint(truth / (sr * .020)) * (sr * .020)
    observed = np.delete(observed, [22, 51, 91])
    fit = ba.robust_fit(observed.astype(int), sr)
    assert fit['eligible']
    assert abs(fit['bpm'] - 145) / 145 < .002
    assert abs(fit['phase_sample'] / sr - .137) < .025
    drift_ms = abs(fit['period_samples'] / (60 * sr / 145) - 1) * 30000
    assert drift_ms <= 20


def test_antiphase_stereo_preserves_audible_channel_without_inventing_waveform():
    wave = np.sin(np.arange(4410) * .071).astype('float32') * .3
    mono, policy = ba.mono_audio(np.column_stack((wave, -wave)))
    assert np.array_equal(mono, wave)
    assert policy == 'strongest-channel-antiphase'


def test_weak_nonzero_pcm_is_not_reported_as_exact_silence(monkeypatch):
    import librosa
    called = []
    monkeypatch.setattr(librosa.onset, 'onset_strength', lambda **kw: np.ones(200))
    monkeypatch.setattr(librosa.beat, 'beat_track', lambda **kw: (called.append(True) or 120, np.array([0, 43, 86])))
    data = np.full((44100, 2), 1e-8, dtype='float32')
    result = ba.librosa_beats(data, 44100)
    assert called, 'Nonzero PCM must not bypass analysis as exact silence'
    assert 'silence' not in result.get('reasons', [])


def source(samples=44100 * 65):
    return {'pcm_sha256': 'a' * 64, 'sample_rate': 44100, 'samples': samples,
            'channels': 2, 'effective_end_sample': samples}


def test_downbeat_phase_and_three_beat_meter_survive_timing_contract():
    sr = 44100
    beats = (6615 + np.arange(90) * 22050).tolist()
    value = mt.timing_map({'beat_samples': beats, 'downbeat_samples': beats[::3]}, source())
    assert value['meter'] == 3
    assert value['phase_sample'] == 6615
    assert value['tempo_points'][0]['sample'] == 6615
    assert value['tempo_points'][0]['meter'] == 3
    assert mt.validate_timing(value) == value


def test_irregular_downbeats_cannot_be_promoted_to_confirmed_four_four():
    beats = (6615 + np.arange(90) * 22050).tolist()
    down = [beats[i] for i in [0, 3, 7, 12, 15, 19]]
    value = mt.timing_map({'beat_samples': beats, 'downbeat_samples': down}, source())
    assert value['meter'] is None
    assert not value['eligibility']['v32']


def test_evidence_cache_ignores_song_names_rules_and_selected_difficulty(tmp_path, monkeypatch):
    data = np.zeros((44100 * 3, 2), dtype='float32')
    data[:44100] = .01
    sf.write(tmp_path / 'source.wav', data, 44100, subtype='FLOAT')
    project = {'sample_rate': 44100, 'samples': len(data),
               'source_pcm_sha256': hashlib.sha256(data.astype('<f4').tobytes()).hexdigest()}
    monkeypatch.setattr(mt, 'ROOT', tmp_path)
    monkeypatch.setattr(mt, 'selected_policy', lambda: {'adapter': 'librosa', 'accepted': False})
    calls = []
    monkeypatch.setattr(ba, 'librosa_beats', lambda *args: (calls.append(True) or
                       {'beat_samples': [], 'downbeat_samples': [], 'reasons': ['insufficient_beats']}))
    first = mt.load_or_analyze(tmp_path, project)
    changed = {**project, 'title': 'New name', 'settings': {'dynamic_strength': .4, 'difficulty': 'hard'}}
    second = mt.load_or_analyze(tmp_path, changed)
    assert first['id'] == second['id']
    assert len(calls) == 1
    assert first['source']['effective_end_sample'] == 44100
    assert first['source']['samples'] == len(data)


def test_tampered_or_different_source_timing_is_rejected():
    beats = (6615 + np.arange(90) * 22050).tolist()
    value = mt.timing_map({'beat_samples': beats, 'downbeat_samples': beats[::4]}, source())
    tampered = json.loads(json.dumps(value))
    tampered['phase_sample'] = 0
    with pytest.raises(ValueError):
        mt.validate_timing(tampered)
    with pytest.raises(ValueError):
        mt.validate_timing(value, {**source(), 'pcm_sha256': 'b' * 64})
