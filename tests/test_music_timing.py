import copy
import hashlib
import numpy as np
import pytest
import soundfile as sf
from malody_studio import music_timing as mt

def fixture_source(tmp_path):
    data=np.zeros((44100*24,2),np.float32);data[:44100*20]=.1
    sf.write(tmp_path/'source.wav',data,44100,subtype='FLOAT')
    return {'samples':len(data),'sample_rate':44100,'source_pcm_sha256':hashlib.sha256(data.astype('<f4').tobytes()).hexdigest()}

def test_full_pcm_hash_and_effective_end_are_separate(tmp_path):
    project=fixture_source(tmp_path);data,source=mt.source_contract(tmp_path,project)
    assert source['samples']==44100*24 and source['effective_end_sample']==44100*20
    assert source['pcm_sha256']==project['source_pcm_sha256']
    data[-1,0]=1e-9;sf.write(tmp_path/'source.wav',data,44100,subtype='FLOAT')
    with pytest.raises(ValueError,match='PCM'):mt.source_contract(tmp_path,project)

def test_scalar_or_beats_without_downbeat_never_invents_zero_phase_meter():
    source={'sample_rate':1000,'samples':24000,'effective_end_sample':24000}
    raw={'beat_samples':list(range(137,23000,500)),'estimated_bpm':120}
    result=mt.timing_map(raw,source)
    assert result['phase_sample'] is None and result['meter'] is None
    assert result['tempo_points']==[] and not result['eligibility']['v32']

def test_measured_phase_is_multi_beat_fit_with_provenance():
    source={'sample_rate':1000,'samples':24000,'effective_end_sample':24000}
    beats=np.arange(137,23000,500);beats[0]+=20
    result=mt.timing_map({'beat_samples':beats.tolist(),'downbeat_samples':beats[::4].tolist()},source)
    assert result['meter']==4 and result['eligibility']['v32']
    assert abs(result['phase_sample']-137)<4
    assert result['observed_downbeat_samples'][0]==157
    assert mt.validate_timing(result,source)==result
    result['phase_sample']=0
    with pytest.raises(ValueError):mt.validate_timing(result,source)

def test_ideal_true_tempo_change_inside_old_window_has_continuous_map():
    sr=44100;beats=np.rint(np.r_[np.arange(.137,18,.5),np.arange(18.137,36,.4)]*sr).astype(int)
    source={'sample_rate':sr,'samples':sr*36,'effective_end_sample':sr*36}
    result=mt.timing_map({'beat_samples':beats.tolist(),'downbeat_samples':beats[::4].tolist()},source)
    assert result['eligibility']['v32'] and len(result['tempo_points'])==2
    assert abs(result['tempo_points'][0]['bpm']-120)<.01
    assert abs(result['tempo_points'][1]['bpm']-150)<.01
    assert abs(result['tempo_points'][1]['sample']/sr-18.137)<1/sr
    assert mt.validate_timing(result)==result

def test_evidence_cache_ignores_metadata_and_difficulty_and_returns_copies(tmp_path,monkeypatch):
    project=fixture_source(tmp_path);monkeypatch.setattr(mt,'ROOT',tmp_path)
    monkeypatch.setattr(mt,'selected_policy',lambda:{'adapter':'librosa','detector_approved':False,'strong_reference_approved':False})
    calls=[]
    def fake(data,sr):calls.append(len(data));return {'beat_samples':[x for x in range(4410,len(data),22050)],'downbeat_samples':[]}
    monkeypatch.setattr(mt.beat_analysis,'librosa_beats',fake)
    first=mt.load_or_analyze(tmp_path,project)
    project.update(title='new',settings={'difficulty':'lunatic'},revision=999)
    second=mt.load_or_analyze(tmp_path,project)
    assert len(calls)==1 and calls[0]==44100*20 and first['id']==second['id']
    second['candidates'][0]['beat_samples'].clear()
    assert mt.load_or_analyze(tmp_path,project)['candidates'][0]['beat_samples']

def test_disagreement_selection_is_sealed_member_and_cannot_block_preview(tmp_path,monkeypatch):
    project=fixture_source(tmp_path);monkeypatch.setattr(mt,'ROOT',tmp_path)
    deployment=tmp_path/'deployment.json';deployment.write_text('frozen-deployment')
    monkeypatch.setattr(mt.beat_analysis,'DEPLOYMENT',deployment)
    monkeypatch.setattr(mt,'selected_policy',lambda:{'adapter':'beat_this_final0','accepted':True,'independent_v32_review':True,
        'deployment_manifest_hash':hashlib.sha256(deployment.read_bytes()).hexdigest()})
    raw={'beat_samples':list(range(4410,44100*20,22050)),'downbeat_samples':list(range(4410,44100*20,88200)),'provenance':{'adapter':'beat_this_final0'}}
    monkeypatch.setattr(mt.beat_analysis,'librosa_beats',lambda *a,**k:{'beat_samples':[],'downbeat_samples':[]})
    monkeypatch.setattr(mt.beat_analysis,'beat_this',lambda *a,**k:raw)
    monkeypatch.setattr(mt.beat_analysis,'v32_timing',lambda *a,**k:{'beat_samples':[],'downbeat_samples':[],'provenance':{'adapter':'v32_ordinary'}})
    evidence=mt.load_or_analyze(tmp_path,project);timing=mt.select_timing(evidence)
    assert timing['id']==evidence['selected_timing_id']
    assert timing['id'] in {x['id'] for x in evidence['candidates']}
    assert not timing['eligibility']['v32'] and timing['uncertain']
    assert mt.validate_timing(timing)==timing
    cached=mt.load_or_analyze(tmp_path,project)
    assert cached['id']==evidence['id']
    assert 'v32_ordinary' in {row['provenance'].get('adapter') for row in cached['candidates']}

def test_incomplete_evidence_cannot_select_new_unstored_identity():
    source={'sample_rate':1000,'samples':24000,'effective_end_sample':24000}
    candidate=mt.timing_map({'beat_samples':list(range(137,23000,500))},source)
    evidence=mt.sealed({'schema':mt.SCHEMA,'version':'shared-original-clock-3',
        'source':source,'candidates':[candidate],'policy':{'adapter':'librosa'}})
    with pytest.raises(ValueError,match='版本缺失'):mt.select_timing(evidence)

def test_detector_selection_does_not_approve_strong_reference(tmp_path,monkeypatch):
    import json
    selection=tmp_path/'selection.json'
    selection.write_text(json.dumps({'adapter':'beat_this_ensemble','detector_approved':True}),encoding='utf-8')
    monkeypatch.setattr(mt,'SELECTION',selection)
    policy=mt.selected_policy()
    assert policy['adapter']=='beat_this_ensemble' and policy['detector_approved']
    assert policy['strong_reference_approved'] is False

def test_frozen_analysis_policy_survives_new_selection_and_rejects_deployment_change(tmp_path,monkeypatch):
    project=fixture_source(tmp_path);monkeypatch.setattr(mt,'ROOT',tmp_path)
    deployment=tmp_path/'deployment.json';deployment.write_text('first-deployment')
    monkeypatch.setattr(mt.beat_analysis,'DEPLOYMENT',deployment)
    frozen={'adapter':'beat_this_final1','detector_approved':True,'strong_reference_approved':False,
            'deployment_manifest_hash':hashlib.sha256(deployment.read_bytes()).hexdigest()}
    monkeypatch.setattr(mt,'selected_policy',lambda:{'adapter':'librosa'})
    monkeypatch.setattr(mt.beat_analysis,'librosa_beats',lambda *a,**k:{'beat_samples':[],'downbeat_samples':[]})
    calls=[]
    def inference(*a,**k):
        calls.append(a[2]);return {'beat_samples':[],'downbeat_samples':[], 'provenance':{'adapter':'beat_this_final1'}}
    monkeypatch.setattr(mt.beat_analysis,'beat_this',inference)
    evidence=mt.load_or_analyze(tmp_path,project,frozen_policy=frozen)
    assert evidence['policy']==frozen and calls==[('final1',)]
    deployment.write_text('changed-deployment')
    with pytest.raises(ValueError,match='部署版本已改变'):mt.load_or_analyze(tmp_path,project,frozen_policy=frozen)


@pytest.fixture
def beat_this_source(tmp_path,monkeypatch):
    project=fixture_source(tmp_path);monkeypatch.setattr(mt,'ROOT',tmp_path)
    deployment=tmp_path/'deployment.json';deployment.write_text('existing-deployment')
    monkeypatch.setattr(mt.beat_analysis,'DEPLOYMENT',deployment)
    policy={'adapter':'beat_this_final1','detector_approved':True,'strong_reference_approved':False,
            'deployment_manifest_hash':hashlib.sha256(deployment.read_bytes()).hexdigest()}
    monkeypatch.setattr(mt,'selected_policy',lambda:policy)
    def forbidden(*args,**kwargs):raise AssertionError('Beat This must not silently run librosa')
    monkeypatch.setattr(mt.beat_analysis,'librosa_beats',forbidden)
    raw={'beat_samples':list(range(4410,44100*20,22050)),
         'downbeat_samples':list(range(4410,44100*20,88200)),
         'provenance':{'adapter':'beat_this_final1'}}
    return project,policy,raw


def test_beat_this_is_the_actual_detector_and_does_not_invoke_librosa(beat_this_source,tmp_path,monkeypatch):
    import librosa
    project,policy,raw=beat_this_source;calls=[]
    def forbidden(*args,**kwargs):raise AssertionError('Legacy tempo estimation must not run')
    monkeypatch.setattr(librosa.feature,'tempo',forbidden)
    monkeypatch.setattr(librosa.beat,'beat_track',forbidden)
    def detector(*args,**kwargs):calls.append(args[2]);return raw
    monkeypatch.setattr(mt.beat_analysis,'beat_this',detector)
    evidence=mt.load_or_analyze(tmp_path,project);timing=mt.select_timing(evidence)
    assert calls==[('final1',)]
    assert {row['provenance']['adapter'] for row in evidence['candidates']}=={'beat_this_final1'}
    assert timing['provenance']['adapter']=='beat_this_final1' and timing['beat_samples']
    assert timing['eligibility']['beat'] and not timing['eligibility']['v32']
    assert evidence['analysis']['failures']==[]
    assert evidence['acoustic']['beat_estimate_samples']==[]


def test_beat_this_failure_is_reported_without_publishing_fallback(beat_this_source,tmp_path,monkeypatch):
    project,_,_=beat_this_source
    def detector(*args,**kwargs):raise RuntimeError('Beat This inference failed')
    monkeypatch.setattr(mt.beat_analysis,'beat_this',detector)
    with pytest.raises(RuntimeError,match='Beat This inference failed'):
        mt.load_or_analyze(tmp_path,project)
    assert not list((tmp_path/'cache/music-evidence').glob('*.json'))


def test_old_librosa_fallback_cache_is_rebuilt_with_selected_detector(beat_this_source,tmp_path,monkeypatch):
    import json
    project,policy,raw=beat_this_source;_,source=mt.source_contract(tmp_path,project)
    fallback=mt.timing_map({'beat_samples':raw['beat_samples'],'provenance':{'adapter':'librosa'}},source)
    old=mt.sealed({'schema':mt.SCHEMA,'version':mt.VERSION,'source':source,'policy':policy,
        'candidates':[fallback],'selected_timing_id':fallback['id'],'analysis':{'failures':[{'adapter':'beat_this_final1'}]}})
    key=mt.digest({'version':mt.VERSION,'source':source,'policy':policy})
    path=tmp_path/'cache/music-evidence'/(key+'.json');path.parent.mkdir(parents=True)
    path.write_text(json.dumps(old),encoding='utf-8')
    before=path.read_bytes()
    monkeypatch.setattr(mt.beat_analysis,'beat_this',lambda *args,**kwargs:raw)
    result=mt.load_or_analyze(tmp_path,project)
    assert mt.select_timing(result)['provenance']['adapter']=='beat_this_final1'
    assert result['analysis']['failures']==[]
    assert path.read_bytes()==before


def test_missing_default_selection_does_not_become_librosa(tmp_path,monkeypatch):
    monkeypatch.setattr(mt,'SELECTION',tmp_path/'missing.json')
    with pytest.raises(RuntimeError,match='不会退回 librosa'):
        mt.selected_policy()


def test_real_historic_beat_this_cache_reuses_beats_without_old_acoustic_beats(beat_this_source,tmp_path,monkeypatch):
    import json
    from malody_studio import acoustic_evidence
    project,policy,raw=beat_this_source
    data,source=mt.source_contract(tmp_path,project)
    acoustic=acoustic_evidence.load_or_analyze(tmp_path,project,data=data,source=source,estimate_beats=False)
    old_acoustic={k:v for k,v in acoustic.items() if k not in ('id','hash')}
    old_acoustic['beat_estimate_samples']=[12345]
    old_acoustic['provenance']={**old_acoustic['provenance'],'beat_estimator':'legacy'}
    identity=mt.digest(old_acoustic)
    old_acoustic={**old_acoustic,'id':identity,'hash':identity}
    candidate=mt.timing_map(raw,source)
    legacy_candidate=mt.timing_map({'beat_samples':[12345],'provenance':{'adapter':'librosa'}},source)
    old=mt.sealed({'schema':mt.SCHEMA,'version':mt.VERSION,'source':source,'policy':policy,
                  'candidates':[candidate,legacy_candidate],'selected_timing_id':candidate['id'],
                  'acoustic':old_acoustic,'analysis':{'failures':[]}})
    key=mt.digest({'version':mt.VERSION,'source':source,'policy':policy})
    path=tmp_path/'cache/music-evidence'/(key+'.json');path.parent.mkdir(parents=True)
    path.write_text(json.dumps(old),encoding='utf-8');before=path.read_bytes()
    def forbidden(*a,**k):raise AssertionError('Valid actual Beat This observations should be reused')
    monkeypatch.setattr(mt.beat_analysis,'beat_this',forbidden)
    result=mt.load_or_analyze(tmp_path,project)
    assert mt.select_timing(result)['beat_samples']==candidate['beat_samples']
    assert result['acoustic']['beat_estimate_samples']==[]
    assert {row['provenance']['adapter'] for row in result['candidates']}=={'beat_this_final1'}
    assert result['analysis']['detector_execution']=='beat-this-primary-v1'
    assert path.read_bytes()==before and result['id']!=old['id']
