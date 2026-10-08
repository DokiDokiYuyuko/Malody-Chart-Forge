import copy
import json
import numpy as np
import soundfile as sf
import pytest
from malody_studio import advanced_generation as gen, section_plan as sp, mapperatorinator
from malody_studio.advanced import defaults
from malody_studio.charts import Note
from malody_studio.density_calibration import freeze


@pytest.fixture
def source_plan(tmp_path,monkeypatch):
    monkeypatch.setattr(sp,'rhythm_features',lambda data:([
        {'start_sample':i*sp.SR,'end_sample':(i+1)*sp.SR,'active_fraction':1.,'onset_rate':8.,'rms':.1}
        for i in range(8)],np.ones(1600),[],.005))
    source=tmp_path/'source.wav'
    sf.write(source,np.full((8*sp.SR,2),.1,np.float32),sp.SR,subtype='FLOAT')
    settings=defaults();settings.update(strategy='fast',dynamic_enabled=False,title='test',artist='test')
    settings['density_policy']=freeze(settings)
    plan=sp.build_plan(source,settings,{'points':[[0,150]],'manual':True,'bpm':150})
    return source,settings,plan


def test_simple_raw_mother_lunatic_has_two_rounds_and_keeps_complete_best_candidate(source_plan,tmp_path,monkeypatch):
    source,settings,plan=source_plan;calls=[]
    def infer(source,folder,options,progress):
        calls.extend(copy.deepcopy(options['_advanced_presets']));results={}
        for r in options['_advanced_presets']:
            assert r['difficulty_key']=='lunatic'
            assert len(r['density_rounds'])<=2
            assert 'retry_condition' not in r
            results[r['key']]=([Note(100,0)],[[0,150]])
            # A complete retry with distinct notes must be selected wholesale,
            # preserving count and timing instead of copying the primary.
            results[r['key']+'__density_retry1']=([Note(500+i*100,i%4) for i in range(20)],[[0,170]])
            results[r['key']+'__density_retry2']=([Note(500+i*100,i%4) for i in range(10)],[[0,190]])
        return results,{}
    monkeypatch.setattr(mapperatorinator,'generate',infer)
    variants=[{'key':'speed--'+key,'pattern':'speed','difficulty':key} for key in ('hard','lunatic')]
    result=gen.generate_sectioned(source,tmp_path/'simple',settings,variants,plan,lambda *_:None,raw_only=True)
    cache=json.loads((tmp_path/'simple'/'speed-mother.json').read_text(encoding='utf-8'))
    assert cache['mother_key']=='lunatic'
    assert set(cache['raw'])=={'lunatic'}
    assert len(cache['raw']['lunatic'])==20
    assert all(len(row['events'])==20 for row in result['advanced_result'])
    assert all(row['provenance']['serialization_tempo']['points']==[(0.,170.)] for row in result['advanced_result'])


def test_stem_snapshot_raw_only_cannot_spend_supply_rounds(source_plan,tmp_path,monkeypatch):
    source,settings,plan=source_plan;calls=[]
    def infer(source,folder,options,progress):
        calls.extend(copy.deepcopy(options['_advanced_presets']))
        return {r['key']:([Note(100,0)],[[0,150]]) for r in options['_advanced_presets']},{}
    monkeypatch.setattr(mapperatorinator,'generate',infer)
    snapshot={'project':{'title':'test','artist':'test','tempo':plan['tempo_reference']},
              'segment':{'start_sample':0,'end_sample':plan['samples']},'settings':settings,
              'variants':[{'key':'speed--hard','pattern':'speed','difficulty':'hard'}],
              'section_plan':plan,'_stem_raw_only':True}
    gen.run(source,tmp_path/'stem',{'_advanced':snapshot},lambda *_:None)
    assert calls and all(r['disable_density_retry'] and not r['density_rounds'] for r in calls)
    assert all('retry_min_heads' not in r for r in calls)


@pytest.mark.parametrize('dynamic',[False,True])
def test_generation_padding_clips_effective_end_but_owns_only_core(source_plan,tmp_path,monkeypatch,dynamic):
    source,settings,plan=source_plan;calls=[]
    settings.pop('density_policy',None)
    settings.update(strategy='independent',dynamic_enabled=dynamic)
    def infer(source,folder,options,progress):
        calls.extend(copy.deepcopy(options['_advanced_presets']))
        return {r['key']:([Note(t,i%4) for i,t in enumerate([1000,2000,3999,4000,5000])],[[0,150]])
                for r in options['_advanced_presets']},{}
    monkeypatch.setattr(mapperatorinator,'generate',infer)
    snapshot={'project':{'title':'test','artist':'test','tempo':{'bpm':150,'manual':True},
                         'samples':8*sp.SR,'tail_trim':{'cutoff_sample':6*sp.SR}},
              'segment':{'start_sample':2*sp.SR,'end_sample':4*sp.SR},'settings':settings,
              'variants':[{'key':'balanced--hard','pattern':'balanced','difficulty':'hard'}],
              '_stem_raw_only':True}
    if dynamic:snapshot['section_plan']=plan
    result=gen.run(source,tmp_path/str(dynamic),{'_advanced':snapshot},lambda *_:None)
    assert calls and all(r['start_time']==0 and r['end_time']==6000 for r in calls)
    assert all(r['core']==[2*sp.SR,4*sp.SR] for r in calls)
    assert [e['start_ms'] for e in result['advanced_result'][0]['events']]==[2000,3999]


def test_density_worker_quota_credits_frozen_measured_singletons(source_plan,tmp_path,monkeypatch):
    from malody_studio.quality_workflow import candidate_contract
    source,settings,plan=source_plan;calls=[]
    settings.update(strategy='native')
    def infer(source,folder,options,progress):
        calls.extend(copy.deepcopy(options['_advanced_presets']))
        return {r['key']:([Note(100,0)],[[0,150]]) for r in options['_advanced_presets']},{}
    monkeypatch.setattr(mapperatorinator,'generate',infer)
    snapshot={'project':{'title':'test','artist':'test','tempo':plan['tempo_reference']},
              'segment':{'start_sample':0,'end_sample':plan['samples']},'settings':settings,
              'variants':[{'key':'balanced--hard','pattern':'balanced','difficulty':'hard'}],
              'section_plan':plan,'_stem_raw_only':True,'_density_raw_supply':True,
              'candidate_policy':candidate_contract('evidenced-single-heads-v1'),
              'evidence':{'acoustic':{'source':{'sample_rate':sp.SR},
                  'onset_samples':[int(i*.05*sp.SR) for i in range(160)],'onset_strengths':[1]*160}}}
    gen.run(source,tmp_path/'acoustic-credit',{'_advanced':snapshot},lambda *_:None)
    assert calls and all(r['density_target_heads']>0 and r['density_min_heads']==0 for r in calls)
    assert all(len(r['density_sections'][0]['acoustic_times'])==160 for r in calls)


@pytest.mark.parametrize('override',[False,True])
def test_explicit_retry_condition_override_survives_frozen_initial_calibration(source_plan,tmp_path,monkeypatch,override):
    source,settings,plan=source_plan;calls=[]
    settings['strategy']='native'
    policy=freeze(settings,[{'engine':'v32','condition':4,'achieved_rate':8.5,'recording_id':'development',
                            'source_role':'mix','pattern':'balanced'}])
    # The retry coordinator has already selected condition 7. Applying the
    # initial inverse again would silently put it back to condition 4.
    for section in plan['sections']:section['per_difficulty']['hard']['model_condition']=7
    plan['content_hash']=sp.canonical_hash({key:value for key,value in plan.items() if key not in ('id','content_hash')})
    plan['id']=plan['content_hash']
    def infer(source,folder,options,progress):
        calls.extend(copy.deepcopy(options['_advanced_presets']))
        return {r['key']:([Note(100,0)],[[0,150]]) for r in options['_advanced_presets']},{}
    monkeypatch.setattr(mapperatorinator,'generate',infer)
    snapshot={'project':{'title':'test','artist':'test','tempo':plan['tempo_reference']},
              'segment':{'start_sample':0,'end_sample':plan['samples']},'settings':settings,
              'variants':[{'key':'balanced--hard','pattern':'balanced','difficulty':'hard'}],
              'section_plan':plan,'_stem_raw_only':True,'density_policy':policy,
              'density_condition_override':override}
    gen.run(source,tmp_path/'override',{'_advanced':snapshot},lambda *_:None)
    assert calls and all(row['sr']==7 for row in calls)
    assert all(row['requested_condition']==7 and row['executed_condition']==7 and
               row['condition_rationale']=='frozen_requested_condition' for row in calls)
    assert all(not row['density_rounds'] for row in calls)


@pytest.mark.parametrize('strategy',['fast','native'])
def test_density_budget_uses_only_classified_model_supply_and_keeps_raw(source_plan,tmp_path,monkeypatch,strategy):
    from malody_studio import adaptive_difficulty, chart_quality, event_evidence
    source,settings,plan=source_plan
    settings['strategy']=strategy
    assert not plan.get('arrangement_enabled')
    original=tmp_path/'original.wav'
    original.write_bytes(source.read_bytes())
    supplied=[Note(100,0,450),Note(500,1,850),Note(900,2)]
    evidence_calls=[];budget_supplies=[]
    def evidence(path,**kwargs):
        evidence_calls.append((path,kwargs))
        return {'id':'matched-original-and-stem'}
    monkeypatch.setattr(event_evidence,'cached_evidence',evidence)
    monkeypatch.setattr(chart_quality,'head_evidence',lambda event,evidence:
                        {'sustain_support':.2,'important_sustain':event['start_ms']==500})
    def infer(source,folder,options,progress):
        return {r['key']:(copy.deepcopy(supplied),[[0,150]]) for r in options['_advanced_presets']},{}
    monkeypatch.setattr(mapperatorinator,'generate',infer)
    def forbidden(*args,**kwargs):
        raise AssertionError('audio-only heads must never be fabricated for density-policy jobs')
    monkeypatch.setattr(adaptive_difficulty,'attacks_adaptive',forbidden)
    def budget(candidates,*args,**kwargs):
        notes=[note for timestamp,score,voices in candidates for note in voices]
        budget_supplies.append(copy.deepcopy(notes))
        return notes,{}
    monkeypatch.setattr(adaptive_difficulty,'calibrate_adaptive',budget)
    snapshot={'project':{'title':'test','artist':'test','tempo':plan['tempo_reference']},
              'segment':{'start_sample':0,'end_sample':plan['samples']},'settings':settings,
              'variants':[{'key':'balanced--hard','pattern':'balanced','difficulty':'hard'}],
              'section_plan':plan,'original_source':{'path':str(original)},
              'source':{'source_id':'stem-vocals','source_role':'vocals',
                        'parent_source_id':plan['source_pcm_sha']}}
    folder=tmp_path/strategy
    result=gen._run_dynamic(source,folder,{'_advanced':snapshot},lambda *_:None)
    assert not result['errors']
    assert evidence_calls[0][0]==str(original)
    assert evidence_calls[0][1]['stems']=={'vocals':source}
    assert budget_supplies and [(n.start,n.lane,n.end) for n in budget_supplies[0]]==[
        (100,0,None),(500,1,850),(900,2,None)]
    cache=json.loads((folder/'balanced-mother.json').read_text(encoding='utf-8'))
    assert cache['raw']['hard']==[[100,0,450],[500,1,850],[900,2,None]]
    raw=next(row for row in result['advanced_result'] if row['kind']=='model_raw')
    assert [e['end_ms'] for e in raw['events']]==[450,850,None]
    playable=next(row for row in result['advanced_result'] if row['provenance']['budget_stage']==1)
    classification=playable['provenance']['hold_classification']
    assert classification['evidence_id']=='matched-original-and-stem'
    assert len(classification['decisions'])==1
    assert classification['decisions'][0]['original_end_ms']==450


@pytest.mark.parametrize('policy_kind',['density_hash','density_version','quality_hash'])
def test_frozen_policy_corruption_rejected_before_model(source_plan,tmp_path,monkeypatch,policy_kind):
    from malody_studio.chart_quality import contract
    import hashlib
    source,settings,plan=source_plan
    snapshot={'settings':settings,'project':{},'segment':{},'variants':[],
              'density_policy':copy.deepcopy(settings['density_policy']),'quality_policy':contract()}
    if policy_kind=='density_hash':
        snapshot['density_policy']['max_extra_rounds']=99
    elif policy_kind=='density_version':
        policy=snapshot['density_policy'];policy['version']='unsupported'
        policy['policy_hash']=hashlib.sha256(json.dumps({k:v for k,v in policy.items() if k!='policy_hash'},
            sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    else:
        snapshot['quality_policy']['hash']='corrupt'
    def forbidden(*args,**kwargs):
        raise AssertionError('corrupted policies must fail before model execution')
    monkeypatch.setattr(mapperatorinator,'generate',forbidden)
    with pytest.raises(ValueError,match='策略'):
        gen._run_dynamic(source,tmp_path/'corrupt',{'_advanced':snapshot},lambda *_:None)


def test_legacy_unhashed_policy_remains_readable():
    gen.validate_frozen_policies({'density_policy':{'version':'fixture'},
                                  'quality_policy':{'version':'legacy'}})


def test_selected_retry_condition_seed_and_clock_are_recorded_on_nonzero_core(source_plan,tmp_path,monkeypatch):
    source,settings,plan=source_plan
    settings['strategy']='native'
    requests=[]
    def infer(source,folder,options,progress):
        requests.extend(copy.deepcopy(options['_advanced_presets']))
        result={}
        for request in options['_advanced_presets']:
            result[request['key']]=([Note(2100,0)],[[0,150]])
            result[request['key']+'__density_retry1']=([Note(2100+i*100,i%4) for i in range(24)],[[0,170],[3000,175],[6000,190]])
        return result,{}
    monkeypatch.setattr(mapperatorinator,'generate',infer)
    snapshot={'project':{'title':'test','artist':'test','tempo':plan['tempo_reference']},
              'segment':{'start_sample':2*sp.SR,'end_sample':5*sp.SR},'settings':settings,
              'variants':[{'key':'balanced--hard','pattern':'balanced','difficulty':'hard'}],
              'section_plan':plan,'_stem_raw_only':True,'_density_raw_supply':True}
    result=gen.run(source,tmp_path/'selected-retry',{'_advanced':snapshot},lambda *_:None)
    record=result['advanced_result'][0]['provenance']['core_conditions'][0]
    assert record['selected_round']==1
    assert record['selected_condition']==requests[0]['density_rounds'][0]['condition']
    assert record['selected_seed']==requests[0]['density_rounds'][0]['seed']
    assert record['inference_condition']==requests[0]['sr']
    assert record['attempts'][1]['condition']==record['selected_condition']
    assert result['timings']['balanced']==[(2000.,170.),(3000.,175.)]


def test_model_only_worker_quota_never_credits_acoustic_heads(source_plan,tmp_path,monkeypatch):
    from malody_studio.quality_workflow import candidate_contract
    source,settings,plan=source_plan;requests=[]
    settings['strategy']='native'
    def infer(source,folder,options,progress):
        requests.extend(copy.deepcopy(options['_advanced_presets']))
        return {r['key']:([Note(100,0)],[[0,150]]) for r in options['_advanced_presets']},{}
    monkeypatch.setattr(mapperatorinator,'generate',infer)
    snapshot={'project':{'title':'test','artist':'test','tempo':plan['tempo_reference']},
              'segment':{'start_sample':0,'end_sample':plan['samples']},'settings':settings,
              'variants':[{'key':'balanced--hard','pattern':'balanced','difficulty':'hard'}],
              'section_plan':plan,'_stem_raw_only':True,'_density_raw_supply':True,
              'candidate_policy':candidate_contract('model-heads-only-v3'),
              'evidence':{'acoustic':{'source':{'sample_rate':sp.SR},
                  'onset_samples':[int(i*.05*sp.SR) for i in range(160)],'onset_strengths':[1]*160}}}
    gen.run(source,tmp_path/'model-only-quota',{'_advanced':snapshot},lambda *_:None)
    assert requests and all(r['density_min_heads']==r['density_target_heads']>0 for r in requests)
    assert all(not row['acoustic_times'] for r in requests for row in r['density_sections'])


def test_advanced_classic_fast_default_never_adds_audio_heads_or_chord_voices(source_plan,tmp_path,monkeypatch):
    source,settings,plan=source_plan
    settings.pop('density_policy',None)
    settings.update(strategy='fast',dynamic_enabled=False)
    supplied=[Note(2100,0),Note(2500,1),Note(3000,2)]
    monkeypatch.setattr(mapperatorinator,'generate',lambda source,folder,options,progress:
        ({r['key']:(copy.deepcopy(supplied),[[0,150]]) for r in options['_advanced_presets']},{}))
    def forbidden(*args,**kwargs):raise AssertionError('default advanced fast must not make acoustic heads')
    monkeypatch.setattr(gen,'attacks',forbidden)
    snapshot={'project':{'title':'test','artist':'test','tempo':plan['tempo_reference']},
              'segment':{'start_sample':2*sp.SR,'end_sample':4*sp.SR},'settings':settings,
              'variants':[{'key':'balanced--lunatic','pattern':'balanced','difficulty':'lunatic'}]}
    result=gen.run(source,tmp_path/'classic-fast',{'_advanced':snapshot},lambda *_:None)
    assert {(e['start_ms'],e['lane']) for e in result['advanced_result'][0]['events']}=={
        (n.start,n.lane) for n in supplied}


@pytest.mark.parametrize('strategy',['fast','native'])
def test_mug_executes_at_most_two_extra_rounds_and_fast_uses_highest_mother(source_plan,tmp_path,monkeypatch,strategy):
    from malody_studio import engine
    source,settings,plan=source_plan
    settings.update(engine='mug',strategy=strategy)
    settings['density_policy']=freeze(settings)
    calls=[]
    class FakeEngine:
        def prepare(self,*args):return None
        def generate(self,wave,unused,cfg,progress):
            calls.append(copy.deepcopy(cfg))
            return [Note(100,0)]
        def unload(self):pass
    monkeypatch.setattr(engine,'Engine',FakeEngine)
    keys=['hard','lunatic']
    variants=[{'key':'balanced--'+key,'pattern':'balanced','difficulty':key} for key in keys]
    result=gen.generate_sectioned(source,tmp_path/strategy,settings,variants,plan,lambda *_:None,raw_only=True)
    assert not result['errors']
    cache=json.loads((tmp_path/strategy/'balanced-mother.json').read_text(encoding='utf-8'))
    assert calls and len(calls)==sum(len(record['attempts']) for record in cache['records'])
    assert all(len(record['attempts'])==3 for record in cache['records'])
    assert all(len(record['density_rounds'])==2 for record in cache['records'])
    if strategy=='fast':
        assert cache['mother_key']=='lunatic'
        assert set(cache['raw'])=={'lunatic'}
        assert calls[0]['mug_difficulty']==settings['conditions']['mug']['lunatic']
    else:
        assert set(cache['raw'])==set(keys)
