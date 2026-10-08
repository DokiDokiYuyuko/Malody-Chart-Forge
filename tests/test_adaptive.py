import copy
import hashlib
import json
from types import SimpleNamespace
import numpy as np
import pytest
import soundfile as sf
from malody_studio import section_plan as sp, adaptive_difficulty as ad, advanced_generation as gen
from malody_studio.advanced import defaults
from malody_studio.charts import Note
from malody_studio.difficulty import PRESETS, calibrate


def features(data):
    duration=len(data)/sp.SR
    profile=[]
    for i in range(int(np.ceil(duration))):
        rate=2 if i<16 else 12 if i<32 else 4
        profile.append({'start_sample':i*sp.SR,'end_sample':min(len(data),(i+1)*sp.SR),
                        'rms':.1,'onset_rate':rate,'active_fraction':1.})
    return profile,np.zeros(round(duration/.005)),[],.005


@pytest.fixture
def planned(tmp_path,monkeypatch):
    monkeypatch.setattr(sp,'rhythm_features',features)
    t=np.arange(sp.SR*48)/sp.SR
    data=np.tile((.1*np.sin(2*np.pi*440*t)).astype(np.float32)[:,None],(1,2))
    path=tmp_path/'source.wav';sf.write(path,data,sp.SR,subtype='FLOAT')
    settings=defaults();settings['fixed_seed']=True
    tempo={'points':[[0,150],[27318,240]],'manual':True,'bpm':150}
    plan=sp.build_plan(path,settings,tempo)
    return path,settings,tempo,plan


def test_plan_stable_canonical_pcm_hash_and_no_note_shifting(planned):
    path,settings,tempo,plan=planned
    assert plan==sp.build_plan(path,settings,tempo)
    assert plan['source_pcm_sha']==hashlib.sha256(sf.read(path,dtype='float32',always_2d=True)[0].astype('<f4').tobytes()).hexdigest()
    assert sp.validate_plan(plan) is plan
    assert all(s['tempo_confidence']['note_shift_ms']==0 for s in plan['sections'])
    assert all(8*sp.SR<=s['core'][1]-s['core'][0]<=32*sp.SR for s in plan['sections'])
    corrupt=copy.deepcopy(plan);corrupt['sections'][0]['core'][1]+=1
    with pytest.raises(ValueError):sp.validate_plan(corrupt)
    modified=sp.build_plan(path,{**settings,'dynamic_strength':.5},tempo)
    assert modified['id']!=plan['id']


def test_legacy_absent_dynamic_is_off_and_user_hard_caps_unchanged(planned):
    path,settings,tempo,_=planned
    settings.pop('dynamic_enabled');settings['difficulty_rules']['hard'].update(gap=93,peak=11,chord=2)
    plan=sp.build_plan(path,settings,tempo)
    assert not plan['dynamic_enabled']
    for section in plan['sections']:
        hard=section['per_difficulty']['hard']
        assert hard['model_condition']==settings['conditions']['v32']['hard']
        assert hard['hard_caps']=={'peak_1s':11,'chord':2,'min_lane_gap_ms':93,'release_gap_ms':35,'hold_max_ms':650}
        assert hard['effective_boost']==0
    events=[(float(t),1,[Note(t,i%4)]) for i,t in enumerate(range(100,8000,100))]
    assert ad.calibrate_adaptive(events,8000,'medium',0,42,plan=plan)==calibrate(events,8000,'medium',0,42,min_notes=0)


def test_activity_boost_is_bounded_independently_from_tempo(planned):
    _,_,_,plan=planned
    by_section=[s['per_difficulty']['hard'] for s in plan['sections']]
    assert len({d['model_condition'] for d in by_section})>1
    for section in plan['sections']:
        for key,detail in section['per_difficulty'].items():
            assert abs(detail['effective_boost'])<=sp.BOOSTS[key]
            assert abs(detail['model_condition']-detail['base_model_condition'])<=sp.DELTAS[key]+1e-5
    envelope=np.zeros(3200);envelope[::50]=1
    evidence=sp._tempo_evidence(0,16,{},envelope,list(np.arange(0,16,.25)),.005)
    assert evidence['uncertain'] and not evidence['eligible_boost'] and evidence['margin']<.2


def test_v32_inference_coalesces_only_adjacent_near_equal_conditions():
    requests=[]
    for key,values in {'easy':[2.,2.08,2.18,2.55], 'expert':[5.5,5.68,6.1]}.items():
        for index,value in enumerate(values):
            a=index*100
            requests.append({'key':f'{key}-{index}','difficulty_key':key,'sr':value,
                             'core':[a,a+100],'context':[max(0,a-10),a+110],
                             'start_time':a/10,'end_time':(a+100)/10,
                             'section_id':f'{key}-section-{index}'})
    # A gap forces a split even when the model condition is identical.
    requests.append({'key':'easy-gap','difficulty_key':'easy','sr':2.55,
                     'core':[500,600],'context':[490,610],
                     'start_time':50,'end_time':60,'section_id':'easy-gap'})
    groups,owners=gen._coalesce_v32_requests(requests,tolerance=.25)
    assert len(groups)==5
    assert len(owners)==len(requests)
    assert owners['easy-0']['key']==owners['easy-2']['key']
    assert owners['easy-2']['key']!=owners['easy-3']['key']
    assert owners['easy-3']['key']!=owners['easy-gap']['key']
    assert owners['expert-0']['key']==owners['expert-1']['key']
    assert owners['expert-1']['key']!=owners['expert-2']['key']
    assert owners['easy-0']['core']==[0,300]
    assert (owners['easy-0']['start_time'],owners['easy-0']['end_time'])==gen._v32_time_bounds([0,310])
    assert (owners['easy-0']['core_start_time'],owners['easy-0']['core_end_time'])==gen._v32_time_bounds([0,300])
    assert owners['easy-0']['sr']==pytest.approx((2.+2.08+2.18)/3)


def test_v32_coalescing_respects_user_selected_maximum_span():
    rows=[{'key':f'easy-{i}','difficulty_key':'easy','sr':2.1,
           'core':[i*100,(i+1)*100],'context':[max(0,i*100-10),(i+1)*100+10],
           'start_time':i*10,'end_time':(i+1)*10,'section_id':f's-{i}',
           'retry_min_heads':4} for i in range(3)]
    groups,owners=gen._coalesce_v32_requests(rows,max_span_samples=200)
    assert [g['core'] for g in groups]==[[0,200],[200,300]]
    assert owners['easy-0']['key']==owners['easy-1']['key']
    assert owners['easy-1']['key']!=owners['easy-2']['key']
    assert groups[0]['retry_sections']==[
        {'start_time':gen._v32_time_bounds([0,100])[0],'end_time':gen._v32_time_bounds([0,100])[1],'retry_min_heads':4},
        {'start_time':gen._v32_time_bounds([100,200])[0],'end_time':gen._v32_time_bounds([100,200])[1],'retry_min_heads':4}]


def test_v32_millisecond_boundary_belongs_to_following_core():
    boundary=Note(27318,0)
    previous=[705600,1204724]
    following=[1204724,1910324]
    assert gen._v32_time_bounds(previous)==(16000,27318)
    assert gen._v32_owned_notes([boundary],previous)==[]
    assert gen._v32_owned_notes([boundary],following)==[boundary]


def test_section_granularity_controls_inference_core_length(planned):
    profile=[{'start_sample':i*sp.SR,'end_sample':(i+1)*sp.SR,'onset_rate':5.}
             for i in range(64)]
    boundaries={key:sp._boundaries(64*sp.SR,profile,{},key)
                for key in ('fine','balanced','coarse')}
    sections={key:np.diff(points)/sp.SR for key,points in boundaries.items()}
    assert len(sections['fine'])>len(sections['balanced'])>len(sections['coarse'])
    assert max(sections['fine'])<=16
    assert max(sections['balanced'])<=32
    assert max(sections['coarse'])<=48
    assert boundaries['fine'][0]==0 and boundaries['coarse'][-1]==64*sp.SR


def test_settings_validate_section_granularity():
    from malody_studio.advanced import validate_settings
    assert defaults()['section_granularity']=='balanced'
    for value in ('fine','balanced','coarse'):
        assert validate_settings({'section_granularity':value})['section_granularity']==value
    with pytest.raises(ValueError):
        validate_settings({'section_granularity':'extreme'})


@pytest.mark.parametrize('manual',[False,True])
def test_imported_timing_is_context_not_confirmed_audio_bpm(planned,manual):
    path, settings, _, _ = planned
    reference = {'points': [[0, 150], [27318, 240]], 'reference_source': 'imported_chart',
                 'reference_conflict': False, 'uncertain': False, 'manual': manual}
    plan = sp.build_plan(path, settings, reference)
    for section in plan['sections']:
        evidence = section['tempo_confidence']
        assert evidence['source'] == 'audio_estimate'
        assert evidence['uncertain'] and not evidence['eligible_boost']
        assert evidence['reference_source'] == 'imported_chart'
        assert evidence['reference_bpm'] in (150, 240)
        assert all(item['tempo_boost'] == 0 for item in section['per_difficulty'].values())


def test_fusion_rule_candidate_keeps_one_budget_and_event_provenance(tmp_path, monkeypatch):
    from malody_studio import advanced
    source = tmp_path / 'input.wav'
    sf.write(source, np.zeros((sp.SR * 2, 2), np.float32), sp.SR, subtype='FLOAT')
    monkeypatch.setattr(advanced, 'audio_metadata', lambda _: ([], {'bpm': 120, 'beat_times': [], 'uncertain': True}))
    store = advanced.ProjectStore(tmp_path / 'projects')
    project = store.create(source, 'Fusion', '')
    project = store.add_segment(project['id'], {'start_sample': 0, 'end_sample': project['samples']})
    segment = project['segments'][0]
    settings = defaults(); settings.update(strategy='fast', dynamic_enabled=False)
    event = {'id': 'stable-head', 'start_ms': 400., 'end_ms': None, 'lane': 0,
             'origins': ['vocal-17'], 'audio_evidence': {'onset': 400.}}
    revision = store.add_revision(project['id'], segment['id'], 'speed--medium', [event], settings,
                                  'fusion', {'global_budget_applied': True, 'section_plan_id': 'shared'})
    def forbidden(*args, **kwargs): raise AssertionError('Fusion must not spend another density budget')
    monkeypatch.setattr(ad, 'calibrate_adaptive', forbidden)
    candidate = gen.rule_candidate(store, project['id'], revision['id'], settings)
    assert candidate['events'] == [event]
    assert candidate['provenance']['global_budget_applied'] is True
    assert candidate['provenance']['playability']['hard_caps_only']
    assert store.revision(project['id'], revision['id'])['events'] == [event]


def test_silence_gets_zero_budget_even_with_trusted_fast_bpm(tmp_path,monkeypatch):
    def silent(data):
        profile,envelope,beats,hop=features(data)
        for p in profile:p.update(active_fraction=0.,onset_rate=0.,rms=0.)
        return profile,envelope,beats,hop
    monkeypatch.setattr(sp,'rhythm_features',silent)
    path=tmp_path/'silence.wav';sf.write(path,np.zeros((sp.SR*16,2),np.float32),sp.SR,subtype='FLOAT')
    plan=sp.build_plan(path,defaults(),{'manual':True,'points':[[0,300]]})
    assert all(s['per_difficulty']['hard']['target_heads_soft']==0 for s in plan['sections'])
    assert all(s['per_difficulty']['hard']['tempo_boost']==0 for s in plan['sections'])
    assert ad.calibrate_adaptive([],16000,'hard',0,42,plan=plan)[0]==[]


def test_distinct_31_to_49ms_model_timestamps_survive_attack_detection():
    sr=22050;y=(.1*np.sin(np.arange(sr*2)*2*np.pi*440/sr)).astype(np.float32)
    master=[Note(1000,0),Note(1031,1),Note(1080,2),Note(1000.6,0),Note(1000,3)]
    attacks=ad.attacks_adaptive(y,sr,master)
    kept=[n for _,_,notes in attacks for n in notes]
    assert {n.start for n in kept}=={1000,1031,1080}
    assert len(kept)==4
    assert ad.attacks_adaptive(np.zeros_like(y),sr,master)==[]


def test_raw_immutable_holds_cross_sections_and_only_hard_caps_repair():
    raw=[Note(100,0,5100),Note(4000,0),Note(4050,1),Note(5000,2)]
    before=raw[:];rule={**PRESETS['expert'],'hold_ms':6000,'gap':40,'peak':19}
    playable,report=ad.repair_playable(raw,6000,rule,'jackspeed')
    assert raw==before and playable[0].end==5100
    assert {n.start for n in playable}=={100,4000,4050,5000}
    assert next(n for n in playable if n.start==4000).lane!=0
    assert not report['global_budget_applied'] and report['hard_caps_only']
    assert report['decisions']


def test_phrase_budget_no_grid_fill_no_audio_only_chords_and_no_double_pass(planned):
    _,_,_,plan=planned
    candidates=[(float(t),1,[]) for t in range(100,16000,1000)]
    notes,report=ad.calibrate_adaptive(candidates,16000,'expert',0,42,plan=plan)
    assert len(notes)<=len(candidates) and set(n.start for n in notes)<=set(c[0] for c in candidates)
    assert report['budget_passes']==1 and report['capacity_saturated']
    assert all(n.end is None for n in notes)
    assert notes==ad.calibrate_adaptive(candidates,16000,'expert',0,42,plan=plan)[0]


def test_dynamic_native_core_conditions_seeds_and_separate_candidate(planned,tmp_path,monkeypatch):
    path,settings,tempo,plan=planned
    from malody_studio import mapperatorinator
    calls=[]
    def infer(source,folder,options,progress):
        calls.append(copy.deepcopy(options['_advanced_presets']))
        results={}
        for r in options['_advanced_presets']:
            a,b=r['core']
            notes=[]
            for section in plan['sections']:
                start,end=section['core']
                if a<=start and end<=b:
                    t=start*1000/sp.SR
                    notes.extend([Note(t+100,0),Note(t+131,1),Note(t+180,2)])
            results[r['key']]=(notes,[[0,150],[27318,240]])
        return results,{}
    monkeypatch.setattr(mapperatorinator,'generate',infer)
    variants=[{'key':'speed--hard','pattern':'speed','difficulty':'hard'}]
    snapshot={'project':{'title':'test','artist':'test','tempo':tempo,'source_pcm_sha256':plan['source_pcm_sha']},
              'segment':{'start_sample':0,'end_sample':plan['samples']},'settings':settings,'variants':variants,'section_plan':plan}
    result=gen.run(path,tmp_path/'run-one',{'_advanced':snapshot},lambda *_:None)
    again=gen.run(path,tmp_path/'run-two',{'_advanced':snapshot},lambda *_:None)
    assert calls[0]==calls[1] and len({r['sr'] for r in calls[0]})>1
    assert len({r['seed'] for r in calls[0]})==len(calls[0])
    assert [r['kind'] for r in result['advanced_result']]==['model_raw','rules']
    raw,candidate=result['advanced_result']
    assert raw['activate_initial'] is False and candidate['activate_initial'] is True
    assert len(raw['events'])==3*len(plan['sections'])
    assert raw['events']==again['advanced_result'][0]['events']
    cache=json.loads((tmp_path/'run-one'/'speed-mother.json').read_text(encoding='utf-8'))
    assert len(cache['records'])==len(plan['sections'])
    assert all(record['attempts'] for record in cache['records'])


def test_dynamic_skips_exact_original_silent_core_without_model(planned,tmp_path,monkeypatch):
    path,settings,tempo,plan=planned
    from malody_studio import mapperatorinator
    data=sf.read(path,dtype='float32',always_2d=True)[0]
    silent=plan['sections'][1]['core']
    data[silent[0]:silent[1]]=0
    sf.write(path,data,sp.SR,subtype='FLOAT')
    plan=sp.build_plan(path,settings,tempo)
    calls=[]
    def infer(source,folder,options,progress):
        calls.extend(options['_advanced_presets'])
        return {r['key']:([Note(r['start_time']+100,0)],[[0,150]])
                for r in options['_advanced_presets']},{}
    monkeypatch.setattr(mapperatorinator,'generate',infer)
    snapshot={'project':{'title':'silent core','artist':'','tempo':tempo},
              'segment':{'start_sample':0,'end_sample':plan['samples']},
              'settings':settings,'variants':[{'key':'speed--hard','pattern':'speed','difficulty':'hard'}],
              'section_plan':plan,'original_source':{'path':str(path)}}
    result=gen.run(path,tmp_path/'silent-core',{'_advanced':snapshot},lambda *_:None)
    assert calls and result['advanced_result']
    assert all(r['core'][1]<=silent[0] or r['core'][0]>=silent[1] for r in calls)
    cache=json.loads((tmp_path/'silent-core'/'speed-mother.json').read_text(encoding='utf-8'))
    assert cache['metadata']['silent_cores']==[{'core':silent,'silence_verified':True,'inference_count':0}]


def test_fast_stem_raw_only_coalesces_mothers_without_budget(planned,tmp_path,monkeypatch):
    path,settings,tempo,plan=planned
    from malody_studio import mapperatorinator
    settings={**settings,'strategy':'fast'}
    calls=[]
    def infer(source,folder,options,progress):
        calls.extend(options['_advanced_presets'])
        return {r['key']:([Note(r['start_time']+100,0)],[[0,150]]) for r in options['_advanced_presets']},{}
    monkeypatch.setattr(mapperatorinator,'generate',infer)
    descriptor={'source_id':'stem:vocals','source_role':'vocals','pcm_sha':plan['source_pcm_sha'],'parent_source_id':plan['source_pcm_sha'],'stem_set_id':'test'}
    snap={'project':{'title':'test','artist':'test','tempo':tempo},'segment':{'start_sample':0,'end_sample':plan['samples']},
          'settings':settings,'variants':[{'key':'speed--'+k,'pattern':'speed','difficulty':k} for k in ('easy','medium')],
          'section_plan':plan,'source':descriptor,'_stem_raw_only':True}
    result=gen.run(path,tmp_path/'stem',{'_advanced':snap},lambda *_:None)
    assert 0<len(calls)<=len(plan['sections'])
    assert calls[0]['core'][0]==0 and calls[-1]['core'][1]==plan['samples']
    assert all(left['core'][1]==right['core'][0] for left,right in zip(calls,calls[1:]))
    assert all(call['core'][1]-call['core'][0]<=32*sp.SR for call in calls)
    assert len(result['advanced_result'])==2
    assert all(r['kind']=='stem_raw' and r['provenance']['global_budget_applied'] is False for r in result['advanced_result'])



def test_mixed_anchor_confidence_uses_only_covered_intervals(planned):
    path,settings,_,_=planned
    tempo={'points':[[0,150],[27318,240]],'manual':True,'reference_source':'mixed','uncertain':True,
           'points_metadata':[{'source':'audio_analysis','confirmed':False},{'source':'user_confirmed','confirmed':True}]}
    plan=sp.build_plan(path,settings,tempo)
    automatic=[section for section in plan['sections'] if section['core'][0]/sp.SR<27.318]
    confirmed=[section for section in plan['sections'] if section['core'][0]/sp.SR>=27.318]
    assert automatic and confirmed
    for section in automatic:
        evidence=section['tempo_confidence']
        assert evidence['source']=='audio_estimate' and evidence['uncertain'] and not evidence['eligible_boost']
        assert all(detail['tempo_boost']==0 for detail in section['per_difficulty'].values())
    for section in confirmed:
        evidence=section['tempo_confidence']
        assert evidence['source']=='manual' and evidence['bpm']==240 and not evidence['uncertain']
        assert evidence['reference_source']=='user_confirmed'
    zeros=np.zeros(4800)
    crossing=sp._tempo_evidence(20,30,tempo,zeros,[],.01)
    assert crossing['reference_source']=='mixed' and crossing['uncertain'] and not crossing['eligible_boost']
    short=sp._tempo_evidence(27.318,28,tempo,zeros,[],.01)
    assert not short['eligible_boost']
    # An inherited manual flag cannot upgrade an imported per-point reference.
    imported=copy.deepcopy(tempo);imported['points_metadata'][1]={'source':'imported_chart','confirmed':True}
    evidence=sp._tempo_evidence(30,40,imported,zeros,[],.01)
    assert evidence['source']=='audio_estimate' and evidence['reference_source']=='imported_chart'
    assert evidence['uncertain'] and not evidence['eligible_boost']


def test_imported_scalar_bpm_remains_unconfirmed_context(planned):
    path,settings,_,_=planned
    tempo={'bpm':174.2,'beat_times':[0.,.35,.69], 'uncertain':False}
    plan=sp.build_plan(path,settings,tempo)
    assert plan['tempo_reference']==tempo and 'points' not in plan['tempo_reference']
    assert len(plan['sections'])>1
    assert len({s['per_difficulty']['hard']['model_condition'] for s in plan['sections']})>1
    for section in plan['sections']:
        evidence=section['tempo_confidence']
        assert evidence['reference_bpm']==174.2
        assert evidence['reference_source']=='audio_analysis'
        assert evidence['source']=='audio_estimate' and evidence['uncertain']
        assert not evidence['eligible_boost']
        assert all(detail['tempo_boost']==0 for detail in section['per_difficulty'].values())
    # Even a historical manual flag does not confirm absent per-interval anchors.
    evidence=sp._tempo_evidence(0,10,{**tempo,'manual':True},np.zeros(1000),[],.01)
    assert evidence['reference_bpm']==174.2 and evidence['uncertain']


def test_confirmed_tempo_crossing_integrates_each_anchor_without_inventing_points():
    tempo={'points':[[0,120],[6000,240]],'manual':True}
    evidence=sp._tempo_evidence(0,10,tempo,np.zeros(1000),[],.01)
    assert evidence['source']=='manual' and not evidence['uncertain']
    assert evidence['beat_count']==28
    assert evidence['bpm']==pytest.approx(168)
    assert evidence['bpm_start']==120 and evidence['bpm_end']==240
    assert evidence['variable_bpm'] is True
    assert evidence['note_shift_ms']==0
    assert tempo['points']==[[0,120],[6000,240]]
    same=sp._tempo_evidence(0,10,{'points':[[0,120],[6000,120]],'manual':True},np.zeros(1000),[],.01)
    assert same['bpm']==pytest.approx(120) and same['variable_bpm'] is False


def test_late_confirmed_anchor_does_not_cover_earlier_audio():
    tempo={'points':[[6000,240]],'manual':True,'reference_source':'user_confirmed',
           'points_metadata':[{'source':'user_confirmed','confirmed':True}]}
    evidence=sp._tempo_evidence(0,10,tempo,np.zeros(1000),[],.01)
    assert evidence['source']=='audio_estimate' and evidence['uncertain']
    assert evidence['reference_bpm'] is None and not evidence['eligible_boost']


def test_invalid_primary_uses_valid_retry_without_losing_other_difficulties(planned,tmp_path,monkeypatch):
    path,settings,tempo,plan=planned
    from malody_studio import mapperatorinator
    def infer(source,folder,options,progress):
        results={}; rejected={}
        for r in options['_advanced_presets']:
            if r['key'].startswith('hard'):
                rejected[r['key']]='音符轨道坐标超出范围'
                results[r['key']+'__retry']=([Note(r['start_time']+100,0)],[[0,150]])
            else:
                results[r['key']]=([Note(r['start_time']+150,1)],[[0,150]])
        return results,{'rejected_charts':rejected}
    monkeypatch.setattr(mapperatorinator,'generate',infer)
    snap={'project':{'title':'test','artist':'test','tempo':tempo},'segment':{'start_sample':0,'end_sample':plan['samples']},
          'settings':settings,'variants':[{'key':'balanced--'+k,'pattern':'balanced','difficulty':k} for k in ('hard','expert')],
          'section_plan':plan,'_stem_raw_only':True}
    result=gen.run(path,tmp_path/'recovered',{'_advanced':snap},lambda *_:None)
    assert not result['errors']
    assert {r['variant'] for r in result['advanced_result']}=={'balanced--hard','balanced--expert'}
    hard=next(r for r in result['advanced_result'] if r['variant']=='balanced--hard')
    assert all(r['recovered_from_rejected_primary'] and r['first_heads'] is None for r in hard['provenance']['core_conditions'])
    assert hard['events'] and all(e['lane']==0 for e in hard['events'])


def test_no_valid_attempt_blocks_only_affected_difficulty(planned,tmp_path,monkeypatch):
    path,settings,tempo,plan=planned
    from malody_studio import mapperatorinator
    def infer(source,folder,options,progress):
        return {r['key']:([Note(r['start_time']+100,0)],[[0,150]]) for r in options['_advanced_presets'] if r['key'].startswith('expert')},{}
    monkeypatch.setattr(mapperatorinator,'generate',infer)
    snap={'project':{'title':'test','artist':'test','tempo':tempo},'segment':{'start_sample':0,'end_sample':plan['samples']},
          'settings':settings,'variants':[{'key':'balanced--'+k,'pattern':'balanced','difficulty':k} for k in ('hard','expert')],
          'section_plan':plan,'_stem_raw_only':True}
    result=gen.run(path,tmp_path/'partial',{'_advanced':snap},lambda *_:None)
    assert {r['variant'] for r in result['advanced_result']}=={'balanced--expert'}
    assert result['errors'] and all(e['difficulty']=='hard' for e in result['errors'])
