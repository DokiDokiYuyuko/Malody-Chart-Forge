import numpy as np
import pytest
from malody_studio import audio

def test_manual_bpm_works_when_automatic_tracking_fails(monkeypatch):
    monkeypatch.setattr(audio.librosa.onset, 'onset_strength', lambda **kwargs: np.zeros(10))
    monkeypatch.setattr(audio.librosa.beat, 'beat_track', lambda **kwargs: (np.array([0.]), np.array([], dtype=int)))
    y = np.ones(22050, dtype=np.float32) * .1
    with pytest.raises(ValueError):
        audio.analyze(y, 22050)
    result = audio.analyze(y, 22050, bpm_override=120)
    assert result['bpm'] == 120
    assert result['beat_times'] == []
    assert np.isfinite(result['beat_variability'])
