import copy
import time
import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from malody_studio import advanced, advanced_api, music_timing, music_workflow, advanced_plans, paths, server


@pytest.fixture
def context(tmp_path,monkeypatch):
    source=tmp_path/'music.wav';sf.write(source,np.full((advanced.SR*2,2),.1,np.float32),advanced.SR,subtype='FLOAT')
    monkeypatch.setattr(advanced,'audio_metadata',lambda _: ([],{'bpm':120,'points':[[0,120]],'uncertain':True}))
    store=advanced.ProjectStore(tmp_path/'outputs/advanced');p=store.create(source,'Music')
    monkeypatch.setattr(server,'ROOT',tmp_path);monkeypatch.setattr(paths,'ROOT',tmp_path)
    monkeypatch.setattr(server,'dispatch_next',lambda:None)
    monkeypatch.setattr(advanced_api,'store',store)
    _,clock=music_timing.source_contract(store.directory(p['id']),p)
    timing=music_timing.timing_map({'beat_samples':[],'downbeat_samples':[]},clock)
    evidence=music_timing.sealed({'schema':music_timing.SCHEMA,'version':music_timing.VERSION,'policy':music_timing.selected_policy(),'analysis':{'failures':[]},'source':clock,'candidates':[timing],'selected_timing_id':timing['id']})
    monkeypatch.setattr(music_timing,'load_or_analyze',lambda *args:copy.deepcopy(evidence))
    monkeypatch.setattr(server,'ensure_engine',lambda options:None)
    monkeypatch.setattr(server,'jobs',{})
    monkeypatch.setattr(advanced_api,'project_tasks',lambda pid:{'tasks':[]})
    plan={'id':'a'*64,'samples':p['samples'],'source_pcm_sha':p['source_pcm_sha256'],'sections':[{'core':[0,advanced.SR],'rhythm_activity':-.5},{'core':[advanced.SR,p['samples']],'rhythm_activity':.5}]}
    advanced.atomic(store.directory(p['id'])/'section-plans'/(plan['id']+'.json'),plan)
    monkeypatch.setattr(advanced_plans,'get_or_build_plan',lambda *args:copy.deepcopy(plan))
    calls=[]
    def batch(pid,payload):
        calls.append(copy.deepcopy(payload));return {'id':'b'*32,'jobs':[],'request_id':payload['request_id']}
    monkeypatch.setattr(advanced_api,'generation_batch',batch)
    app=FastAPI();app.include_router(advanced_api.router)
    with TestClient(app) as client:yield store,p,evidence,timing,client,'/api/advanced/projects/'+p['id'],calls


def get_draft(context,**extra):
    store,p,evidence,timing,c,url,calls=context
    advanced.atomic(store.directory(p['id'])/'music-evidence'/(evidence['id']+'.json'),evidence)
    advanced.atomic(store.directory(p['id'])/'timing-maps'/(timing['id']+'.json'),timing)
    response=c.post(url+'/segmentation-preview',json={'expected_revision':store.load(p['id'])['revision'],'evidence_id':evidence['id'],'timing_map_id':timing['id'],'source_id':'original','patterns':['balanced'],'difficulties':['hard'],'cuts':[advanced.SR],**extra})
    assert response.status_code==200,response.text
    return response.json()


def test_new_defaults_and_preview_freeze_options_without_mutation(context):
    store,p,evidence,timing,c,url,calls=context
    assert p['profile']=='keyboard' and [v['key'] for v in p['variants']]==['balanced--hard']
    draft=get_draft(context,regions=[{'intensity':'calm'},{'intensity':'dense'}])
    assert store.load(p['id'])==p and calls==[]
    assert [r['intensity'] for r in draft['regions']]==['calm','dense']
    assert draft['options']['variants']==['balanced--hard']
    assert draft['evidence_id']==evidence['id'] and draft['timing_map_id']==timing['id']


def test_music_workflow_merges_partial_nps_range_override_into_preview_snapshot(context):
    draft=get_draft(context,settings={'nps_ranges':{'hard':{'min':7,'max':11}}})

    assert set(draft['options']['settings']['nps_ranges'])==set(advanced.PRESETS)
    assert draft['options']['settings']['nps_ranges']['hard']=={'min':7.,'max':11.}
    assert draft['options']['settings']['difficulty_rules']['hard']['rate']==9.


def test_analysis_is_async_external_and_reuses_completed_evidence(context):
    store,p,evidence,timing,c,url,calls=context
    task=c.post(url+'/music-analysis',json={'expected_revision':0,'source_id':'original'}).json()
    assert task['status']=='queued'
    job=server.jobs[task['task_id']]
    music_workflow.run_analysis(store.directory(p['id'])/'source.wav',store.root.parent/job['id'],job['options'],lambda *_:None)
    job['status']='completed'
    result=c.get(url+'/music-analysis/'+task['analysis_id']).json()
    assert result['status']=='ambiguous' and result['timing_map']['id']==timing['id'],result
    assert store.load(p['id'])==p and calls==[]
    assert c.post(url+'/music-analysis',json={'expected_revision':0,'source_id':'original'}).json()['id']==task['id']


def test_confirm_generates_frozen_plan_once_and_rejects_changed_reuse(context):
    store,p,evidence,timing,c,url,calls=context;draft=get_draft(context)
    payload={'request_id':'c'*32,'expected_revision':0,'draft_id':draft['id'],'arrangement_plan_id':draft['arrangement_plan_id']}
    first=c.post(url+'/confirm-and-generate',json=payload);assert first.status_code==200,first.text
    second=c.post(url+'/confirm-and-generate',json=payload);assert second.json()==first.json() and len(calls)==1
    assert len(first.json()['project']['segments'])==2
    assert calls[0]['evidence']['id']==evidence['id'] and calls[0]['initial_plan_id']==draft['arrangement_plan_id']
    rejected=c.post(url+'/confirm-and-generate',json={**payload,'expected_revision':2})
    assert rejected.status_code in (400,409) and len(calls)==1


def test_stale_confirmation_never_applies_boundaries_or_starts_generation(context):
    store,p,evidence,timing,c,url,calls=context;draft=get_draft(context)
    latest=store.update(p['id'],{'title':'Edited'},0)
    response=c.post(url+'/confirm-and-generate',json={'request_id':'d'*32,'expected_revision':0,'draft_id':draft['id'],'arrangement_plan_id':draft['arrangement_plan_id']})
    assert response.status_code==409 and store.load(p['id'])==latest and calls==[]


def test_evidence_paths_and_foreign_timing_are_rejected(context):
    store,p,evidence,timing,c,url,calls=context
    response=c.post(url+'/segmentation-preview',json={'expected_revision':0,'evidence_id':'../project','timing_map_id':timing['id']})
    assert response.status_code==400 and store.load(p['id'])==p


def test_initial_adoption_does_not_override_user_choice_or_later_settings(context):
    store,p,evidence,timing,c,url,calls=context;draft=get_draft(context)
    c.post(url+'/confirm-and-generate',json={'request_id':'e'*32,'expected_revision':0,'draft_id':draft['id'],'arrangement_plan_id':draft['arrangement_plan_id']})
    project=store.load(p['id']);part=project['segments'][0]
    snap={'project':{'id':p['id']},'segment':copy.deepcopy(part),'initial_plan_id':draft['arrangement_plan_id'],'arrangement_plan':{'settings':copy.deepcopy(project['settings'])},'activate_initial':False}
    row={'variant':'balanced--hard','events':[{'id':'n','start_ms':100,'end_ms':None,'lane':0}],'settings':project['settings'],'kind':'model_raw','provenance':{},'activate_initial':False}
    playable={**row,'kind':'rules','activate_initial':True}
    result={'advanced_result':[row,playable],'bounds':[part['start_sample'],part['end_sample']]}
    advanced_api.commit_generated({'_advanced':snap},result)
    chosen=store.load(p['id'])['segments'][0]['active']['balanced--hard']
    assert store.revision(p['id'],chosen)['kind']=='rules'
    advanced_api.commit_generated({'_advanced':snap},result)
    assert store.load(p['id'])['segments'][0]['active']['balanced--hard']==chosen


def test_dual_stem_plan_cannot_confirm_unprepared_audio(context):
    store,p,evidence,timing,c,url,calls=context
    advanced.atomic(store.directory(p['id'])/'music-evidence'/(evidence['id']+'.json'),evidence)
    advanced.atomic(store.directory(p['id'])/'timing-maps'/(timing['id']+'.json'),timing)
    response=c.post(url+'/segmentation-preview',json={'expected_revision':0,'evidence_id':evidence['id'],
        'timing_map_id':timing['id'],'source_id':'vocals_accompaniment'})
    assert response.status_code in (400,409) and '尚未准备' in response.text
    assert store.load(p['id'])==p and calls==[]


def test_complete_export_rejects_missing_targets_before_producing_output(context):
    store,p,evidence,timing,c,url,calls=context
    store.add_segment(p['id'],{'start_sample':0,'end_sample':p['samples']})
    response=c.post(url+'/assemblies',json={'require_complete':True})
    assert response.status_code==409 and 'balanced--hard' in response.text
    assert not list((store.directory(p['id'])/'assemblies').glob('*'))


def test_switching_to_original_does_not_freeze_legacy_dual_source_mode(context):
    store,p,evidence,timing,c,url,calls=context
    store.update(p['id'],{'settings':{**p['settings'],'source_mode':'vocals_accompaniment'}})
    advanced.atomic(store.directory(p['id'])/'music-evidence'/(evidence['id']+'.json'),evidence)
    advanced.atomic(store.directory(p['id'])/'timing-maps'/(timing['id']+'.json'),timing)
    response=c.post(url+'/segmentation-preview',json={'expected_revision':store.load(p['id'])['revision'],
        'evidence_id':evidence['id'],'timing_map_id':timing['id'],'source_id':'original'})
    assert response.status_code==200,response.text
    assert response.json()['options']['settings']['source_mode']=='mix'
    advanced.validate_settings(response.json()['options']['settings'])



def test_preview_is_a_new_analysis_with_current_bucket_version_and_per_region_tempo(context):
    store,p,evidence,timing,c,url,calls=context
    sr=advanced.SR
    beats=[int(sr*(.1+.2*i)) for i in range(10)]
    beat_timing=music_timing.timing_map({'beat_samples':beats,'downbeat_samples':[],'provenance':{'adapter':'beat_this_final1'}},evidence['source'])
    beat_evidence=music_timing.sealed({**{k:v for k,v in evidence.items() if k!='id'},'candidates':[beat_timing],'selected_timing_id':beat_timing['id']})
    advanced.atomic(store.directory(p['id'])/'music-evidence'/(beat_evidence['id']+'.json'),beat_evidence)
    advanced.atomic(store.directory(p['id'])/'timing-maps'/(beat_timing['id']+'.json'),beat_timing)
    response=c.post(url+'/segmentation-preview',json={'expected_revision':store.load(p['id'])['revision'],'evidence_id':beat_evidence['id'],'timing_map_id':beat_timing['id'],'source_id':'original','patterns':['balanced'],'difficulties':['hard'],'cuts':[sr]})
    assert response.status_code==200,response.text
    draft=response.json()
    arrangement=advanced.read(store.directory(p['id'])/'arrangement-plans'/(draft['arrangement_plan_id']+'.json'))
    policy=arrangement['section_plan']['bpm_buckets']
    assert policy['version']=='bpm-buckets-v3' and draft['bpm_bucket_version']=='bpm-buckets-v3'
    assert policy['region_assignment']=='user_region_dominant_bucket_v1'
    assert [(row['start_sample'],row['end_sample']) for row in draft['segment_tempo']]==[(row['start_sample'],row['end_sample']) for row in draft['segments']]
    assert all(row['bpm_bucket_id']==0 and not row['multi_tempo'] and row['detected_bpm']==pytest.approx(150,rel=.03) for row in draft['segment_tempo'])
