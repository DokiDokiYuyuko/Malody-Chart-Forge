import numpy as np
from malody_studio.beat_analysis import mono_audio,robust_fit,librosa_beats

def test_antiphase_retains_audible_source_clock():
    x=np.sin(np.arange(400)*.1).astype(np.float32)
    y,policy=mono_audio(np.column_stack((x,-x)))
    assert np.array_equal(y,x) and policy=='strongest-channel-antiphase'

def test_multi_beat_fit_recovers_missing_index_without_halving_tempo():
    beats=np.arange(137,20000,500,dtype=float)
    beats=np.delete(beats,12);beats[8]+=35
    fit=robust_fit(beats,1000)
    assert abs(fit['bpm']-120)<.05
    assert fit['indices'][12]-fit['indices'][11]==2
    assert abs(fit['phase_sample']-137)<2

def test_silence_has_no_fake_bpm_phase_or_beats():
    result=librosa_beats(np.zeros((44100,2),np.float32),44100)
    assert result['beat_samples']==[] and result['reasons']==['silence']

def test_deterministic_reference_match_penalizes_misses():
    from tools.run_timing_experiments import detection_metrics
    metrics=detection_metrics([100,600,1100,1600],[110,610],1000)
    assert metrics['recall']==.5 and metrics['missed']==2
    assert metrics['matched_p95_ms']==10 and metrics['p95_ms']>900

def test_full_reference_phase_is_not_hidden_by_match_window():
    from tools.run_timing_experiments import detection_metrics
    metrics=detection_metrics([100,2100,4100,6100],[350,2350,4350,6350],1000)
    assert metrics['matched']==0 and metrics['signed_offset_ms'] is None
    assert metrics['nearest_median_absolute_ms']==250
    assert metrics['nearest_signed_offset_ms']==250
