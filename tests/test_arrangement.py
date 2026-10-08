import copy
import hashlib
import numpy as np
import soundfile as sf
import pytest
from malody_studio import arrangement, music_timing, section_plan, adaptive_difficulty
from malody_studio.advanced import defaults, chart_events, beat_value
from malody_studio.charts import Note
from malody_studio.mapperatorinator import serialize_with_timing


def test_full_acoustic_cache_survives_settings_and_tail_policy_changes(tmp_path,monkeypatch):
    from malody_studio import acoustic_evidence
    data=np.tile(np.asarray([1e-7,-1e-7],dtype='float32'),(44100,1))
    sf.write(tmp_path/'source.wav',data,44100,subtype='FLOAT')
    source={'source_pcm_sha256':hashlib.sha256(data.astype('<f4').tobytes()).hexdigest(),'samples':len(data)}
    calls=[]
    def analyze(data):
        calls.append(True)
        return [{'start_sample':0,'end_sample':len(data),'active_fraction':0.,'rms':1e-7,'onset_rate':0.}],np.zeros(200),[],.005
    monkeypatch.setattr(section_plan,'rhythm_features',analyze)
    first=acoustic_evidence.load_or_analyze(tmp_path,source)
    second=acoustic_evidence.load_or_analyze(tmp_path,{**source,'title':'changed','settings':{'rate':8},'tail_trim':{'end_sample':20000}})
    assert first==second and len(calls)==1
    assert first['profile'][0]['exact_silence'] is False
    assert first['profile'][0]['active_fraction']==1


def test_beat_this_reuses_activity_without_legacy_beats_or_cache_overwrite(tmp_path,monkeypatch):
    from malody_studio import acoustic_evidence
    data=np.full((44100,2),.1,np.float32);sf.write(tmp_path/'source.wav',data,44100,subtype='FLOAT')
    project={'source_pcm_sha256':hashlib.sha256(data.astype('<f4').tobytes()).hexdigest(),'samples':len(data)}
    calls=[]
    def analyze(data):
        calls.append(True)
        return [{'start_sample':0,'end_sample':len(data),'active_fraction':1.,'rms':.1,'onset_rate':2.}],np.zeros(200),[.1,.2],.005
    monkeypatch.setattr(section_plan,'rhythm_features',analyze)
    old=acoustic_evidence.load_or_analyze(tmp_path,project)
    old_path=next((tmp_path/'acoustic-evidence').glob('*.json'));before=old_path.read_bytes()
    new=acoustic_evidence.load_or_analyze(tmp_path,project,estimate_beats=False)
    assert old['beat_estimate_samples'] and new['beat_estimate_samples']==[]
    assert new['profile']==old['profile'] and len(calls)==1
    assert old_path.read_bytes()==before and new['id']!=old['id']


def test_empty_beat_this_result_cannot_use_old_librosa_beat_groups():
    evidence,timing,acoustic=evidence_fixture()
    timing=music_timing.timing_map({'beat_samples':[],'provenance':{'adapter':'beat_this_final1'}},timing['source'])
    acoustic={**acoustic,'beat_estimate_samples':list(range(0,36*44100,11025))}
    activity=[{**r,'activity_delta':0.} for r in acoustic['profile']]
    regions,_=arrangement._suggest_regions(timing['source'],timing,activity,acoustic,defaults())
    assert not any('beat_group' in row['cut_reason'] for row in regions)


def evidence_fixture():
    sr=44100;source={'pcm_sha256':'a'*64,'sample_rate':sr,'samples':sr*40,'channels':2,'effective_end_sample':sr*36}
    beats=[round((.2+i*.5)*sr) for i in range(72)]
    timing=music_timing.timing_map({'beat_samples':beats,'downbeat_samples':beats[::4]},source)
    evidence=music_timing.sealed({'schema':music_timing.SCHEMA,'source':source,'policy':{},'candidates':[timing],'selected_timing_id':timing['id']})
    acoustic={'schema':'original-acoustic-v1','source':{k:source[k] for k in ('pcm_sha256','sample_rate','samples')},
        'profile':[{'start_sample':i*sr,'end_sample':(i+1)*sr,'active_fraction':1. if i<36 else 0.,'onset_rate':4.,'exact_silence':i>=36} for i in range(40)],
        'onset_samples':beats,'onset_strengths':[1.]*len(beats)}
    acoustic=music_timing.sealed(acoustic)
    return evidence,timing,acoustic


def test_conserved_bounded_regions_gaps_and_effective_tail():
    evidence,timing,acoustic=evidence_fixture();sr=44100
    regions=[{'start_sample':0,'end_sample':8*sr,'factor':.75},
             {'start_sample':12*sr,'end_sample':36*sr,'factor':1.25}]
    plan=arrangement.build(evidence,timing,acoustic,defaults(),[],regions=regions)
    arrangement.validate(plan,evidence,timing)
    assert len(plan['regions'])==2
    assert all(.75<=row['normalized_factor']<=1.25 for row in plan['regions'])
    sections=plan['sections']
    assert sections[1]['active_seconds']==sections[-1]['active_seconds']==0
    for key,rule in defaults()['difficulty_rules'].items():
        assert sum(s['per_difficulty'][key]['target_heads_soft'] for s in sections)==pytest.approx(32*rule['rate'],abs=.001)


def test_native_arrangement_keeps_actual_heads_holds_and_phrase_deficit():
    evidence,timing,acoustic=evidence_fixture();settings=defaults()
    plan=arrangement.build(evidence,timing,acoustic,settings,[])['section_plan']
    # A deficit in an empty earlier phrase cannot be spent in this dense group.
    # A sustained voice attacks on the measured 3200 ms pulse. Other voices
    # remain on different lanes until its real release; the old fixture put
    # repeated attacks inside the same LN and depended on a hold-type bonus.
    raw=[Note(3000+i*40,0 if i==5 else 1+i%3,3600 if i==5 else None) for i in range(20)]
    before=copy.deepcopy(raw)
    candidates=adaptive_difficulty.model_candidates(raw,acoustic,timing)
    from malody_studio.quality_workflow import candidate_contract
    policy=candidate_contract()
    notes,report=adaptive_difficulty.calibrate_adaptive(candidates,40000,'easy',0,42,plan=plan,
        selection_policy=policy['selection_policy'],audio_vote_cap=policy['audio_vote_cap'])
    model_times={r.start for r in raw}
    assert 0<len(notes)<len(raw)
    assert {n.start for n in notes}<=model_times
    assert raw==before and all(candidate[2] for candidate in candidates)
    assert report['candidate_acoustic_heads']==report['selected_acoustic_heads']==0
    assert report['selected_model_heads']==len(notes)
    assert all(any(n.start==r.start and n.end==r.end for r in raw) for n in notes)
    assert any(n.start==3200 and n.end==3600 and n.lane==0 for n in notes)
    assert report['budget_passes']==1
    phrases=report['section_stats'][0]['phrase_stats']
    empty=next(row for row in phrases if row['range_ms']==[200.,2200.])
    dense=next(row for row in phrases if row['range_ms']==[2200.,4200.])
    assert empty['target_heads']==empty['unspent_heads']==5
    assert empty['candidate_heads']==empty['selected_heads']==0
    assert dense['candidate_heads']==len(raw)==20
    assert dense['target_heads']==dense['selected_heads']==len(notes)==5
    assert dense['unspent_heads']==0
    assert all(row['redistributed'] is False for row in phrases)
    omitted=[decision for decision in report['decisions'] if decision['type']=='omitted']
    assert len(omitted)==len(raw)-len(notes)==15
    assert all(decision['reason_category']=='soft_selection'
               and decision['reason']=='frozen_phrase_budget_whole_candidate'
               and decision['hard_conflicts']==[]
               and decision['candidate_provenance']['kind']=='model' for decision in omitted)


def test_rule_revision_preserves_valid_lanes_even_after_long_unbalanced_phrase():
    # Soft balance must not rewrite a legal model phrase during ordinary rules.
    raw=[Note(i*200.,0) for i in range(600)]
    rule=defaults()['difficulty_rules']['hard']
    notes,report=adaptive_difficulty.repair_playable(raw,120000.,rule,'balanced')
    assert notes==raw and report['decisions']==[]
    # A true hold collision may move the attack, with its reason recorded.
    notes,report=adaptive_difficulty.repair_playable([Note(0.,0,800.),Note(200.,0)],1000.,rule)
    assert notes[1].lane!=0
    assert report['decisions'][0]['reason']=='user_hard_caps'


@pytest.mark.parametrize('meter,phase',[(3,137.123),(4,200.25),(7,487.5)])
def test_nonzero_bar_phase_roundtrip_in_classic_chart(meter,phase):
    raw=[Note(12.75,0,487.25),Note(phase,1)]
    chart=serialize_with_timing(raw,'title','artist','hard',[(phase,120)],meter=meter,phase_ms=phase)
    restored=chart_events(chart)
    assert all(abs(a.start-b['start_ms'])<1 for a,b in zip(raw,restored))
    assert abs(restored[0]['end_ms']-raw[0].end)<1
    assert float(beat_value(chart['note'][1]['beat']))%meter==pytest.approx(0,abs=1e-5)
    assert chart['meta']['mode_ext']['column']==4


def region_fixture(seconds=180):
    sr=44100
    source={'pcm_sha256':'b'*64,'sample_rate':sr,'samples':sr*(seconds+5),'channels':2,'effective_end_sample':sr*seconds}
    beats=[sr+i*(sr//2) for i in range((seconds-1)*2)]
    timing=music_timing.timing_map({'beat_samples':beats,'downbeat_samples':[]},source)
    evidence=music_timing.sealed({'schema':music_timing.SCHEMA,'source':source,'policy':{},'candidates':[timing],'selected_timing_id':timing['id']})
    acoustic=music_timing.sealed({'schema':'original-acoustic-v1','source':{k:source[k] for k in ('pcm_sha256','sample_rate','samples')},
        'profile':[{'start_sample':i*sr,'end_sample':(i+1)*sr,'active_fraction':float(i<seconds),'onset_rate':4.,'exact_silence':i>=seconds}
                   for i in range(seconds+5)],'onset_samples':beats,'onset_strengths':[1.]*len(beats)})
    return evidence,timing,acoustic


def test_automatic_regions_merge_pickup_short_tail_and_preserve_original_clock():
    evidence,timing,acoustic=region_fixture(181);before=copy.deepcopy((evidence,timing,acoustic))
    plan=arrangement.build(evidence,timing,acoustic,defaults(),[])
    stop=evidence['source']['effective_end_sample'];sr=44100
    rows=[r for r in plan['regions'] if r['start_sample']<stop]
    assert len(rows)<20 and all(r['end_sample']-r['start_sample']>=8*sr for r in rows)
    assert rows[0]['start_sample']==0 and rows[-1]['end_sample']==stop
    assert not any(r['end_sample']==sr for r in rows)  # one-second pickup retained within a phrase
    assert plan['sections'][-1]['active_seconds']==0
    assert (evidence,timing,acoustic)==before
    assert sorted(x for section in plan['sections'] for x in section['beat_samples'])==timing['beat_samples']


def test_region_limit_and_granularity_only_rederive_plan_not_evidence():
    evidence,timing,acoustic=region_fixture(300)
    fine=arrangement.build(evidence,timing,acoustic,{**defaults(),'region_granularity':'fine','region_max_count':50},[])
    coarse=arrangement.build(evidence,timing,acoustic,{**defaults(),'region_granularity':'coarse','region_max_count':50},[])
    capped=arrangement.build(evidence,timing,acoustic,{**defaults(),'region_max_count':7},[])
    assert len(fine['regions'])>len(coarse['regions'])>len(capped['regions'])
    assert len(capped['regions'])==8  # seven audible regions plus the excluded tail
    assert any(row['reason']=='region_count_limit' for row in capped['region_policy']['merges'])
    assert {plan['timing_map_id'] for plan in (fine,coarse,capped)}=={timing['id']}
    for plan in (fine,coarse,capped):
        assert sum(row['per_difficulty']['hard']['target_heads_soft'] for row in plan['sections'])==pytest.approx(300*defaults()['difficulty_rules']['hard']['rate'])


def test_short_activity_jitter_does_not_create_display_fragments_but_sustained_change_does():
    evidence,timing,acoustic=region_fixture(180)
    profile=acoustic['profile']
    for i,row in enumerate(profile):row['onset_rate']=0. if i%2 else 12.
    acoustic=music_timing.sealed(acoustic)
    jitter=arrangement.build(evidence,timing,acoustic,defaults(),[])
    assert not any(r['cut_reason']=='sustained_activity_change' for r in jitter['regions'])
    for i,row in enumerate(profile):row['onset_rate']=0. if i<90 else 12.
    acoustic=music_timing.sealed({**acoustic,'profile':profile})
    contrast=arrangement.build(evidence,timing,acoustic,defaults(),[])
    assert any(r['cut_reason']=='sustained_activity_change' for r in contrast['regions'])


def test_explicit_short_regions_not_merged_by_automatic_limit():
    evidence,timing,acoustic=region_fixture()
    regions=[{'start_sample':0,'end_sample':44100},{'start_sample':44100,'end_sample':2*44100}]
    plan=arrangement.build(evidence,timing,acoustic,{**defaults(),'region_max_count':1},[],regions=regions)
    assert [(r['start_sample'],r['end_sample']) for r in plan['regions']]==[(0,44100),(44100,88200)]


def test_bpm_jitter_does_not_split_regions_but_larger_measured_change_has_priority():
    evidence,timing,acoustic=region_fixture(180)
    timing=music_timing.timing_map({'beat_samples':timing['beat_samples'],'downbeat_samples':timing['beat_samples'][::4]},evidence['source'])
    # Valid anchored timing points exercise display policy, independently of
    # detector accuracy. Every point must survive in the frozen TimingMap.
    anchors=timing['downbeat_samples']
    points=[{'sample':anchors[0],'bpm':120.,'meter':4,'confirmed':True},
            {'sample':anchors[15],'bpm':122.,'meter':4,'confirmed':True},
            {'sample':anchors[45],'bpm':145.,'meter':4,'confirmed':True}]
    timing=music_timing.sealed({**timing,'tempo_points':points})
    evidence=music_timing.sealed({**evidence,'candidates':[timing],'selected_timing_id':timing['id']})
    plan=arrangement.build(evidence,timing,acoustic,defaults(),[])
    assert not any(r['start_sample']==anchors[15] for r in plan['regions'])
    assert any(r['start_sample']==anchors[45] and r['cut_reason']=='measured_tempo_change' for r in plan['regions'])
    assert timing['tempo_points']==points


def test_all_silence_and_short_track_still_form_valid_single_region():
    evidence,timing,acoustic=region_fixture(2)
    plan=arrangement.build(evidence,timing,acoustic,defaults(),[])
    assert len(plan['regions'])==2  # a short whole song plus the excluded tail
    assert plan['regions'][0]['end_sample']==2*44100
    source={**evidence['source'],'effective_end_sample':0}
    timing=music_timing.timing_map({'beat_samples':[],'downbeat_samples':[]},source)
    evidence=music_timing.sealed({**evidence,'source':source,'candidates':[timing],'selected_timing_id':timing['id']})
    acoustic=music_timing.sealed({**acoustic,'profile':[{**r,'active_fraction':0.,'onset_rate':0.,'exact_silence':True} for r in acoustic['profile']]})
    plan=arrangement.build(evidence,timing,acoustic,defaults(),[])
    assert len(plan['regions'])==1 and plan['sections'][0]['active_seconds']==0


@pytest.mark.parametrize('key,value',[('region_granularity','huge'),('region_max_count',False),('region_max_count',0),('region_max_count',101),('region_max_count',4.5)])
def test_invalid_region_settings_rejected(key,value):
    from malody_studio.advanced import validate_settings
    with pytest.raises(ValueError):validate_settings({key:value})
