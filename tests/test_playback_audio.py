import hashlib
import numpy as np
import soundfile as sf
import pytest
from malody_studio.playback_audio import audition_file, gain_for_peak


def test_audition_rms_metadata_matches_uniform_peak_gain_without_changing_source(tmp_path):
    from malody_studio.playback_audio import audition_metadata
    data=np.array([[1.5,-1.1],[.2,-.8]],dtype=np.float32)
    source=tmp_path/'audio.wav';sf.write(source,data,44100,subtype='FLOAT')
    before=source.read_bytes();metadata=audition_metadata(source,tmp_path)
    assert metadata['frames']==len(data) and metadata['channels']==2
    assert metadata['peak']==1.5
    assert metadata['rms']==pytest.approx(np.sqrt(np.mean(data.astype(np.float64)**2)))
    assert metadata['audition_rms']==pytest.approx(metadata['rms']*.98/1.5)
    assert audition_metadata(source,tmp_path)==metadata
    assert source.read_bytes()==before


def test_peak_safe_copy_preserves_channels_frames_spectrum_and_raw_source(tmp_path):
    phase=np.arange(44100)/44100
    data=np.column_stack((1.3*np.sin(2*np.pi*440*phase),1.1*np.sin(2*np.pi*7800*phase))).astype(np.float32)
    source=tmp_path/'source.wav';sf.write(source,data,44100,subtype='FLOAT')
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    target=audition_file(source,tmp_path);actual,rate=sf.read(target,dtype='float32')
    assert target!=source and rate==44100 and actual.shape==data.shape
    assert np.max(abs(actual))<=.980001
    np.testing.assert_allclose(actual,data*gain_for_peak(float(abs(data).max())),atol=1e-7)
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    assert audition_file(source,tmp_path)==target


def test_clean_audio_has_no_reencoding_or_amplification(tmp_path):
    source=tmp_path/'voice.wav';sf.write(source,np.full((4410,2),.1,np.float32),44100,subtype='FLOAT')
    assert audition_file(source,tmp_path)==source
    assert gain_for_peak(.1)==gain_for_peak(1.)==1
    with pytest.raises(ValueError):gain_for_peak(float('nan'))


def test_distinct_sources_and_changed_file_never_reuse_wrong_playback_cache(tmp_path):
    a=tmp_path/'vocals.wav';b=tmp_path/'accompaniment.wav'
    sf.write(a,np.full((4410,2),1.4,np.float32),44100,subtype='FLOAT')
    sf.write(b,np.full((4410,2),-1.6,np.float32),44100,subtype='FLOAT')
    ar,br=audition_file(a,tmp_path),audition_file(b,tmp_path)
    assert ar!=br
    assert sf.read(ar)[0].mean()>0 and sf.read(br)[0].mean()<0
    sf.write(a,np.full((4410,2),.3,np.float32),44100,subtype='FLOAT')
    assert audition_file(a,tmp_path)==a
