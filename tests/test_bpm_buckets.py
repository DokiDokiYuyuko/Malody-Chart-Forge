import copy
import hashlib
import json
from pathlib import Path
import numpy as np
import pytest
import soundfile as sf
from malody_studio import advanced, arrangement, bpm_buckets, music_timing as mt, section_plan as sp
from malody_studio import advanced_generation as gen, mapperatorinator
from malody_studio.charts import Note, package
from malody_studio.generation_context import contract


def timing_for(bpms,beats_per_part=32,sr=44100):
    samples=[0];cursor=0.
    for bpm in bpms:
        for _ in range(beats_per_part):
            cursor+=60*sr/bpm;samples.append(round(cursor))
    source={'pcm_sha256':'a'*64,'sample_rate':sr,'samples':samples[-1]+sr,
            'effective_end_sample':samples[-1]+sr,'channels':2}
    return mt.timing_map({'beat_samples':samples,'downbeat_samples':[],
                         'provenance':{'adapter':'beat_this_final1'}},source)


def arrangement_for(timing,settings,regions=None,bucket_version=None):
    source=timing['source'];sr=source['sample_rate']
    evidence=mt.sealed({'schema':mt.SCHEMA,'source':source,'policy':{},
                        'candidates':[timing],'selected_timing_id':timing['id']})
    acoustic=mt.sealed({'schema':'original-acoustic-v1','source':{k:source[k] for k in ('pcm_sha256','sample_rate','samples')},
        'profile':[{'start_sample':i*sr,'end_sample':min(source['samples'],(i+1)*sr),
                    'active_fraction':1.,'onset_rate':8.,'exact_silence':False}
                   for i in range(int(np.ceil(source['samples']/sr)))],
        'onset_samples':[],'onset_strengths':[],'beat_estimate_samples':[]})
    return arrangement.build(evidence,timing,acoustic,settings,[],regions=regions,bucket_version=bucket_version)['section_plan']


def test_twenty_to_three_hundred_bpm_uses_at_most_five_buckets_and_middle_is_base():
    timing=timing_for([20,40,60,80,100,120,160,200,260,300])
    policy=bpm_buckets.build(timing,advanced.defaults())
    assert policy['count']==5 and len(policy['segments'])<=10
    assert policy['baseline_bucket_id']==2
    factors=[row['factor'] for row in policy['buckets']]
    assert factors==pytest.approx([.8,.9,1.,1.1,1.2])
    assert policy['segments'][0]['bucket_id']<policy['segments'][-1]['bucket_id']
    assert abs(policy['segments'][0]['bpm']-20)<.1
    assert abs(policy['observed_segments'][-1]['bpm']-300)<1


def test_ten_thousand_jittered_estimates_do_not_make_ten_thousand_classes():
    timing=timing_for(120+np.sin(np.arange(10000))*.2,beats_per_part=1)
    policy=bpm_buckets.build(timing,advanced.defaults())
    assert policy['count']==1 and len(policy['segments'])==1
    assert policy['buckets'][0]['factor']==1.


def test_half_double_layers_remain_useful_for_difficulty_without_certifying_meter():
    timing=timing_for([110,220,110])
    assert not timing['eligibility']['v32']
    policy=bpm_buckets.build(timing,advanced.defaults())
    assert policy['count']==2 and [row['bucket_id'] for row in policy['segments']]==[0,1,0]
    assert policy['buckets'][0]['factor']==1 and policy['buckets'][1]['factor']==pytest.approx(1.2)


@pytest.mark.parametrize('field,value',[('bpm_bucket_count',0),('bpm_bucket_count',10),
                                     ('bpm_bucket_count',True),('bpm_bucket_range',.3)])
def test_invalid_bucket_settings_are_rejected(field,value):
    with pytest.raises(ValueError):advanced.validate_settings({**advanced.defaults(),field:value})


def test_bucket_targets_conditions_and_actual_core_boundaries_share_one_policy():
    settings=advanced.defaults();timing=timing_for([80,120,180])
    plan=arrangement_for(timing,settings)
    assert plan['bpm_buckets']['count']==3
    for section in plan['sections']:
        factor=section['bpm_bucket_factor']
        for key in ('expert','master'):
            detail=section['per_difficulty'][key]
            assert detail['target_rate']==pytest.approx(settings['difficulty_rules'][key]['rate']*factor)
            assert detail['target_heads_soft']==pytest.approx(detail['target_rate']*section['active_seconds'])
            if section['bpm_bucket_id']==1:assert detail['model_condition']==settings['conditions']['v32'][key]
    assert {section['bpm_bucket_id'] for section in plan['sections']} >= {0,1,2}
    assert any(section['per_difficulty']['expert']['model_condition']<5.9 for section in plan['sections'])
    assert any(section['per_difficulty']['expert']['model_condition']>5.9 for section in plan['sections'])
    for row in plan['sections']:
        overlap=[segment for segment in plan['bpm_buckets']['segments']
                 if segment['range'][0]<row['core'][1] and segment['range'][1]>row['core'][0]]
        assert len(overlap)<=1


def test_dynamic_off_has_no_density_or_condition_floating():
    settings={**advanced.defaults(),'dynamic_enabled':False}
    plan=arrangement_for(timing_for([80,120,180]),settings)
    for section in plan['sections']:
        assert section['normalized_factor']==1
        assert section['per_difficulty']['expert']['model_condition']==5.9
        assert section['per_difficulty']['expert']['target_rate']==13


def test_actual_bucket_conditions_reach_worker_even_in_continuous_parent(tmp_path,monkeypatch):
    source=tmp_path/'source.wav';timing=timing_for([80,120,180],beats_per_part=16)
    data=np.full((timing['source']['samples'],2),.1,np.float32)
    sf.write(source,data,44100,subtype='FLOAT')
    settings=advanced.defaults();plan=arrangement_for(timing,settings)
    pcm=hashlib.sha256(data.astype('<f4').tobytes()).hexdigest()
    body={k:v for k,v in plan.items() if k not in ('id','content_hash')};body['source_pcm_sha']=pcm
    digest=sp.canonical_hash(body);plan={**body,'id':digest,'content_hash':digest}
    calls=[]
    def infer(source,folder,options,progress):
        calls.extend(copy.deepcopy(options['_advanced_presets']))
        return {row['key']:([Note(row['core'][0]*1000/sp.SR+100,0)],[[0,110],[1000,220]])
                for row in options['_advanced_presets']},{}
    monkeypatch.setattr(mapperatorinator,'generate',infer)
    snapshot={'project':{'id':'a'*32,'title':'test','artist':'test','tempo':{'bpm':110,'uncertain':True}},
              'segment':{'id':'b'*32,'start_sample':0,'end_sample':len(data)},
              'variants':[{'key':'balanced--expert','pattern':'balanced','difficulty':'expert'}],
              'settings':settings,'section_plan':plan,'_stem_raw_only':True,
              'disable_density_retry':True,'generation_context_policy':contract()}
    result=gen.run(source,tmp_path/'generated',{'_advanced':snapshot},lambda *_:None)
    assert {round(row['sr'],4) for row in calls}=={5.3,5.9,6.5}
    assert len(calls)<=len(plan['sections'])
    records=result['advanced_result'][0]['provenance']['core_conditions']
    assert all(row['selected_condition']==row['regional_plan_condition'] for row in records)
    assert all(row['condition_rationale']=='bpm_bucket_actual_condition' for row in records)
    assert result['advanced_result'][0]['provenance']['serialization_tempo']['points']==[[0.,120.]]


def test_equal_bucket_neighbors_coalesce_but_different_buckets_never_do():
    rows=[{'key':str(i),'difficulty_key':'expert','core':[i*44100,(i+1)*44100],
           'context':[0,3*44100],'start_time':i*1000,'end_time':(i+1)*1000,
           'sr':5.9,'bpm_bucket_id':bucket,'seed':42,'section_id':str(i)}
          for i,bucket in enumerate((1,1,2))]
    requests,owners=gen._coalesce_v32_requests(rows,tolerance=1.)
    assert len(requests)==2
    assert owners['0']['key']==owners['1']['key']!=owners['2']['key']


def test_fixed_scroll_package_roundtrip_keeps_heads_and_cross_segment_hold(tmp_path):
    # Musical BPM can take arbitrarily many values; the MC delivery clock is
    # fixed and independent. A hold spanning any of those changes keeps its ms.
    notes=[Note(100.125,0,20000.75),Note(3050.375,1),Note(17000.125,2)]
    chart=mapperatorinator.serialize_fixed_scroll(notes,'test','artist','expert')
    assert chart['time']==[{'beat':[0,0,1],'bpm':120.}]
    assert chart['effect']==[]
    restored=advanced.chart_events(chart)
    assert len(restored)==len(notes)
    for note,event in zip(notes,restored):
        assert event['start_ms']==pytest.approx(note.start,abs=.3)
        if note.end is not None:assert event['end_ms']==pytest.approx(note.end,abs=.3)


# --- bpm-buckets-v3 ---------------------------------------------------------
# Replay fixtures hold only the saved Beat This beat_samples (no audio) of real projects.
FIXTURES=Path(__file__).parent/'fixtures'/'bpm_buckets'
V2,V3=bpm_buckets.VERSION_V2,bpm_buckets.VERSION_V3
SR=44100


def replay(name):
    data=json.loads((FIXTURES/(name+'.json')).read_text(encoding='utf-8'))
    end=data['source']['effective_end_sample']
    source={'pcm_sha256':'a'*64,'sample_rate':data['source']['sample_rate'],'samples':end,
            'effective_end_sample':end,'channels':2}
    return mt.timing_map({'beat_samples':data['beat_samples'],'downbeat_samples':[],
                          'provenance':{'adapter':'beat_this_final1'}},source)


def parts(*rows):
    """Piecewise-constant synthetic beats: (bpm, beat_count) rows."""
    samples=[0];cursor=0.
    for bpm,count in rows:
        for _ in range(count):
            cursor+=60*SR/bpm;samples.append(round(cursor))
    source={'pcm_sha256':'a'*64,'sample_rate':SR,'samples':samples[-1],'effective_end_sample':samples[-1],'channels':2}
    return mt.timing_map({'beat_samples':samples,'downbeat_samples':[],
                          'provenance':{'adapter':'beat_this_final1'}},source)


def seconds(row):return (row['range'][1]-row['range'][0])/SR


def test_default_estimator_is_the_frozen_v2_and_versions_are_explicit():
    assert bpm_buckets.VERSION==V3 and bpm_buckets.KNOWN_VERSIONS==(V2,V3)
    timing=timing_for([100,120])
    assert bpm_buckets.build(timing,advanced.defaults())['version']==V2
    assert bpm_buckets.build(timing,advanced.defaults(),V3)['version']==V3
    with pytest.raises(ValueError):bpm_buckets.build(timing,advanced.defaults(),'bpm-buckets-v9')


def test_v2_replay_of_saved_beats_is_unchanged():
    gem=bpm_buckets.build(replay('gem_13'),advanced.defaults())
    assert [round(b['bpm'],1) for b in gem['buckets']]==[105.3,115.3,133.9,232.1,340.9]
    assert [(round(r['range'][0]/SR,1),round(r['bpm'],1),r['bucket_id']) for r in gem['observed_segments']]==[
        (0.0,115.4,1),(68.6,133.9,2),(72.5,115.1,1),(81.7,340.9,4),(85.5,116.1,1),
        (116.4,230.8,3),(119.4,113.6,1),(144.8,105.3,0),(177.0,232.2,3),(208.5,116.0,1)]
    # The middle-rank baseline was the 3.9 s blip, not the dominant tempo.
    assert gem['baseline_bucket_id']==2 and gem['baseline_rule']=='middle_ordered_bucket_lower_when_even'
    assert 'observed_bpm' not in gem['segments'][0]
    witch=bpm_buckets.build(replay('witch'),advanced.defaults())
    assert [round(b['bpm'],1) for b in witch['buckets']]==[107.5,214.9]
    assert [round(r['range'][0]/SR,1) for r in witch['observed_segments']]==[0.0,144.7,149.3,228.4,239.2]


def test_v3_gem_song_is_one_tempo_without_the_spurious_341_or_blip_baseline():
    policy=bpm_buckets.build(replay('gem_13'),advanced.defaults(),V3)
    assert policy['count']==1 and len(policy['segments'])==1
    assert policy['buckets'][0]['bpm']==pytest.approx(114.5,abs=.3)
    assert policy['baseline_bucket_id']==0 and policy['baseline_rule']=='dominant_duration_bucket'
    assert all(seconds(row)>=8 and row['interval_count']>=16 for row in policy['observed_segments'])
    assert max(row['bpm'] for row in policy['observed_segments'])<250
    assert all(np.isfinite(row['bpm']) and np.isfinite(row['observed_bpm']) for row in policy['observed_segments'])
    assert policy['buckets'][0]['offset']==0 and policy['buckets'][0]['factor']==1.


def test_v3_witch_song_loses_the_doubled_pulse_bucket():
    policy=bpm_buckets.build(replay('witch'),advanced.defaults(),V3)
    assert policy['count']==1 and policy['buckets'][0]['bpm']==pytest.approx(107.4,abs=.3)


@pytest.mark.parametrize('name,low,high',[('teikoku',118.1,142.2),('machine_voice',90.0,103.1)])
def test_v3_keeps_two_tempo_regimes_that_last_long_enough(name,low,high):
    policy=bpm_buckets.build(replay(name),advanced.defaults(),V3)
    assert [round(b['bpm'],1) for b in policy['buckets']]==[low,high]
    assert all(seconds(row)>=8 and row['interval_count']>=16 for row in policy['segments'])
    assert policy['baseline_bucket_id']==0 and [b['offset'] for b in policy['buckets']]==[0,1]


def test_empty_support_window_no_longer_produces_nan_rates():
    # Two intervals, neither within 12% of their median: v2 averaged an empty set.
    assert np.isfinite(bpm_buckets._local_rates(np.asarray([.5,.9]))).all()
    assert np.isfinite(bpm_buckets._local_rates(np.asarray([.5,.9,.5,.9]))).all()
    # d554080e has such a window at 223.6 s; v3 keeps detecting regimes after it.
    rows=bpm_buckets.tempo_segments_v3(replay('gem_13'))
    assert rows and all(np.isfinite(row['bpm']) for row in rows)


@pytest.mark.parametrize('rate,expected',[
    (230.,115.),(345.,115.),(57.5,115.),(172.5,115.),(76.7,115.),(153.4,115.),(460.,115.),(28.75,115.),
    (118.,118.),(114.,114.),(140.,140.),(95.,95.)])
def test_harmonic_ratios_fold_onto_the_song_tempo_only_when_close(rate,expected):
    assert bpm_buckets._fold(rate,115.)==pytest.approx(expected,rel=.01)


@pytest.mark.parametrize('rate',[340.9,300.,45.,500.])
def test_unexplained_rates_far_from_the_song_are_clamped_to_it(rate):
    folded=bpm_buckets._fold(rate,115.)
    assert folded==pytest.approx(115.,rel=.06)
    assert folded<250


def test_song_reference_is_one_octave_and_duration_weighted():
    gaps=np.full(40,60/220);rates=np.full(40,220.)
    assert bpm_buckets._reference_tempo(rates,gaps)==pytest.approx(110.)
    # A long slow part outweighs many short fast intervals.
    rates=np.asarray([60.]*10+[120.]*40);gaps=60/rates
    assert bpm_buckets._reference_tempo(rates,gaps)==pytest.approx(120.)


def test_short_regime_is_absorbed_but_a_sustained_one_is_kept():
    blip=parts((120,80),(140,10),(120,80))
    assert len(bpm_buckets.tempo_segments(blip))>1  # v2: a four second blip becomes its own regime
    assert len(bpm_buckets.tempo_segments_v3(blip))==1
    sustained=bpm_buckets.build(parts((120,80),(140,40),(120,80)),advanced.defaults(),V3)
    assert sustained['count']==2 and all(seconds(row)>=8 and row['interval_count']>=16 for row in sustained['observed_segments'])


def test_baseline_is_the_bucket_held_longest_not_the_middle_rank():
    timing=parts((120,30),(140,70))
    v2=bpm_buckets.build(timing,advanced.defaults(),V2);v3=bpm_buckets.build(timing,advanced.defaults(),V3)
    assert v2['count']==v3['count']==2
    assert v2['baseline_bucket_id']==0
    assert v3['baseline_bucket_id']==1 and v3['buckets'][1]['offset']==0 and v3['buckets'][0]['offset']==-1


def test_v3_keeps_raw_observed_bpm_beside_the_folded_value():
    # The detector marked the pulse one level up for most of the song.
    policy=bpm_buckets.build(parts((110,60),(220,200)),advanced.defaults(),V3)
    assert policy['count']==1
    row=policy['segments'][0]
    assert row['bpm']==pytest.approx(110,rel=.02) and row['observed_bpm']==pytest.approx(220,rel=.02)


def test_summary_of_a_range_is_dominant_by_duration_with_weighted_bpm():
    policy=bpm_buckets.build(replay('machine_voice'),advanced.defaults(),V3)
    boundary=policy['segments'][0]['range'][1];end=policy['segments'][-1]['range'][1]
    whole=bpm_buckets.summarize_range(policy,0,end)
    low,high=policy['segments'][0],policy['segments'][1]
    expected=(low['bpm']*seconds(low)+high['bpm']*seconds(high))/(seconds(low)+seconds(high))
    assert whole['bpm_bucket_id']==0 and whole['multi_tempo'] and whole['bucket_ids']==[0,1]
    assert whole['detected_bpm']==pytest.approx(expected)
    inside=bpm_buckets.summarize_range(policy,boundary,end)
    assert inside['bpm_bucket_id']==1 and not inside['multi_tempo'] and inside['detected_bpm']==pytest.approx(high['bpm'])
    assert bpm_buckets.summarize_range(policy,end+10,end+20)['bpm_bucket_id'] is None


def machine_case():
    timing=replay('machine_voice');settings=advanced.defaults()
    policy=bpm_buckets.build(timing,settings,V3)
    return timing,settings,policy['segments'][0]['range'][1],timing['source']['samples']


def test_a_merged_user_region_is_one_condition_but_suggestions_still_split():
    timing,settings,boundary,end=machine_case()
    suggested=arrangement_for(timing,settings,bucket_version=V3)
    assert suggested['bpm_buckets']['region_assignment']=='automatic_bucket_split'
    assert any(section['bpm_bucket_id']==1 for section in suggested['sections'])
    assert any(section['core'][1]==boundary for section in suggested['sections'])
    merged=arrangement_for(timing,settings,[{'start_sample':0,'end_sample':end}],V3)
    assert merged['bpm_buckets']['region_assignment']==bpm_buckets.REGION_ASSIGNMENT
    assert len(merged['sections'])==1
    section=merged['sections'][0]
    assert section['core']==[0,end] and section['bpm_bucket_id']==0 and section['bpm_bucket_offset']==0
    policy=merged['bpm_buckets']
    expected=bpm_buckets.summarize_range(policy,0,end)['detected_bpm']
    assert section['tempo_confidence']['bpm']==pytest.approx(expected) and 90<expected<103
    assert section['tempo_confidence']['bpm']!=pytest.approx(policy['segments'][0]['bpm'])


def test_an_explicit_user_split_keeps_separate_conditions():
    timing,settings,boundary,end=machine_case()
    plan=arrangement_for(timing,settings,[{'start_sample':0,'end_sample':boundary},{'start_sample':boundary,'end_sample':end}],V3)
    assert [section['bpm_bucket_id'] for section in plan['sections']]==[0,1]
    assert [section['bpm_bucket_offset'] for section in plan['sections']]==[0,1]
    # A user cut that is not a tempo boundary does not invent a new bucket either.
    cut=boundary//2
    plan=arrangement_for(timing,settings,[{'start_sample':0,'end_sample':cut},{'start_sample':cut,'end_sample':end}],V3)
    assert [section['bpm_bucket_id'] for section in plan['sections']]==[0,0]


def test_historical_v2_plans_keep_resplitting_user_regions_at_bucket_boundaries():
    timing,settings,boundary,end=machine_case()
    plan=arrangement_for(timing,settings,[{'start_sample':0,'end_sample':end}])
    assert plan['bpm_buckets']['version']==V2 and 'region_assignment' not in plan['bpm_buckets']
    cuts={section['core'][1] for section in plan['sections']}
    old=bpm_buckets.build(timing,settings)
    assert len(plan['sections'])>1 and {row['range'][1] for row in old['segments'][:-1]}<=cuts


def test_direct_requests_follow_the_user_regions():
    from malody_studio import direct_v32, nps_star_calibration
    timing,settings,boundary,end=machine_case()
    policy=nps_star_calibration.freeze_policy()
    assert policy['bpm_bucket_version']==V3
    variants=[{'key':'balanced--master','pattern':'balanced','difficulty':'master'}]
    merged=arrangement_for(timing,settings,[{'start_sample':0,'end_sample':end}],V3)
    requests=direct_v32.requests_for(merged,variants,settings,policy,[0,end])
    assert len(requests)==1 and requests[0]['core']==[0,end]
    split=arrangement_for(timing,settings,[{'start_sample':0,'end_sample':boundary},{'start_sample':boundary,'end_sample':end}],V3)
    requests=direct_v32.requests_for(split,variants,settings,policy,[0,end])
    assert [row['core'] for row in requests]==[[0,boundary],[boundary,end]]
    assert requests[0]['sr']!=requests[1]['sr']


def test_frozen_plans_of_any_known_version_are_not_rebuilt_but_unknown_ones_are():
    from malody_studio import advanced_plans
    settings=advanced.defaults()
    def frozen(version,**extra):
        return {'planner':advanced_plans.PLANNER,'bpm_buckets':{'version':version},
                'settings_hash':sp.plan_settings_hash(settings),**extra}
    assert not advanced_plans.frozen_plan_needs_rebuild(frozen(V2),settings)
    assert not advanced_plans.frozen_plan_needs_rebuild(frozen(V3),settings)
    assert advanced_plans.frozen_plan_needs_rebuild(frozen('bpm-buckets-v1'),settings)
    assert advanced_plans.frozen_plan_needs_rebuild({**frozen(V2),'bpm_buckets':{}},settings)
    assert advanced_plans.frozen_plan_needs_rebuild(frozen(V2,planner='old'),settings)
    assert advanced_plans.frozen_plan_needs_rebuild(frozen(V3),{**settings,'dynamic_strength':.5})
    policy={'adapter':'beat_this_final1'}
    plan={**frozen(V2),'detector_execution':'beat-this-primary-v1','analysis_policy_hash':advanced_plans.canonical_hash(policy)}
    assert advanced_plans.current_plan(plan,policy)
    assert not advanced_plans.current_plan({**plan,'bpm_buckets':{'version':'bpm-buckets-v1'}},policy)


def test_new_simple_policy_freezes_v3_and_a_historical_policy_without_the_key_is_v2(monkeypatch):
    from malody_studio import direct_v32
    seen=[]
    def build(*args,**options):
        seen.append(options['bucket_version']);raise StopIteration
    monkeypatch.setattr(arrangement,'build',build)
    monkeypatch.setattr(mt,'load_or_analyze',lambda *args,**kw:{'id':'e','acoustic':{}})
    monkeypatch.setattr(mt,'select_timing',lambda _:{'id':'t','provenance':{'adapter':'beat_this_final1'}})
    import soundfile as soundfile
    monkeypatch.setattr(soundfile,'info',lambda _:type('Info',(),{'frames':44100,'samplerate':44100})())
    for policy,expected in (({'beat_analysis_policy':{}},V2),({'beat_analysis_policy':{},'bpm_bucket_version':V3},V3)):
        with pytest.raises(StopIteration):
            direct_v32.make_simple_plan('.',advanced.defaults(),[],policy,lambda *_:None)
        assert seen[-1]==expected
