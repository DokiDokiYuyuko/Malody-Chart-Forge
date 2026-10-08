import copy

import pytest

from malody_studio.adaptive_difficulty import Candidate
from malody_studio.charts import Note
from malody_studio.chart_quality import apply
from malody_studio.density_validation import evaluate
from malody_studio.quality_workflow import candidate_events


def test_unknown_density_failure_is_not_automatically_a_model_failure():
    plan={'sample_rate':1000,'sections':[{'id':'s','core':[0,10000],'active_seconds':10,
        'per_difficulty':{'expert':{'target_heads_soft':130,'target_rate':13,
            'hard_caps':{'peak_1s':19}}}}]}
    result=evaluate([],plan,'expert',[0,10000],candidates={'model_heads':3})
    assert result['cause']=='unknown' and result['reason_label']=='原因待核验'
    assert not result['retry_allowed']
    known=evaluate([],plan,'expert',[0,10000],candidates={'model_heads':3,'model_supply_failure':True})
    assert known['cause']=='model_supply' and known['retry_allowed']


def test_materialization_preserves_both_model_chord_heads_and_audio_proof():
    raw=[{'id':'tap','start_ms':100.,'lane':0,'end_ms':None},
         {'id':'hold','start_ms':100.,'lane':1,'end_ms':450.}]
    proof={'detections':[{'cache_id':'frozen-audio','sample':8820,'strength':2.}]}
    candidates=[Candidate(100.,2.,[Note(100.,0),Note(100.,1,450.)]),
                Candidate(200.,2.,[],{'kind':'acoustic'},proof)]
    before=copy.deepcopy(raw)
    result=candidate_events([Note(100.,0),Note(100.,1,450.),Note(200.,2)],candidates,raw,
        raw_revision_id='immutable-parent',source_id='original-pcm')
    assert raw==before
    assert [e['origins'][0]['note_id'] for e in result[:2]]==['tap','hold']
    assert result[1]['end_ms']==450.
    assert result[2]['origins'][0]['candidate_kind']=='acoustic'
    assert result[2]['candidate_support']==proof and result[2]['end_ms'] is None


def test_audio_peak_cannot_be_expanded_to_a_chord_or_hold():
    peak=Candidate(100.,2.,[],{'kind':'acoustic'},{'detections':[{'sample':4410}]})
    with pytest.raises(ValueError,match='多个'):
        candidate_events([Note(100.,0),Note(100.,1)],[peak],[],source_id='pcm')
    with pytest.raises(ValueError,match='长条'):
        candidate_events([Note(100.,0,400.)],[peak],[],source_id='pcm')


def test_accent_alignment_cannot_create_a_peak_violation():
    events=[{'id':'ln','start_ms':997.,'lane':0,'end_ms':1400.},
            {'id':'tap','start_ms':1006.,'lane':1,'end_ms':None},
            {'id':'later','start_ms':1999.,'lane':2,'end_ms':None}]
    onset={'time_ms':1000.,'strength':2.,'uncertainty_ms':2.,'multiscale_agreement':True}
    evidence={'heads':{'ln':{'onsets':[onset],'sustain_support':1.,'important_sustain':True},
                       'tap':{'onsets':[onset]}}}
    settings={'difficulty_rules':{'expert':{'peak':2}}}
    result=apply(events,settings,evidence,'expert','balanced')
    assert [e['start_ms'] for e in result['events']]==[997.,1006.,1999.]
    assert any(d['type']=='chord_rollback' and d['reason']=='rolling_peak_cap' for d in result['decisions'])


def test_experimental_snapshot_is_forwarded_to_worker_request():
    from malody_studio.mapperatorinator import build_worker_request
    request=build_worker_request('audio.wav','output',{'title':'song','artist':'artist',
        'seed':1,'ln_ratio':.15,'experimental_parameter_snapshot':True})
    assert request['experimental_parameter_snapshot'] is True


def test_alignment_checks_stricter_neighbor_window_ceiling():
    events=[{'id':'early','start_ms':0.,'lane':0,'end_ms':None},
            {'id':'a','start_ms':1003.,'lane':1,'end_ms':None},
            {'id':'b','start_ms':1008.,'lane':2,'end_ms':None}]
    onset={'time_ms':998.,'strength':2.,'uncertainty_ms':2.,'multiscale_agreement':True}
    evidence={'heads':{key:{'onsets':[onset]} for key in ('a','b')}}
    plan={'sections':[{'core':[0,500*44.1],'per_difficulty':{'expert':{'hard_caps':{'peak_1s':2}}}},
        {'core':[500*44.1,3000*44.1],'per_difficulty':{'expert':{'hard_caps':{'peak_1s':19}}}}]}
    result=apply(events,{},evidence,'expert','balanced',plan=plan)
    assert result['events']==events
    assert result['decisions'][-1]['reason']=='rolling_peak_cap'


def test_parent_clock_experiments_cannot_enter_production_calibration():
    from malody_studio.density_calibration import freeze,response_curves
    normal={'engine':'v32','condition':5.9,'achieved_rate':4.,'recording_id':'normal',
            'source_role':'vocals','pattern':'balanced','status':'completed'}
    experiments=[{**normal,'condition':8.,'reference_mode':'revision'},
        {**normal,'condition':7.,'experimental_only':True},
        {**normal,'condition':6.,'reference_qualified':False},
        {**normal,'condition':5.,'reference':{'qualified':False}}]
    assert [r['condition'] for r in response_curves([normal]+experiments)]==[5.9]
    assert freeze({},experiments)['response_curves']==[]


def test_candidate_policy_preserves_frozen_v1_and_rejects_mutation():
    from malody_studio.quality_workflow import candidate_contract
    from malody_studio.advanced_generation import validate_frozen_policies
    old=candidate_contract('evidenced-single-heads-v1');new=candidate_contract()
    validate_frozen_policies({'candidate_policy':old})
    validate_frozen_policies({'candidate_policy':new})
    assert 'selection_policy' not in old and new['selection_policy']=='model_only'
    assert candidate_contract('evidenced-single-heads-v2')['selection_policy']=='model_skeleton_then_audio'
    with pytest.raises(ValueError):candidate_contract('unknown')
    mutated={**new,'audio_vote_cap':9.}
    with pytest.raises(ValueError):validate_frozen_policies({'candidate_policy':mutated})


def test_assembly_peak_checks_touching_regions_after_audio_mapping():
    from malody_studio.quality_workflow import assembly_density
    from malody_studio.advanced import SR
    report={'version':'fixture','passed':True,'target_heads':6.,'actual_heads':6,'active_seconds':2.,
        'peak_cap':2,'attempts':0,'status':'pass','regions':[
            {'bounds':[0,SR],'peak_ceiling':2},
            {'bounds':[5*SR,6*SR],'peak_ceiling':4}]}
    revision={'provenance':{'density_validation':report}}
    events=[{'start_ms':t} for t in [100.,300.,5100.,5200.,5300.,5400.]]
    check=assembly_density([revision],events,6.)
    assert check['passed'] and check['peak_nps']==4
    # Removing the musical gap puts four high-region heads into a rolling
    # second touching the stricter previous region; output must reject it.
    compact=[{'start_ms':t} for t in [1600.,1800.,2600.,2700.,2800.,2900.]]
    mapping=[{'source_start':0,'source_end':SR,'output_start':1.5*SR},
             {'source_start':5*SR,'source_end':6*SR,'output_start':2.5*SR}]
    check=assembly_density([revision],compact,3.5,mapping)
    assert not check['passed'] and check['status']=='constraints'


@pytest.fixture
def replay_project(tmp_path,monkeypatch):
    import numpy as np
    import soundfile as sf
    from malody_studio import advanced,quality_workflow,section_plan,separation,stem_generation
    monkeypatch.setattr(advanced,'audio_metadata',lambda _: ([],{'bpm':120,'uncertain':True}))
    source=tmp_path/'song.wav';sf.write(source,np.ones((4*advanced.SR,2),np.float32)*.1,advanced.SR,subtype='FLOAT')
    store=advanced.ProjectStore(tmp_path/'projects');p=store.create(source,'Replay')
    sid='a'*32;plan_id='b'*32;parents=[]
    for i in range(2):
        p=store.add_segment(p['id'],{'start_sample':i*2*advanced.SR,'end_sample':(i+1)*2*advanced.SR})
        part=p['segments'][-1];raw=[]
        for j,role in enumerate(('vocals','accompaniment')):
            r=store.add_revision(p['id'],part['id'],'balanced--hard',
                [{'id':role,'start_ms':i*2000+100+j*100.,'lane':j,'end_ms':None}],p['settings'],'stem_raw',
                {'source_role':role,'source_id':sid+':'+role,'stem_set_id':sid,'parent_source_id':p['source_pcm_sha256']},activate_initial=False)
            raw.append(r)
        fused=store.add_revision(p['id'],part['id'],'balanced--hard',raw[0]['events'],p['settings'],'fusion',
            {'parents':[r['id'] for r in raw],'stem_set_id':sid,'applied_plan_hash':plan_id})
        parents.append(fused)
    directory=store.directory(p['id'])
    plan={'id':plan_id,'source_pcm_sha':p['source_pcm_sha256'],'samples':4*advanced.SR,'sample_rate':advanced.SR,
          'sections':[{'id':str(i),'core':[i*2*advanced.SR,(i+1)*2*advanced.SR],'active_seconds':2.,
              'per_difficulty':{'hard':{'target_heads_soft':2.,'target_rate':1.,'hard_caps':{'peak_1s':13}}}}
              for i in range(2)]}
    advanced.atomic(directory/'section-plans'/(plan_id+'.json'),plan)
    advanced.atomic(directory/'stems'/sid/'manifest.json',{'id':sid})
    monkeypatch.setattr(section_plan,'validate_plan',lambda plan,*_:plan)
    monkeypatch.setattr(separation,'resolve_source',lambda store,pid,source_id:{'source_id':source_id,
        'source_role':source_id.split(':')[1],'path':str(source)})
    def energy(rows,*_):
        for raw in rows.values():
            for event in raw['events']:event['audio_evidence']={'audible':True,'salience':1.}
    monkeypatch.setattr(stem_generation,'_audio_evidence',energy)
    monkeypatch.setattr(quality_workflow,'evidence_for',lambda *_:{'id':'frozen-proof','sources':{}})
    payload={'request_id':advanced.uid(),'revision_ids':[r['id'] for r in parents],
             'expected_revision':store.load(p['id'])['revision']}
    return store,p,payload,parents


def test_full_workflow_replay_is_one_atomic_inactive_budget_pass(replay_project):
    from malody_studio.quality_workflow import derive_workflow
    store,p,payload,parents=replay_project;before=store.load(p['id'])
    result=derive_workflow(store,p['id'],payload);after=store.load(p['id'])
    assert after['revision']==before['revision']+1
    assert [s['active'] for s in after['segments']]==[s['active'] for s in before['segments']]
    assert derive_workflow(store,p['id'],payload)==result and store.load(p['id'])==after
    children=[store.revision(p['id'],r['id']) for r in result['revisions']]
    assert sum(len(r['events']) for r in children)==4
    assert all(r['provenance']['budget_stage']==1 for r in children)
    assert all(store.revision(p['id'],r['id'])==r for r in parents)
    different={**payload,'revision_ids':payload['revision_ids'][::-1]}
    with pytest.raises(ValueError,match='同一请求'):derive_workflow(store,p['id'],different)


def test_replay_commit_crash_recovers_without_duplicate_versions(replay_project,monkeypatch):
    from malody_studio import quality_workflow
    store,p,payload,_=replay_project;real_atomic=quality_workflow.atomic
    def interrupt_result(path,data):
        if data.get('result') and not data.get('pending'):raise OSError('simulated final record crash')
        return real_atomic(path,data)
    monkeypatch.setattr(quality_workflow,'atomic',interrupt_result)
    with pytest.raises(OSError):quality_workflow.derive_workflow(store,p['id'],payload)
    after=store.load(p['id'])
    monkeypatch.setattr(quality_workflow,'atomic',real_atomic)
    result=quality_workflow.derive_workflow(store,p['id'],payload)
    assert store.load(p['id'])==after and result['project_revision']==after['revision']
    assert len(result['revisions'])==2


def test_batch_checks_every_candidate_before_writing(replay_project):
    from malody_studio import advanced
    store,p,_,parents=replay_project;before=store.load(p['id'])
    rows=[{**copy.deepcopy(r),'id':advanced.uid(),'kind':'quality'} for r in parents]
    rows[1]['events'][0]['lane']=4
    with pytest.raises(ValueError):store.add_candidate_batch(p['id'],rows,before['revision'])
    assert store.load(p['id'])==before
    assert all(not (store.directory(p['id'])/'revisions'/(r['id']+'.json')).exists() for r in rows)
