import json
import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from malody_studio import advanced, advanced_api, workflow_api, server


@pytest.fixture
def project(tmp_path, monkeypatch):
    path = tmp_path/'input.wav'
    sf.write(path,np.zeros((44100*2,2),dtype=np.float32),44100,subtype='FLOAT')
    monkeypatch.setattr(advanced,'audio_metadata',lambda _:([],{'bpm':120,'beat_times':[],'uncertain':True}))
    store = advanced.ProjectStore(tmp_path/'projects')
    p = store.create(path,'Workflow Test','')
    monkeypatch.setattr(advanced_api,'store',store)
    return store,p


def test_source_roles_resolve_only_current_project_and_assembly(project,tmp_path):
    store,p = project;root=store.directory(p['id'])
    other = tmp_path/'outside.wav';other.write_bytes(b'private')
    folder=root/'stems'/'test';folder.mkdir(parents=True)
    manifest=folder/'manifest.json'
    advanced.atomic(manifest,{'stems':[{'source_id':'vocals:test','path':str(other)}]})
    with pytest.raises(ValueError,match='项目目录'):workflow_api.source_path(p['id'],'vocals:test')
    with pytest.raises(ValueError):workflow_api.source_path(p['id'],'assembly:../../outside')
    with pytest.raises(ValueError):workflow_api.source_path(p['id'],'fusion:logical-chart-id')
    inside=folder/'vocals.wav';inside.write_bytes(b'local')
    advanced.atomic(manifest,{'stems':[{'source_id':'vocals:test','path':str(inside)}]})
    assert workflow_api.source_path(p['id'],'vocals:test')==inside.resolve()
    assert workflow_api.source_path(p['id'])==root/'source.wav'


def test_spectrum_http_queues_real_audio_and_rejects_invalid_ranges(project):
    store,p = project;app=FastAPI();app.include_router(advanced_api.router)
    prefix='/api/advanced/projects/'+p['id']
    with TestClient(app) as client:
        result=client.post(prefix+'/analysis',json={'source_id':'original','start_ms':0,'end_ms':1500})
        assert result.status_code==200
        aid=result.json()['analysis_id']
        assert client.get(prefix+'/analysis/'+aid).status_code==200
        assert client.get(prefix+'/analysis/'+aid+'/tiles/0/0').status_code in (200,202)
        assert client.get(prefix+'/analysis/'+aid+'/tiles/0/999').status_code==400
        assert client.post(prefix+'/analysis',json={'start_ms':1000,'end_ms':900}).status_code==400
        assert client.post(prefix+'/analysis',json={'source_id':'unknown'}).status_code==400
        audio=client.get(prefix+'/sources/original/audio',headers={'Range':'bytes=0-127'})
        assert audio.status_code==206 and len(audio.content)==128


def test_new_dynamic_default_and_legacy_snapshot_keep_distinct_policies():
    assert advanced.validate_settings(advanced.defaults())['dynamic_enabled'] is True
    legacy=advanced.defaults();legacy.pop('dynamic_enabled')
    assert advanced.validate_settings(legacy)['dynamic_enabled'] is False
    with pytest.raises(ValueError):advanced.validate_settings({'dynamic_enabled':'false'})


def test_rhythm_api_uses_shared_cache_for_analysis_and_overrides(project,monkeypatch):
    from malody_studio import advanced_plans
    store,p=project;calls=[]
    def cached(current_store,current_project,settings):
        calls.append((current_store,current_project,settings))
        return {'id':'cache-result','marker':settings['difficulty_rules']['medium']['rate']}
    monkeypatch.setattr(advanced_plans,'get_or_build_plan',cached)
    app=FastAPI();app.include_router(advanced_api.router)
    with TestClient(app) as client:
        url='/api/advanced/projects/'+p['id']+'/section-plans'
        first=client.post(url,json={})
        second=client.post(url,json={'settings':{'nps_ranges':{'medium':{'min':6,'max':8}}}})
    assert first.json()=={'id':'cache-result','marker':5.}
    assert second.json()=={'id':'cache-result','marker':7}
    assert len(calls)==2 and all(call[0] is store and call[1]['id']==p['id'] for call in calls)
    assert store.load(p['id'])==p


def test_game_clip_uses_selected_stem_without_changing_sample_origin(project):
    import io
    store,p=project
    p=store.add_segment(p['id'],{'start_sample':22050,'end_sample':66150})
    s=p['segments'][0]
    revision=store.add_revision(p['id'],s['id'],'speed--medium',
        [{'id':'note','start_ms':700,'end_ms':None,'lane':0}],advanced.defaults(),'model_raw')
    folder=store.directory(p['id'])/'stems'/'test';folder.mkdir(parents=True)
    source=folder/'vocals.wav'
    wave=np.zeros((88200,2),np.float32);wave[44100]=1.32
    sf.write(source,wave,44100,subtype='FLOAT')
    advanced.atomic(folder/'manifest.json',{'stems':[{'source_id':'vocals:test','path':str(source)}]})
    app=FastAPI();app.include_router(advanced_api.router)
    url='/api/advanced/projects/'+p['id']+'/revisions/'+revision['id']+'/audio'
    with TestClient(app) as client:
        result=client.get(url,params={'source_id':'vocals:test'})
        assert result.status_code==200
        data,rate=sf.read(io.BytesIO(result.content),dtype='float32',always_2d=True)
        assert rate==44100 and len(data)==44100
        # Browser playback uses uniform peak-safe attenuation; model PCM stays raw.
        from malody_studio.playback_audio import gain_for_peak
        gain=gain_for_peak(float(np.abs(wave).max()))
        np.testing.assert_allclose(data,wave[22050:66150]*gain,atol=1e-7)
        assert np.max(np.abs(data))<=.980001
        assert np.array_equal(sf.read(source,dtype='float32',always_2d=True)[0],wave)
        assert client.get(url,params={'source_id':'missing'}).status_code==400
        assert client.get(url,params={'source_id':'assembly:'+'0'*32}).status_code==400


def test_advanced_parent_exclusive_even_when_regular_parallel_is_enabled(tmp_path,monkeypatch):
    class Pool:
        def __init__(self):self.calls=[]
        def submit(self,*args):self.calls.append(args)
    pool=Pool();monkeypatch.setattr(server,'pool',pool)
    monkeypatch.setattr(server,'configured_concurrency',lambda:2)
    monkeypatch.setattr(server,'ROOT',tmp_path)
    ids=[f'{i:032x}' for i in range(1,4)]
    jobs={jid:{'id':jid,'status':'queued','queue_order':i,'options':({'_advanced':{'settings':{}}} if i==1 else {})} for i,jid in enumerate(ids,1)}
    for jid in ids:(tmp_path/'outputs'/jid).mkdir(parents=True)
    monkeypatch.setattr(server,'jobs',jobs);monkeypatch.setattr(server,'scheduler_active',set())
    server.dispatch_next()
    assert len(pool.calls)==1 and server.scheduler_active=={ids[0]}
    server.dispatch_next();assert len(pool.calls)==1
    jobs[ids[0]]['status']='completed';server.scheduler_active.clear()
    server.dispatch_next();assert len(pool.calls)==3


def test_imported_tempo_reference_does_not_claim_acoustic_measurement(project):
    store,p = project
    from malody_studio.charts import Note,serialize
    chart=serialize([Note(500,0,None)],'Workflow Test','','Speed Easy',120)
    chart['time']=[{'beat':[0,0,1],'bpm':120},{'beat':[2,0,1],'bpm':240}]
    result=advanced_api.import_charts(p,[chart])
    assert result['tempo']['uncertain'] is True
    assert result['tempo']['reference_source']=='imported_chart'
    assert result['tempo']['points']==[[0.,120],[1000.,240]]
    assert not result['tempo']['reference_conflict']
