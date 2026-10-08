"""Actual project transactions; acoustic/model work is replaced with fixed proof."""
import copy
from pathlib import Path
import numpy as np
import pytest
import soundfile as sf
from malody_studio import advanced, quality_workflow, stem_generation
from malody_studio.chart_quality import classify_holds


def test_new_candidate_contract_only_uses_model_heads_and_keeps_legacy_explicit():
    current=quality_workflow.candidate_contract()
    assert current['selection_policy']=='model_only'
    assert current['acoustic_new_heads'] is False
    assert current['budget_passes']==1
    assert quality_workflow.candidate_contract('evidenced-single-heads-v2')['selection_policy']=='model_skeleton_then_audio'
    assert 'selection_policy' not in quality_workflow.candidate_contract('evidenced-single-heads-v1')


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(advanced,'audio_metadata',lambda _: ([],{'bpm':120,'points':[[0,120]],'uncertain':True}))
    source=tmp_path/'input.wav';sf.write(source,np.zeros((advanced.SR*22,2),np.float32),advanced.SR,subtype='FLOAT')
    store=advanced.ProjectStore(tmp_path/'projects');p=store.create(source,'Quality transactions')
    monkeypatch.setattr(quality_workflow,'evidence_for',lambda *_: {'id':'fixed-proof','sources':{}})
    return store,p


def add_part(store,p,start,end,events):
    p=store.add_segment(p['id'],{'start_sample':int(start*advanced.SR),'end_sample':int(end*advanced.SR)})
    part=p['segments'][-1]
    revision=store.add_revision(p['id'],part['id'],'balanced--hard',events,p['settings'],'model_raw')
    return revision


def request(store,p,revisions,identity='quality-request-01'):
    return {'request_id':identity,'revision_ids':[r['id'] for r in revisions],
            'expected_revision':store.load(p['id'])['revision']}


def test_derivation_replay_preserves_all_1736_heads_and_old_active(project):
    store,p=project;revisions=[];remaining=1736
    for i in range(21):
        count=min(83,remaining);remaining-=count
        events=[dict(id=f'head-{j}',start_ms=i*1000+10+j*10,end_ms=None,lane=j%4) for j in range(count)]
        revisions.append(add_part(store,p,i,i+1,events))
    payload=request(store,p,revisions);before=store.load(p['id']);originals=copy.deepcopy(revisions)
    first=quality_workflow.derive_many(store,p['id'],payload)
    after=store.load(p['id']);second=quality_workflow.derive_many(store,p['id'],payload)
    assert second==first and store.load(p['id'])==after
    assert [part['active'] for part in before['segments']]==[part['active'] for part in after['segments']]
    derived=[store.revision(p['id'],row['id']) for row in first['revisions']]
    assert sum(len(row['events']) for row in derived)==1736
    assert first['quality_summary']['heads_before']==first['quality_summary']['heads_after']==1736
    for parent,child in zip(originals,derived):
        assert store.revision(p['id'],parent['id'])==parent
        assert [(e['start_ms'],e['lane'],e['end_ms']) for e in child['events']]==[(e['start_ms'],e['lane'],e['end_ms']) for e in parent['events']]
        assert {origin['note_id'] for e in child['events'] for origin in e['origins']}=={e['id'] for e in parent['events']}


def test_mixed_ln_tap_derivation_uses_head_and_keeps_absolute_tail(project,monkeypatch):
    store,p=project
    raw=[dict(id='ln',start_ms=997.,end_ms=1400.,lane=0),dict(id='tap',start_ms=1006.,end_ms=None,lane=1)]
    parent=add_part(store,p,0,2,raw)
    def evidence(*_):
        return {'id':'common-accent','heads':{parent['id']+':'+e['id']:{'onsets':[dict(time_ms=1000.,strength=2.,uncertainty_ms=2.,multiscale_agreement=True)]} for e in raw}}
    monkeypatch.setattr(quality_workflow,'evidence_for',evidence)
    result=quality_workflow.derive_many(store,p['id'],request(store,p,[parent]))
    child=store.revision(p['id'],result['revisions'][0]['id'])
    assert [e['start_ms'] for e in child['events']]==[1000.,1000.]
    assert child['events'][0]['end_ms']==1400.
    assert store.revision(p['id'],parent['id'])['events']==raw


def test_expected_revision_conflict_is_checked_before_deriving(project):
    store,p=project;parent=add_part(store,p,0,1,[dict(id='n',start_ms=100.,end_ms=None,lane=0)])
    payload=request(store,p,[parent]);store.update(p['id'],{'title':'New name'})
    before=store.load(p['id'])
    with pytest.raises(Exception,match='项目已变化|版本|更新|revision'):
        quality_workflow.derive_many(store,p['id'],payload)
    assert store.load(p['id'])==before


def test_candidate_export_keeps_adoption_and_checks_every_selected_combo(project):
    store,p=project
    first=add_part(store,p,0,1,[dict(id='head',start_ms=100.,end_ms=None,lane=0)])
    candidate=store.add_revision(p['id'],first['segment_id'],first['variant'],
        [dict(id='new-head',start_ms=200.,end_ms=None,lane=1)],p['settings'],'quality',activate_initial=False)
    store.update(p['id'],{'tail_trim_enabled':False,'difficulties':['hard','expert']})
    before=store.load(p['id'])
    # Other default combinations are intentionally missing. Candidate selection
    # must not let require_complete bypass this preflight or adopt the candidate.
    with pytest.raises(ValueError,match='所有所选组合'):
        advanced.assemble(store,p['id'],revision_overrides={first['segment_id']:{first['variant']:candidate['id']}},require_complete=True)
    assert store.load(p['id'])==before


def test_density_whole_average_uses_effective_song_but_frozen_active_budget(project,monkeypatch):
    store,p=project
    p['tail_trim']={'enabled':True,'cutoff_sample':10*advanced.SR}
    bounds=[0,10*advanced.SR]
    plan={'sample_rate':advanced.SR,'sections':[{'id':'s','core':bounds,'active_seconds':5.,
          'per_difficulty':{'hard':{'target_rate':8.5,'target_heads_soft':42.5,'hard_caps':{'peak_1s':13}}}}]}
    snapshot={'project':p,'segment':{'start_sample':bounds[0],'end_sample':bounds[1]},
              'variants':[{'pattern':'balanced','difficulty':'hard'}],'section_plan':plan}
    rows=[dict(id=str(i),start_ms=i*100.,end_ms=None,lane=i%4) for i in range(40)]
    checked=quality_workflow.finish(rows,p['settings'],snapshot,'unused',store.directory(p['id']),evidence={'heads':{}})
    report=checked['density_validation']
    assert report['whole_nps']==pytest.approx(4.)
    assert report['active_nps']==pytest.approx(8.)
    assert report['target_heads']==pytest.approx(42.5)


def test_concurrent_change_during_analysis_cannot_commit_quality(project,monkeypatch):
    store,p=project;parent=add_part(store,p,0,1,[dict(id='n',start_ms=100.,end_ms=None,lane=0)])
    payload=request(store,p,[parent]);before=store.load(p['id'])
    def changed(*_):
        store.update(p['id'],{'title':'Changed during analysis'});return {'id':'proof'}
    monkeypatch.setattr(quality_workflow,'evidence_for',changed)
    with pytest.raises(Exception):quality_workflow.derive_many(store,p['id'],payload)
    after=store.load(p['id'])
    assert [s['versions'] for s in before['segments']]==[s['versions'] for s in after['segments']]


def test_preplanning_hold_classification_uses_no_raw_count_quota():
    rows=[dict(id=str(i),start_ms=1000.,end_ms=1300.,lane=i) for i in range(4)]
    evidence={'heads':{str(i):{'sustain_support':.9,'rearticulations':0} for i in range(4)}}
    assert classify_holds(rows,evidence)['events']==rows
    evidence['heads']['0']['sustain_support']=.1
    changed=classify_holds(rows,evidence)
    assert len(changed['events'])==4 and changed['events'][0]['end_ms'] is None
    assert all(e['end_ms']==1300. for e in changed['events'][1:])
    assert rows[0]['end_ms']==1300.


def test_raw_budget_derivation_is_idempotent_and_cannot_repeat_on_finished_result(project,monkeypatch):
    from malody_studio import section_plan
    store,p=project
    parent=add_part(store,p,0,22,[dict(id='original',start_ms=1000.,end_ms=None,lane=0)])
    store.update(p['id'],{'tail_trim_enabled':False})
    monkeypatch.setattr(section_plan,'rhythm_features',lambda data:([
        {'start_sample':i*advanced.SR,'end_sample':(i+1)*advanced.SR,'active_fraction':1.,'onset_rate':8.,'rms':.1}
        for i in range(22)],np.ones(4400),[],.005))
    directory=store.directory(p['id']);plan=section_plan.build_plan(directory/'source.wav',p['settings'],{'bpm':120,'manual':True,'points':[[0,120]]})
    advanced.atomic(directory/'section-plans'/(plan['id']+'.json'),plan)
    parent['provenance']['section_plan_id']=plan['id']
    advanced.atomic(directory/'revisions'/(parent['id']+'.json'),parent)
    before=store.load(p['id']);payload=request(store,p,[parent],advanced.uid())
    output=quality_workflow.derive_budget(store,p['id'],payload)
    after=store.load(p['id'])
    assert quality_workflow.derive_budget(store,p['id'],payload)==output
    assert store.load(p['id'])==after
    assert [s['active'] for s in before['segments']]==[s['active'] for s in after['segments']]
    child=store.revision(p['id'],output['revisions'][0]['id'])
    assert len(child['events'])==1 and child['events'][0]['start_ms']==1000.
    assert child['provenance']['budget_stage']==1
    assert child['events'][0]['origins'][0]['note_id']=='original'
    assert store.revision(p['id'],parent['id'])==parent
    with pytest.raises(ValueError,match='原谱'):
        quality_workflow.derive_budget(store,p['id'],request(store,p,[child],advanced.uid()))


def test_finalization_keeps_raw_supply_separate_from_existing_playable(project,monkeypatch):
    store,p=project
    raw={'id':'raw-parent','variant':'balanced--hard','kind':'model_raw','events':[dict(id='raw',start_ms=100.,end_ms=400.,lane=0)],
         'settings':p['settings'],'provenance':{}}
    playable={'variant':'balanced--hard','kind':'rules','events':[dict(id='selected',start_ms=100.,end_ms=None,lane=0)],
              'settings':p['settings'],'provenance':{'global_budget_applied':True,'budget_stage':1}}
    original=copy.deepcopy(raw)
    monkeypatch.setattr(quality_workflow,'finish',lambda rows,*args,**kwargs:{'events':rows,'summary':{},'decisions':[],
                       'density_validation':{'status':'underfilled','actual_heads':len(rows)}})
    snapshot={'project':p,'segment':{'start_sample':0,'end_sample':advanced.SR},'settings':p['settings'],
              'variants':[{'key':'balanced--hard','pattern':'balanced','difficulty':'hard'}],
              'quality_policy':{'version':'fixture'}}
    result=quality_workflow.finalize_generated(store.directory(p['id'])/'source.wav',store.directory(p['id']),
        {'_advanced':snapshot},{'advanced_result':[raw,playable]})
    assert len(result['advanced_result'])==2 and raw==original
    assert playable['provenance']['density_validation']['actual_heads']==1
    origin=playable['events'][0]['origins'][0]
    assert origin['revision_id']=='raw-parent' and origin['note_id']=='raw'
    assert origin['original_end_ms']==400. and playable['events'][0]['end_ms'] is None


def test_preplanning_weak_hold_removal_frees_all_lanes_for_later_head():
    from malody_studio.fusion import fuse_revisions
    from tests.test_fusion import revision,plan,note
    holds=[note(100,lane=i,tail=800,identity=f'h{i}') for i in range(4)]
    for row in holds:row['strength']=100.
    vocals=revision('vocals',holds[:2]);accompaniment=revision('accompaniment',holds[2:]+[note(250,identity='later')])
    before=copy.deepcopy([vocals,accompaniment]);shared=plan(target=20,chord=4,gap=120,peak=8)
    old=fuse_revisions(vocals,accompaniment,shared)
    assert len(old['events'])==4
    evidence={'heads':{row['id']:{'sustain_support':.1} for row in holds}}
    improved=fuse_revisions(vocals,accompaniment,shared,evidence=evidence)
    assert len(improved['events'])==5
    assert [vocals,accompaniment]==before
    assert sum(d['type']=='hold_to_tap_before_planning' for d in improved['decisions'])==4
    from malody_studio.chart_quality import apply
    final=apply(improved['events'],{},evidence,'medium','balanced')
    assert len(final['events'])==len(improved['events'])==5
    assert not any(d['type']=='hold_to_tap' for d in final['decisions'])


def test_dual_supply_reuses_completed_source_and_spends_only_two_shared_rounds(project,tmp_path,monkeypatch):
    from malody_studio import advanced_generation, section_plan, density_calibration
    store,p=project
    p=store.add_segment(p['id'],{'start_sample':0,'end_sample':p['samples']})
    settings=copy.deepcopy(p['settings']);settings['conditions'].setdefault(settings['engine'],{})['hard']=5.9
    directory=store.directory(p['id']);stem_id='b'*64;folder=directory/'stems'/stem_id;folder.mkdir(parents=True)
    stems=[];descriptors=[]
    for role in ('vocals','accompaniment'):
        path=folder/(role+'.wav');sf.write(path,np.zeros((p['samples'],2),np.float32),advanced.SR,subtype='FLOAT')
        row={'role':role,'source_id':stem_id+':'+role,'pcm_sha':role+'-pcm','path':str(path),'file':path.name}
        stems.append(row);descriptors.append({'source_role':role,'source_id':row['source_id'],'pcm_sha':row['pcm_sha']})
    manifest={'id':stem_id,'frame_count':p['samples'],'source_pcm_sha':p['source_pcm_sha256'],'stems':stems,
              'sample_rate':advanced.SR,'origin_sample':0}
    advanced.atomic(folder/'manifest.json',manifest)
    plan={'id':'a'*64,'sample_rate':advanced.SR,'sections':[{'id':'s','core':[0,p['samples']],
          'per_difficulty':{'hard':{'model_condition':5.9}}}]}
    snapshot={'project':p,'segment':p['segments'][0],'settings':settings,'original_source':{'path':str(directory/'source.wav')},
              'input_sources':descriptors,'stem_set_id':stem_id,'section_plan':plan,'auto_fuse':True,
              'variants':[{'key':'balanced--hard','pattern':'balanced','difficulty':'hard'}],
              'density_policy':{'observations':[]},'quality_policy':{'version':'fixture'}}
    monkeypatch.setattr(stem_generation,'ROOT',tmp_path)
    monkeypatch.setattr(stem_generation,'validate_manifest',lambda *_:None)
    monkeypatch.setattr(section_plan,'validate_plan',lambda *_:None)
    monkeypatch.setattr(stem_generation,'_audio_evidence',lambda *_:None)
    monkeypatch.setattr(stem_generation,'_find_existing',lambda *_:None)
    calls=[];fail_once=[True]
    def infer(source,destination,options,progress):
        snap=options['_advanced'];role=snap['source']['source_role']
        condition=snap['settings']['conditions'][settings['engine']]['hard']
        calls.append((role,condition,snap.get('disable_density_retry'),str(destination)))
        if role=='accompaniment' and fail_once[0]:
            fail_once[0]=False;raise RuntimeError('one source failed')
        count=1 if condition==5.9 else int(condition)-4
        rows=[dict(id=role+str(i),start_ms=100.+(150 if role=='accompaniment' else 0)+i*300,
                   end_ms=None,lane=0 if role=='vocals' else 1) for i in range(count)]
        return {'advanced_result':[{'variant':'balanced--hard','events':rows,'settings':snap['settings'],'provenance':{}}]}
    monkeypatch.setattr(advanced_generation,'run',infer)
    shared_fusions=[]
    def fusion(v,a,plan,settings,evidence=None,**kwargs):
        shared_fusions.append((len(v['events']),len(a['events'])))
        return {'events':copy.deepcopy(v['events']+a['events']),'provenance':{'global_budget_applied':True,'budget_stage':1}}
    monkeypatch.setattr(stem_generation,'fuse_revisions',fusion)
    def finish(rows,*args,attempts=0,**kwargs):
        return {'events':rows,'summary':{},'decisions':[],
                'density_validation':{'target_heads':220.,'target_nps':10.,'actual_heads':len(rows),
                'passed':False,'cause':'model_supply','retry_allowed':attempts<2,'attempts':attempts,
                'local_deficits':[{'deficit':220-len(rows)}]}}
    monkeypatch.setattr(quality_workflow,'finish',finish)
    requested=[]
    def condition(policy,engine,key,target_rate,attempt,used_conditions,**kwargs):
        requested.append((attempt,target_rate));return float(6+attempt)
    monkeypatch.setattr(density_calibration,'condition_next',condition)
    options={'_advanced':snapshot}
    partial=stem_generation.run(directory/'source.wav',tmp_path/'first-job',options,lambda *_:None)
    assert len(partial['advanced_result'])==1 and not shared_fusions
    completed=partial['advanced_result'][0]
    second=stem_generation.run(directory/'source.wav',tmp_path/'second-job',options,lambda *_:None)
    assert sum(role=='vocals' and c==5.9 for role,c,_,_ in calls)==1
    # A completed checkpoint that was not committed by the failed parent is
    # carried forward with the same revision ID, without repeating inference.
    assert any(row['id']==completed['id'] for row in second['advanced_result'])
    assert shared_fusions==[(1,1),(2,2),(3,3)]
    extra=[row for row in calls if row[1]!=5.9]
    assert len(extra)==4 and all(row[2] is True for row in extra)
    assert [attempt for attempt,_ in requested]==[0,0,1,1]
    assert all(sum(rate for attempt,rate in requested if attempt==round_index)==pytest.approx(11.5) for round_index in (0,1))
    fused=next(row for row in second['advanced_result'] if row['kind']=='fusion')
    assert fused['provenance']['density_validation']['attempts']==2
    assert not fused['provenance']['density_validation']['retry_allowed']
