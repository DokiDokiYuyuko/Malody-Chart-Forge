import copy
import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from malody_studio import advanced, advanced_api


@pytest.fixture
def context(tmp_path, monkeypatch):
    source=tmp_path/'music.wav'; sf.write(source,np.zeros((advanced.SR*4,2),np.float32),advanced.SR,subtype='FLOAT')
    monkeypatch.setattr(advanced,'audio_metadata',lambda _: ([],{'bpm':120,'points':[[0,120]],'uncertain':True}))
    store=advanced.ProjectStore(tmp_path/'projects');p=store.create(source,'Timeline')
    monkeypatch.setattr(advanced_api,'store',store)
    plan={'id':'a'*64,'samples':p['samples'],'source_pcm_sha':p['source_pcm_sha256'],
          'sections':[{'core':[0,advanced.SR]},{'core':[advanced.SR,2*advanced.SR]},{'core':[2*advanced.SR,p['samples']]}]}
    advanced.atomic(store.directory(p['id'])/'section-plans'/(plan['id']+'.json'),plan)
    app=FastAPI();app.include_router(advanced_api.router)
    with TestClient(app) as client:yield store,p,plan,client,'/api/advanced/projects/'+p['id']


def preview(ctx, **extra):
    store,p,plan,c,url=ctx
    r=c.post(url+'/segmentation-preview',json={'plan_id':plan['id'],'expected_revision':store.load(p['id'])['revision'],**extra})
    assert r.status_code==200,r.text
    return r.json()


def test_preview_does_not_change_project_apply_once_and_undo(context):
    store,p,plan,c,url=context;draft=preview(context)
    assert store.load(p['id'])==p and draft['proposed_count']==3
    applied=c.post(url+'/segmentation-apply',json={'draft_id':draft['id'],'expected_revision':p['revision']})
    assert applied.status_code==200,applied.text
    after=applied.json();assert len(after['segments'])==3 and after['revision']==p['revision']+1
    assert c.post(url+'/segmentation-apply',json={'draft_id':draft['id']}).status_code==409
    restored=c.post(url+'/undo',json={'expected_revision':after['revision']}).json()
    assert restored['segments']==[]
    assert len(c.get(url+'/layouts').json()['layouts'])==1


def test_manual_boundaries_candidates_crossing_holds_and_reviews_preserved(context):
    store,p,plan,c,url=context
    p=store.add_segment(p['id'],{'start_sample':0,'end_sample':int(2.5*advanced.SR),'name':'人工范围'})
    parent=p['segments'][0]
    events=[{'id':'hold','start_ms':500,'end_ms':1500,'lane':0},{'id':'boundary','start_ms':1000,'end_ms':None,'lane':1}]
    original=store.add_revision(p['id'],parent['id'],'balanced--easy',events,p['settings'],'model_raw')
    alternate=store.add_revision(p['id'],parent['id'],'balanced--easy',[{'id':'candidate','start_ms':1100,'end_ms':None,'lane':2}],p['settings'],'rules',activate_initial=False)
    before=copy.deepcopy(store.load(p['id'])['segments']);draft=preview(context)
    assert draft['segments'][-1]['end_sample']==int(2.5*advanced.SR)
    response=c.post(url+'/segmentation-apply',json={'draft_id':draft['id']});assert response.status_code==200,response.text;result=response.json();children=result['segments']
    assert len(children)==3
    assert all(len(s['versions']['balanced--easy'])==2 for s in children)
    inherited=store.revision(p['id'],children[0]['active']['balanced--easy'])
    assert inherited['events']==[events[0]] and inherited['events'][0]['end_ms']==1500
    assert store.revision(p['id'],children[1]['active']['balanced--easy'])['events']==[events[1]]
    assert store.revision(p['id'],original['id'])['events']==events
    assert c.get(url+'/layouts').json()['layouts'][0]['segments']==before
    assert store.revision(p['id'],alternate['id'])['events'][0]['id']=='candidate'


def test_invalid_draft_and_running_task_do_not_mutate(context,monkeypatch):
    store,p,plan,c,url=context
    bad=c.post(url+'/segmentation-preview',json={'plan_id':plan['id'],'cuts':[1]})
    assert bad.status_code==400 and store.load(p['id'])['segments']==[]
    p=store.add_segment(p['id'],{'start_sample':0,'end_sample':p['samples']})
    draft=preview(context)
    monkeypatch.setattr(advanced_api,'project_tasks',lambda _: {'tasks':[{'segment_id':p['segments'][0]['id'],'status':'running'}]})
    result=c.post(url+'/segmentation-apply',json={'draft_id':draft['id']})
    assert result.status_code==409 and store.load(p['id'])==p


def test_batch_preflight_frozen_manual_selection_and_duplicate_request(context,monkeypatch):
    from malody_studio import server
    store,p,plan,c,url=context
    p=store.add_segment(p['id'],{'start_sample':0,'end_sample':advanced.SR})
    p=store.add_segment(p['id'],{'start_sample':advanced.SR,'end_sample':p['samples']})
    captured=[]
    monkeypatch.setattr(server,'ensure_engine',lambda _:None)
    monkeypatch.setattr(server,'enqueue_jobs',lambda entries:captured.extend(copy.deepcopy(entries)) or [{'id':advanced.uid()} for _ in entries])
    settings={**p['settings'],'dynamic_enabled':False,'fixed_seed':True}
    payload={'request_id':advanced.uid(),'segment_ids':[s['id'] for s in reversed(p['segments'])], 'settings':settings,'expected_revision':p['revision']}
    bad=c.post(url+'/generation-batches',json={**payload,'segment_ids':[p['segments'][0]['id'],'f'*32]})
    assert bad.status_code==400 and not captured
    result=c.post(url+'/generation-batches',json=payload)
    assert result.status_code==200,result.text
    assert len(captured)==2 and all(entry[0]['_advanced']['activate_initial'] is False for entry in captured)
    assert captured[0][0]['_advanced']['segment']['start_sample']==0
    assert c.post(url+'/generation-batches',json=payload).json()==result.json() and len(captured)==2
    snapshot=captured[0][0]['_advanced'];store.update(p['id'],{'settings':{**settings,'seed':99}})
    assert snapshot['settings']['seed']!=99
    advanced_api.commit_generated({'_advanced':snapshot},{'bounds':[0,advanced.SR],'advanced_result':[
        {'variant':'balanced--easy','events':[{'id':'n','start_ms':500,'end_ms':None,'lane':0}], 'settings':settings,'kind':'model_raw','provenance':{},'activate_initial':True}]})
    assert not store.load(p['id'])['segments'][0]['active']


def test_select_many_validates_all_before_manifest_commit(context):
    store,p,plan,c,url=context;p=store.add_segment(p['id'],{'start_sample':0,'end_sample':p['samples']});s=p['segments'][0]
    r=store.add_revision(p['id'],s['id'],'balanced--easy',[],p['settings'],'model_raw',activate_initial=False)
    choices=[{'segment_id':s['id'],'variant':'balanced--easy','revision_id':r['id']}]
    before=store.load(p['id'])
    assert c.post(url+'/select-many',json={'choices':choices+[{**choices[0],'variant':'balanced--hard'}]}).status_code==409
    assert store.load(p['id'])==before
    result=c.post(url+'/select-many',json={'choices':choices})
    assert result.status_code==200 and result.json()['segments'][0]['active']['balanced--easy']==r['id']


def test_version_conflict_keeps_draft_available_and_io_failure_keeps_layout(context,monkeypatch):
    from malody_studio import advanced_workbench
    store,p,plan,c,url=context
    p=store.add_segment(p['id'],{'start_sample':0,'end_sample':p['samples']})
    part=p['segments'][0]
    store.add_revision(p['id'],part['id'],'balanced--easy',[{'id':'n','start_ms':500,'lane':0,'end_ms':None}],p['settings'],'model_raw')
    draft=preview(context)
    current=store.load(p['id']);store.edit_segment(p['id'],part['id'],{'name':'Renamed'})
    assert c.post(url+'/segmentation-apply',json={'draft_id':draft['id']}).status_code==409
    assert (store.directory(p['id'])/'segmentation-drafts'/(draft['id']+'.json')).is_file()
    draft=preview(context);before=store.load(p['id']);old_atomic=advanced_workbench.atomic
    def fail(path,data):
        if path.parent.name=='revisions':raise OSError('write unavailable')
        old_atomic(path,data)
    monkeypatch.setattr(advanced_workbench,'atomic',fail)
    assert c.post(url+'/segmentation-apply',json={'draft_id':draft['id']}).status_code==400
    assert store.load(p['id'])==before
