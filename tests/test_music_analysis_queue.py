"""Music preparation is a queued, frozen stage and never generates chart notes."""
import copy
import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from malody_studio import advanced, advanced_api, music_timing, music_workflow, paths, server, separation, separation_tasks


@pytest.fixture
def context(tmp_path, monkeypatch):
    sf.write(tmp_path/'input.wav', np.full((44100*4, 2), .1, np.float32), 44100, subtype='FLOAT')
    monkeypatch.setattr(advanced, 'audio_metadata', lambda _: ([], {'bpm':120, 'points':[[0,120]], 'uncertain':True}))
    store=advanced.ProjectStore(tmp_path/'outputs/advanced');project=store.create(tmp_path/'input.wav','Frozen music')
    monkeypatch.setattr(advanced_api,'store',store)
    monkeypatch.setattr(server,'ROOT',tmp_path);monkeypatch.setattr(paths,'ROOT',tmp_path)
    monkeypatch.setattr(separation_tasks,'ROOT',tmp_path)
    monkeypatch.setattr(server,'jobs',{})
    monkeypatch.setattr(server,'dispatch_next',lambda:None)
    monkeypatch.setattr(server,'ensure_engine',lambda _:pytest.fail('Analysis attempted chart inference'))
    monkeypatch.setattr(separation,'deployment',lambda _: {})
    _,clock=music_timing.source_contract(store.directory(project['id']),project)
    timing=music_timing.timing_map({'beat_samples':[],'downbeat_samples':[]},clock)
    evidence=music_timing.sealed({'schema':music_timing.SCHEMA,'source':clock,'candidates':[timing],'policy':{},'selected_timing_id':timing['id']})
    calls=[]
    def detect(directory, frozen, progress=None, frozen_policy=None):
        calls.append(copy.deepcopy(frozen));return copy.deepcopy(evidence)
    monkeypatch.setattr(music_timing,'load_or_analyze',detect)
    app=FastAPI();app.include_router(advanced_api.router)
    with TestClient(app) as client:yield store,project,client,'/api/advanced/projects/'+project['id'],calls


def complete(context, task):
    store, project, client, url, calls=context
    job=server.jobs[task['task_id']]
    result=music_workflow.run_analysis(store.directory(project['id'])/'source.wav',store.root.parent/job['id'],job['options'],lambda *_:None)
    job.update(status='completed', **{k:v for k,v in result.items() if k!='status'})
    return client.get(url+'/music-analysis/'+task['id']).json()


def test_analysis_enters_actual_queue_and_freezes_source_without_generation(context):
    store,project,client,url,calls=context
    task=client.post(url+'/music-analysis',json={'expected_revision':0,'source_id':'original'}).json()
    assert task['status']=='queued' and task['task_id'] in server.jobs and not calls
    queued=server.jobs[task['task_id']]['options']['_advanced']
    assert queued['task_type']=='music_analysis' and queued['project']['source_pcm_sha256']==project['source_pcm_sha256']
    assert client.get(url+'/tasks').json()['tasks'][0]['task_type']=='music_analysis'
    store.update(project['id'],{'title':'Changed while queued'},0)
    ready=complete(context,task)
    assert ready['status']=='ambiguous' and calls[0]['title']=='Frozen music'
    assert ready['resolved_source']=={'source_id':'original'}
    assert store.load(project['id'])['segments']==[]
    assert not list((store.directory(project['id'])/'revisions').glob('*.json'))
    again=client.post(url+'/music-analysis',json={'expected_revision':1,'source_id':'original'}).json()
    assert again['id']==task['id'] and len(server.jobs)==1 and len(calls)==1


def test_double_submit_shares_queued_analysis(context):
    store,project,client,url,calls=context
    body={'expected_revision':0,'source_id':'original'}
    first=client.post(url+'/music-analysis',json=body).json()
    second=client.post(url+'/music-analysis',json=body).json()
    assert first==second and len(server.jobs)==1 and store.load(project['id'])==project


def test_auto_stems_finish_before_ready_and_frozen_generation_input(context,monkeypatch):
    store,project,client,url,calls=context
    seen=[]
    def prepare(source,directory,options,progress):
        seen.append(copy.deepcopy(options['_advanced']['settings']))
        stem='b'*64;folder=store.directory(project['id'])/'stems'/stem;folder.mkdir(parents=True)
        rows=[]
        for role in ('vocals','accompaniment'):
            data=np.full((project['samples'],2),.05,np.float32);path=folder/(role+'.wav');sf.write(path,data,44100,subtype='FLOAT')
            rows.append({'role':role,'source_id':stem+':'+role,'file':path.name,'pcm_sha':separation.pcm_hash(data),'file_sha256':separation.file_hash(path)})
        advanced.atomic(folder/'manifest.json',{'id':stem,'recipe_hash':stem,'adapter_version':separation.VERSION,
            'source_pcm_sha':project['source_pcm_sha256'],'frame_count':project['samples'],'origin_sample':0,'sample_rate':44100,
            'stems':rows,'settings':separation.validated_settings()})
        return {'stem_set_id':stem}
    monkeypatch.setattr(separation_tasks,'run',prepare)
    task=client.post(url+'/music-analysis',json={'expected_revision':0,'source_id':'vocals_accompaniment'}).json()
    assert not seen and task['source']['auto_prepare_stems']
    ready=complete(context,task)
    assert seen and calls and ready['resolved_source']=={'source_id':'vocals_accompaniment','stem_set_id':'b'*64}
    assert store.load(project['id'])==project
    response=client.post(url+'/segmentation-preview',json={'expected_revision':0,'evidence_id':ready['evidence_id'],
        'timing_map_id':ready['timing_map_id'],**ready['resolved_source']})
    assert response.status_code==200,response.text
    draft=response.json()
    assert draft['options']['stem_set_id']=='b'*64 and not draft['options'].get('auto_prepare_stems')


def test_source_preparation_failure_has_no_chart_or_project_side_effect(context,monkeypatch):
    store,project,client,url,calls=context
    def fail(*_):raise RuntimeError('Injected source preparation failure')
    monkeypatch.setattr(separation_tasks,'run',fail)
    task=client.post(url+'/music-analysis',json={'expected_revision':0,'source_id':'vocals_accompaniment'}).json()
    with pytest.raises(RuntimeError,match='preparation'):complete(context,task)
    server.jobs[task['task_id']].update(status='failed',error='Injected source preparation failure')
    result=client.get(url+'/music-analysis/'+task['id']).json()
    assert result['status']=='failed' and not calls and store.load(project['id'])==project
    retry=client.post(url+'/music-analysis',json={'expected_revision':0,'source_id':'vocals_accompaniment'}).json()
    assert retry['id']!=task['id'] and server.jobs[task['task_id']]['status']=='failed'


def test_full_queue_leaves_no_untracked_analysis_or_project_change(context):
    store,project,client,url,calls=context
    server.jobs.update({str(i):{'status':'queued'} for i in range(100)})
    result=client.post(url+'/music-analysis',json={'expected_revision':0,'source_id':'original'})
    assert result.status_code==429 and store.load(project['id'])==project
    assert not list((store.directory(project['id'])/'music-analysis').glob('*.json'))
