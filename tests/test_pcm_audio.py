import numpy as np
import soundfile as sf
from tools.pcm_audio import load_wave


def test_float_input_preserves_above_fullscale_values_and_native_sample_time(tmp_path):
    data=np.zeros(44100,np.float32);data[22050]=1.32
    path=tmp_path/'float.wav';sf.write(path,data,44100,subtype='FLOAT')
    raw=load_wave(path,44100,normalize=False)
    assert raw[22050]==data[22050] and len(raw)==44100
    samples=load_wave(path,22050)
    assert len(samples)==22050 and np.argmax(samples)==11025
    assert np.max(np.abs(samples))==1


def test_pcm_and_float_use_same_model_normalization(tmp_path):
    data=np.sin(np.arange(22050)*2*np.pi*440/22050).astype(np.float32)*.5
    a=tmp_path/'pcm.wav';b=tmp_path/'float.wav'
    sf.write(a,data,22050,subtype='PCM_16');sf.write(b,data,22050,subtype='FLOAT')
    assert np.max(np.abs(load_wave(a,22050)-load_wave(b,22050)))<.0001
