import copy
import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from malody_studio import advanced, advanced_api


@pytest.fixture
def context(tmp_path, monkeypatch):
    source = tmp_path/'song.wav'
    sf.write(source, np.zeros((advanced.SR*4, 2), np.float32), advanced.SR, subtype='FLOAT')
    monkeypatch.setattr(advanced, 'audio_metadata', lambda _:([], {'bpm':120, 'beat_times':[], 'uncertain':True}))
    store = advanced.ProjectStore(tmp_path/'projects')
    p = store.create(source, 'Undo test')
    monkeypatch.setattr(advanced_api, 'store', store)
    app = FastAPI(); app.include_router(advanced_api.router)
    with TestClient(app) as client:
        yield store, p, client, '/api/advanced/projects/'+p['id']


def add(client, url, start=0, end=3):
    result = client.post(url+'/segments', json={'start_sample':round(start*advanced.SR), 'end_sample':round(end*advanced.SR)})
    assert result.status_code == 200
    return result.json()


def revision(store, p, s):
    return store.add_revision(p['id'], s['id'], 'balanced--easy',
                             [{'id':'n', 'start_ms':500, 'end_ms':None, 'lane':0}],
                             p['settings'], 'model_raw')


def test_delete_undo_restores_selected_revision_and_same_segment_id(context):
    store, p, client, url = context
    p = add(client, url); s=p['segments'][0]; r=revision(store,p,s)
    before=store.load(p['id'])['segments']
    assert client.delete(url+'/segments/'+s['id']).status_code==200
    restored=client.post(url+'/undo').json()
    assert restored['segments']==before
    assert store.revision(p['id'],r['id'])['events'][0]['start_ms']==500


def test_split_undo_restores_parent_and_keeps_child_files(context):
    store, p, client, url = context
    p=add(client,url);s=p['segments'][0];revision(store,p,s)
    before=store.load(p['id'])['segments']
    split=client.post(url+'/segments/'+s['id']+'/split',json={'cuts':[advanced.SR]}).json()
    children=[v['id'] for part in split['segments'] for rows in part['versions'].values() for v in rows]
    restored=client.post(url+'/undo',json={'expected_revision':split['revision']}).json()
    assert restored['segments']==before
    assert all(store.revision(p['id'],rid)['id']==rid for rid in children)


def test_new_generation_blocks_old_undo_without_losing_new_version(context):
    store, p, client, url=context
    p=add(client,url);s=p['segments'][0];r=revision(store,p,s)
    assert not client.get(url+'/edit-history').json()['can_undo']
    failed=client.post(url+'/undo')
    assert failed.status_code==409
    assert store.load(p['id'])['segments'][0]['active']['balanced--easy']==r['id']


def test_invalid_edit_and_no_op_do_not_add_history(context):
    store,p,client,url=context
    p=add(client,url);s=p['segments'][0]
    count=client.get(url+'/edit-history').json()['count']
    assert client.patch(url+'/segments/'+s['id'],json={'end_sample':1}).status_code==400
    assert client.patch(url+'/segments/'+s['id'],json={'overrides':{}}).status_code==200
    assert client.get(url+'/edit-history').json()['count']==count


def test_undo_preserves_project_settings_tempo_and_checks_stale_revision(context):
    store,p,client,url=context
    p=add(client,url);s=p['segments'][0]
    edited=client.patch(url+'/segments/'+s['id'],json={'end_sample':2*advanced.SR}).json()
    newer=store.update(p['id'],{'tempo':{'points':[[0,150]]},'patterns':['speed'],'difficulties':['hard']})
    before=copy.deepcopy(newer)
    assert client.post(url+'/undo',json={'expected_revision':edited['revision']}).status_code==409
    restored=client.post(url+'/undo',json={'expected_revision':newer['revision']}).json()
    assert restored['tempo']==before['tempo'] and restored['variants']==before['variants']
    assert restored['segments'][0]['end_sample']==3*advanced.SR


def test_client_cannot_supply_forged_undo_snapshot(context):
    _,_,client,url=context
    add(client,url)
    assert client.post(url+'/undo',json={'segments':[]}).status_code==400
    assert len(client.get(url).json()['segments'])==1
