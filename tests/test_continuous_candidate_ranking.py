from copy import deepcopy

from malody_studio import advanced_generation
from malody_studio.charts import Note
from malody_studio.density_validation import candidate_rank, evaluate

import json
import numpy as np
import pytest
import soundfile as sf


def test_a_model_round_that_can_be_calibrated_is_not_replaced_by_a_sparse_round():
    core = [0, 441000]
    caps = {'peak_1s': 19, 'chord': 4, 'min_lane_gap_ms': 55,
            'hold_max_ms': 400, 'release_gap_ms': 35}
    plan = {'sample_rate': 44100, 'sections': [{'id': 'full', 'core': core,
            'active_seconds': 10, 'profile': [{'start_sample': 0, 'end_sample': 441000, 'active_fraction': 1}],
            'per_difficulty': {'expert': {'target_rate': 13, 'target_heads_soft': 130, 'hard_caps': caps}}}]}
    settings = {'seed': 42, 'ln_ratio': .15, 'difficulty_rules': {'expert':
                {'rate': 13, 'gap': 55, 'chord': 4, 'peak': 19, 'hold_ms': 400}}}
    dense = [Note(i * 50, i % 4) for i in range(200)]
    sparse = [Note(i * 250, i % 4) for i in range(39)]
    before = deepcopy((dense, sparse, plan, settings))
    raw_scores = []
    for notes in (dense, sparse):
        events, _ = advanced_generation.owned(notes, 0, 10000, 10000)
        raw_scores.append(candidate_rank(events, evaluate(events, plan, 'expert', core), settings['difficulty_rules']['expert']))
    assert raw_scores[1] < raw_scores[0]  # the real pre-calibration failure
    scores = []
    for notes in (dense, sparse):
        score, report = advanced_generation.rank_playable_model_candidate(notes, plan=plan, core=core,
            difficulty='expert', settings=settings, pattern='balanced', source_end_ms=10000,
            evidence={'source': {'sample_rate': 1000}, 'onset_samples': [], 'onset_strengths': []},
            timing=None)
        scores.append(score)
        assert report['candidate_acoustic_heads'] == 0
        assert not report['density']['peak_violation']
    assert scores[0] < scores[1]
    assert (dense, sparse, plan, settings) == before


@pytest.mark.parametrize('continuous,raw_only', [(True,False),(True,True),(False,False)])
def test_actual_pipeline_ranks_playable_mix_but_preserves_raw_and_legacy(
        tmp_path,monkeypatch,continuous,raw_only):
    from malody_studio import advanced, section_plan, mapperatorinator
    from malody_studio.density_calibration import freeze
    from malody_studio.generation_context import contract
    from malody_studio.quality_workflow import candidate_contract
    source=tmp_path/'source.wav'
    sf.write(source,np.full((10*44100,2),.1,np.float32),44100,subtype='FLOAT')
    monkeypatch.setattr(section_plan,'rhythm_features',lambda data: ([
        {'start_sample':i*44100,'end_sample':(i+1)*44100,'active_fraction':1.,
         'onset_rate':8.,'rms':.1} for i in range(10)],np.ones(2000),[],.005))
    settings=advanced.defaults()
    settings.update(strategy='independent',dynamic_enabled=True)
    settings['density_policy']=freeze(settings)
    plan=section_plan.build_plan(source,settings,{'bpm':120,'manual':True})
    dense=[Note(i*50,i%4) for i in range(200)]
    sparse=[Note(i*250,i%4) for i in range(39)]
    rounds=[dense,sparse,[Note(i*400,i%4) for i in range(20)]]
    before=deepcopy(rounds)
    calls=[]
    def infer(source,folder,options,progress):
        calls.extend(deepcopy(options['_advanced_presets']))
        results={}
        for row in options['_advanced_presets']:
            results[row['key']]=(rounds[0],[[0,120]])
            for index in (1,2):
                results[row['key']+'__density_retry'+str(index)]=(rounds[index],[[0,120]])
        return results,{}
    monkeypatch.setattr(mapperatorinator,'generate',infer)
    snapshot={'project':{'id':'a'*32,'title':'test','artist':'test','samples':10*44100,
                         'tempo':{'bpm':120,'manual':True}},
              'segment':{'id':'b'*32,'start_sample':0,'end_sample':10*44100},
              'settings':settings,'section_plan':plan,'_stem_raw_only':raw_only,'_density_raw_supply':True,
              'candidate_policy':candidate_contract(),
              'variants':[{'key':'balanced--expert','pattern':'balanced','difficulty':'expert'}]}
    if continuous:snapshot['generation_context_policy']=contract()
    else:
        monkeypatch.setattr(advanced_generation,'rank_playable_model_candidate',
                            lambda *a,**kw:pytest.fail('legacy invoked the new selection view'))
    result=advanced_generation.run(source,tmp_path/'output',{'_advanced':snapshot},lambda *_:None)
    assert not result['errors']
    cache=json.loads((tmp_path/'output'/'balanced-mother.json').read_text(encoding='utf-8'))
    selected_round=0 if continuous and not raw_only else 1
    assert {row['selected_round'] for row in cache['records']}=={selected_round}
    raw=next(row for row in result['advanced_result'] if row['kind']=='model_raw')
    assert [(e['start_ms'],e['lane'],e['end_ms']) for e in raw['events']]==[
        (float(n.start),n.lane,n.end) for n in rounds[selected_round]]
    assert rounds==before
    assert len(calls)==1 and calls[0]['sr']==settings['conditions']['v32']['expert']
    if continuous and not raw_only:
        record=cache['records'][0]
        assert record['density_selection_policy']=='frozen-playable-model-round-v1'
        assert record['attempts'][0]['selection_view']['calibrated_heads']>len(sparse)
        assert all(row['selection_view']['candidate_acoustic_heads']==0 for row in record['attempts'])
    else:
        assert all('density_selection_policy' not in row for row in cache['records'])
