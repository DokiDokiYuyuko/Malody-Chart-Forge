import copy
import pytest
from malody_studio.density_validation import evaluate, contract, candidate_rank, RATES, PEAKS
from malody_studio.density_calibration import freeze, condition_next, highest_requested, initial_condition


def plan(key='hard', seconds=10, active=1., rate=None, peak=None):
    rate=RATES[key] if rate is None else rate
    return {'sample_rate':1000,'sections':[{'id':'a','core':[0,seconds*1000],
        'active_seconds':seconds*active,
        'profile':[{'start_sample':0,'end_sample':seconds*1000,'active_fraction':active}],
        'per_difficulty':{key:{'target_rate':rate,'target_heads_soft':seconds*active*rate,
                             'hard_caps':{'peak_1s':PEAKS[key] if peak is None else peak}}}}]}


def events(count, start=0, end=10000):
    return [{'start_ms':start+(end-start)*i/count,'lane':i%4,'end_ms':None} for i in range(count)]


@pytest.mark.parametrize('key',list(RATES))
def test_six_difficulties_contract_and_count_band(key):
    p=plan(key)
    report=evaluate(events(round(RATES[key]*10)),p,key,[0,10000])
    assert report['status']=='pass'
    assert report['target_nps']==RATES[key]
    assert report['peak_cap']==PEAKS[key]
    assert evaluate(events(1),p,key,[0,10000])['status']=='underfilled'


def test_model_span_never_shrinks_acoustic_denominator_and_duration_only_affects_whole_rate():
    result=evaluate(events(10,5000,5500),plan(), 'hard',[0,10000],duration=20)
    assert result['active_seconds']==10
    assert result['target_heads']==85
    assert result['whole_nps']==.5
    assert result['acoustic_capacity']=={'heads':None,'known':False}


def test_local_hole_not_hidden_by_global_total():
    p=plan('easy'); second=copy.deepcopy(p['sections'][0]);second.update(id='b',core=[10000,20000])
    second['profile']=[{'start_sample':10000,'end_sample':20000,'active_fraction':1}]
    p['sections'].append(second)
    result=evaluate(events(50,10000,20000),p,'easy',[0,20000])
    assert result['global_count_passed']
    assert result['status']=='underfilled'
    assert result['local_deficits'][0]['section_id']=='a'


def test_rolling_peak_crosses_region_seam():
    p=plan();p['sections'][0]['core']=[0,5000]
    p['sections'][0]['profile'][0]['end_sample']=5000
    p['sections'][0]['active_seconds']=5;p['sections'][0]['per_difficulty']['hard']['target_heads_soft']=42.5
    second=copy.deepcopy(p['sections'][0]);second.update(id='b',core=[5000,10000])
    second['profile']=[{'start_sample':5000,'end_sample':10000,'active_fraction':1}];p['sections'].append(second)
    result=evaluate(events(9,4750,4990)+events(9,5010,5250),p,'hard',[0,10000])
    assert result['peak_nps']==18
    assert result['status']=='constraints'


def test_peak_is_ceiling_and_custom_frozen_quota_is_authoritative():
    p=plan('hard',rate=2,peak=20)
    p['sections'][0]['per_difficulty']['hard']['target_heads_soft']=24
    result=evaluate(events(24),p,'hard',[0,10000])
    assert result['status']=='pass' and result['peak_nps']<20
    partial=evaluate(events(6,5000,10000),p,'hard',[5000,10000])
    assert partial['target_heads']==12


def test_owned_active_profile_integration_is_not_uniform_duration_scaling():
    p=plan();p['sections'][0]['active_seconds']=5
    p['sections'][0]['per_difficulty']['hard']['target_heads_soft']=42.5
    p['sections'][0]['profile']=[{'start_sample':0,'end_sample':5000,'active_fraction':0},
                                 {'start_sample':5000,'end_sample':10000,'active_fraction':1}]
    report=evaluate([],p,'hard',[5000,10000])
    assert report['active_seconds']==5 and report['target_heads']==42.5


def test_causes_and_retry_cap():
    p=plan()
    assert evaluate([],p,'hard',[0,10000],candidates={'model_heads':3})['cause']=='unknown'
    assert evaluate([],p,'hard',[0,10000],candidates={'model_heads':3,'model_supply_failure':True})['cause']=='model_supply'
    assert evaluate([],p,'hard',[0,10000],candidates={'acoustic_capacity_heads':3})['cause']=='acoustic_capacity'
    assert evaluate([],p,'hard',[0,10000],candidates={'model_heads':90,'constraint_removed':90})['cause']=='constraints'
    assert not evaluate([],p,'hard',[0,10000],attempts=2)['retry_allowed']
    assert evaluate([],None,'hard',[0,10000])['status']=='not_evaluated'
    assert evaluate([],plan(rate=100,peak=5),'hard',[0,10000])['status']=='infeasible'


def test_freeze_detached_and_inverse_mapping_is_not_monotonic():
    settings={'conditions':{'v32':{'hard':4.6}},'difficulty_rules':{'hard':{'rate':8.5}}}
    rows=[{'engine':'v32','condition':9,'achieved_rate':3},
          {'engine':'v32','condition':4,'achieved_rate':8.5},
          {'engine':'v32','condition':6,'achieved_rate':12}]
    policy=freeze(settings,rows);settings['conditions']['v32']['hard']=1;rows[1]['achieved_rate']=0
    assert condition_next(policy,'v32','hard')==4
    assert condition_next(policy,'v32','hard',used_conditions=[4])==6
    assert condition_next(policy,'v32','hard',attempt=2) is None
    assert freeze({})['calibration_status']=='experimental_fallback'
    assert highest_requested(['hard','master','lunatic'])=='lunatic'
    frozen=contract();frozen['rates']['hard']=100
    assert contract()['rates']['hard']==8.5


def test_complete_candidate_selection_prefers_structural_validity_over_more_heads():
    clean=events(60);bad=events(85)
    bad[1]=dict(bad[0])
    p=plan()
    assert candidate_rank(clean,evaluate(clean,p,'hard',[0,10000]))<candidate_rank(bad,evaluate(bad,p,'hard',[0,10000]))


def test_profile_clipped_to_core_and_holdout_never_selects_initial_condition():
    p=plan();p['sections'][0]['core']=[500,9500]
    p['sections'][0]['active_seconds']=9;p['sections'][0]['per_difficulty']['hard']['target_heads_soft']=76.5
    report=evaluate([],p,'hard',[500,9500])
    assert report['target_heads']==76.5
    policy=freeze({},[{'engine':'v32','condition':3,'achieved_rate':8.5,'split':'holdout'},
                     {'engine':'v32','condition':7,'achieved_rate':8.6,'source_role':'vocals','pattern':'stream'},
                     {'engine':'v32','condition':6,'achieved_rate':9,'source_role':'mix','pattern':'stream'}])
    assert initial_condition(policy,'v32','hard',4.6,source_role='mix',pattern='stream')==4.6
    assert initial_condition(policy,'v32','hard',4.6,source_role='mix',pattern='balanced')==4.6


def test_inverse_aggregates_recordings_equally_instead_of_cherry_picking_closest_trial():
    rows=[]
    for recording,condition,response in [('a',4,8.5),('b',4,20),('a',6,9),('b',6,10)]:
        rows.append({'engine':'v32','source_role':'mix','pattern':'balanced','recording_id':recording,
                     'condition':condition,'achieved_rate':response})
    rows.append({**rows[0],'recording_id':'hold','condition':3,'split':'holdout'})
    policy=freeze({},rows)
    assert condition_next(policy,'v32','hard',source_role='mix',pattern='balanced')==6
    # Hundred duplicates of recording a at condition 4 must not outvote b.
    repeated=freeze({},rows+[dict(rows[0]) for _ in range(100)])
    assert condition_next(repeated,'v32','hard',source_role='mix',pattern='balanced')==6
    curve=next(row for row in repeated['response_curves'] if row['condition']==4)
    assert curve['recording_count']==2 and curve['achieved_rate']==14.25
    assert all(row['condition']!=3 for row in policy['response_curves'])
    assert len(policy['policy_hash'])==64
    assert policy['policy_hash']==freeze({},rows)['policy_hash']
    assert policy['policy_hash']!=repeated['policy_hash']


def test_invalid_or_failed_trials_do_not_fit_and_policy_support_is_explicit():
    rows=[{'engine':'v32','condition':4,'achieved_rate':8.5,'status':'failed'},
          {'engine':'v32','condition':5,'achieved_rate':9,'split':'holdout'}]
    policy=freeze({},rows)
    assert policy['calibration_status']=='experimental_fallback'
    assert policy['response_curves']==[]
    assert initial_condition(policy,'v32','hard',4.6)==4.6


def test_policy_hash_covers_generation_settings_and_peak_failure_is_not_supply_retry():
    policy=freeze({'ln_ratio':.2,'v32_temperature':.9})
    assert policy['policy_hash']!=freeze({'ln_ratio':.4,'v32_temperature':.9})['policy_hash']
    assert policy['generation_settings']['ln_ratio']==.2
    result=evaluate(events(20,100,500),plan(),'hard',[0,10000],candidates={'model_heads':20})
    assert result['status']=='constraints' and result['cause']=='constraints'
    assert not result['retry_allowed']
