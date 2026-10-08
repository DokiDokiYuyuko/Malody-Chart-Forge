import copy
import pytest
from malody_studio import paired_timing


def fixture(start=0):
    project={'samples':441000,'sample_rate':44100,'source_pcm_sha256':'original',
             'timing_map':{'uncertain':True},'tempo':{'bpm':123,'uncertain':True}}
    segment={'start_sample':start,'end_sample':441000}
    manifest={'id':'stems','source_pcm_sha':'original','sample_rate':44100,'origin_sample':0,
              'frame_count':441000,'stems':[{'role':'accompaniment','source_id':'stems:accompaniment','pcm_sha':'a'}]}
    row={'id':'revision','kind':'stem_raw','variant':'balanced--expert','range':[start,441000],
         'provenance':{'source_role':'accompaniment','source_id':'stems:accompaniment','pcm_sha':'a',
                       'parent_source_id':'original','stem_set_id':'stems',
                       'serialization_tempo':{'source':'model_output','points':[[start/44.1,110],[9000,112]]}}}
    return project,segment,row,manifest


@pytest.mark.parametrize('start',[0,44100*3])
def test_model_clock_rescue_keeps_nonzero_origin_and_does_not_change_detection(start):
    project,segment,row,manifest=fixture(start);before=copy.deepcopy((project,segment,row,manifest))
    frozen,record=paired_timing.recovery_project(project,segment,'vocals',row,manifest,'balanced--expert',authorized_missing_timing=True)
    assert (project,segment,row,manifest)==before
    assert frozen['timing_rescue']['points']==row['provenance']['serialization_tempo']['points']
    assert frozen['tempo']==project['tempo']
    assert frozen['timing_rescue']['authorized_missing_timing'] and not frozen['timing_rescue']['confirmed']
    assert record['reference_revision_id']=='revision' and record['attempts']==1
    assert record['confirmed'] is False and record['clock']=='original_absolute_ms'
    assert frozen['timing_map']==project['timing_map'] and project['timing_map']['uncertain'] is True


@pytest.mark.parametrize('change',[{'range':[0,440999]},{'variant':'balanced--hard'},
    {'provenance':{'pcm_sha':'other'}},{'provenance':{'stem_set_id':'other'}},
    {'provenance':{'parent_source_id':'other'}},{'provenance':{'source_role':'vocals'}}])
def test_wrong_paired_artifact_is_rejected(change):
    project,segment,row,manifest=fixture()
    row.update({k:v for k,v in change.items() if k!='provenance'})
    row['provenance'].update(change.get('provenance',{}))
    with pytest.raises(ValueError):paired_timing.recovery_project(project,segment,'vocals',row,manifest,'balanced--expert',authorized_missing_timing=True)


@pytest.mark.parametrize('points',[[[-1000,110]],[[0,0]],[[0,float('nan')]],[[0,110],[0,112]],[[0,110],[11000,112]]])
def test_invalid_model_clock_not_used(points):
    project,segment,row,manifest=fixture();row['provenance']['serialization_tempo']['points']=points
    with pytest.raises(ValueError):paired_timing.recovery_project(project,segment,'vocals',row,manifest,'balanced--expert',authorized_missing_timing=True)


def test_audio_estimate_cannot_masquerade_as_model_timing():
    project,segment,row,manifest=fixture();row['provenance']['serialization_tempo']['source']='audio_analysis'
    with pytest.raises(ValueError):paired_timing.recovery_project(project,segment,'vocals',row,manifest,'balanced--expert',authorized_missing_timing=True)


def test_original_native_clock_runs_once_caches_and_preserves_nonzero_anchor(tmp_path,monkeypatch):
    from malody_studio import beat_analysis, resident
    import numpy as np
    import soundfile as sf
    project,segment,_,_=fixture(3*44100);calls=[]
    sf.write(tmp_path/'original.wav',np.zeros((project['samples'],2),np.float32),44100,subtype='FLOAT')
    monkeypatch.setattr(resident,'code_version',lambda:'locked-code')
    def analyze(*args):
        calls.append(args)
        return {'beat_samples':[44100+i*22050 for i in range(16)],'model_timing_points':[[1000,110],[8000,110]],
                'provenance':{'reference_used':False,'rescue_used':False}}
    monkeypatch.setattr(beat_analysis,'v32_timing',analyze)
    frozen,record=paired_timing.original_recovery_project(tmp_path/'original.wav',tmp_path,project,segment,lambda *_:None,authorized_missing_timing=True)
    second,_=paired_timing.original_recovery_project(tmp_path/'original.wav',tmp_path,project,segment,lambda *_:None,authorized_missing_timing=True)
    assert frozen==second and len(calls)==1
    assert frozen['timing_rescue']['points'][0]==[1000,110] and frozen['tempo']==project['tempo']
    assert project['timing_map']['uncertain'] and record['source']=='original_native_timing_model'
    # The measured beat can begin after the audio origin: keep its real phase,
    # rather than rejecting it or inventing a zero-second anchor.
    leading,_=paired_timing.original_recovery_project(tmp_path/'original.wav',tmp_path,project,{**segment,'start_sample':0},lambda *_:None,authorized_missing_timing=True)
    assert leading['timing_rescue']['points'][0]==[1000,110]
    assert len(calls)==1 and leading['tempo']==project['tempo']


def test_original_timing_without_observed_beats_cannot_rescue(tmp_path,monkeypatch):
    from malody_studio import beat_analysis, resident
    import numpy as np
    import soundfile as sf
    project,segment,_,_=fixture(3*44100)
    sf.write(tmp_path/'source.wav',np.zeros((project['samples'],2),np.float32),44100,subtype='FLOAT')
    monkeypatch.setattr(resident,'code_version',lambda:'locked-code')
    monkeypatch.setattr(beat_analysis,'v32_timing',lambda *_:{'beat_samples':[],'model_timing_points':[[0,120]]})
    with pytest.raises(ValueError,match='确认 BPM'):
        paired_timing.original_recovery_project(tmp_path/'source.wav',tmp_path,project,segment,lambda *_:None,authorized_missing_timing=True)
    assert list(tmp_path.rglob('*.json'))==[]
