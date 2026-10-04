"""Regression checks for rain-song import contracts, without a model or live service."""
import importlib
import io
import json
import sys
import zipfile
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from malody_studio import advanced, paths
from malody_studio.charts import Note, serialize


@pytest.fixture
def imported_project(tmp_path, monkeypatch):
    # API import restores saved review state. Keep that work inside this fixture.
    for name in ('outputs', 'cache', 'uploads', 'logs', 'web'):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(paths, 'ROOT', tmp_path)
    monkeypatch.setattr(advanced, 'ROOT', tmp_path)
    api = importlib.import_module('malody_studio.advanced_api')
    monkeypatch.setattr(api, 'ROOT', tmp_path)
    monkeypatch.setattr(advanced, 'audio_metadata', lambda _: (
        [], {'bpm': 143.555, 'beat_times': [0.1, 0.5], 'uncertain': False}))
    store = advanced.ProjectStore(tmp_path / 'projects')
    monkeypatch.setattr(api, 'store', store)
    source = tmp_path / 'input.wav'
    sf.write(source, np.full((advanced.SR, 2), .1, np.float32), advanced.SR, subtype='FLOAT')
    project = store.create(source, '私は雨', 'Rain test')
    return api, store, project, source


def chart(bpm, version='4K Expert / V32'):
    return serialize([Note(500, 0)], '私は雨', 'Rain test', version, bpm)


def test_imported_chart_updates_default_bpm_and_keeps_reference_unconfirmed(imported_project):
    api, store, project, _ = imported_project
    result = api.import_charts(project, [chart(144)])
    assert result['tempo']['points'] == [[0., 144.]]
    assert result['tempo']['bpm'] == result['tempo']['points'][0][1]
    assert result['tempo']['reference_source'] == 'imported_chart'
    assert result['tempo']['uncertain'] is True
    assert result['tempo']['reference_conflict'] is False
    revision = store.revision(result['id'], result['segments'][0]['active']['balanced--expert'])
    assert revision['events'][0]['start_ms'] == pytest.approx(500, abs=.01)


def test_conflicting_imports_do_not_select_an_arbitrary_default_bpm(imported_project):
    api, _, project, _ = imported_project
    result = api.import_charts(project, [chart(144), chart(180, '4K Master / V32')])
    assert result['tempo']['reference_conflict'] is True
    assert result['tempo']['bpm'] == 143.555
    assert 'points' not in result['tempo']


@pytest.mark.parametrize('reference,options,identity', [
    ({'type': 'youtube', 'video_id': 'UKZt1vq8bKI'}, {}, 'youtube'),
    ({}, {'source': 'https://www.youtube.com/watch?v=UKZt1vq8bKI'}, 'youtube'),
    ({'type': 'upload', 'name': 'audio.wav'}, {'artwork_video_id': 'UKZt1vq8bKI'}, 'unknown'),
    ({}, {'artwork_video_id': 'UKZt1vq8bKI'}, 'unknown'),
    ({'type': 'youtube', 'video_id': 'UKZt1vq8bKI'},
     {'source': 'https://www.youtube.com/watch?v=AAAAAAAAAAA'}, 'unknown'),
])
def test_job_import_inherits_music_identity_without_claiming_direct_original(
        imported_project, monkeypatch, reference, options, identity):
    api, store, _, source = imported_project
    job_id = 'a' * 32
    directory = api.ROOT / 'outputs' / job_id / '0'
    directory.mkdir(parents=True)
    (directory / 'audio.ogg').write_bytes(source.read_bytes())
    job = {'id': job_id, 'status': 'completed', 'title': '私は雨', 'artist': 'Rain test',
           'source_ref': reference, 'options': options}
    # Do not import the queue module: its startup recovery rewrites running jobs.
    monkeypatch.setitem(sys.modules, 'malody_studio.server', SimpleNamespace(
        get_job=lambda _: job, _charts_from_disk=lambda _: {'expert': chart(144)}))
    result = api.from_source({'type': 'job', 'id': job_id})
    provenance = result['source_provenance']
    assert provenance['kind'] == 'imported_job_audio'
    assert provenance['source_identity'] == identity
    assert provenance['direct_original'] is False
    assert provenance['source_job_id'] == job_id
    assert provenance['bytes'] == source.stat().st_size
    if identity == 'youtube':
        assert provenance['url'] == 'https://www.youtube.com/watch?v=UKZt1vq8bKI'
    else:
        assert 'url' not in provenance and 'video_id' not in provenance
    assert store.load(result['id'])['source_provenance'] == provenance


def audio_bytes(seconds, level, container='WAV'):
    stream=io.BytesIO()
    sf.write(stream, np.full((round(advanced.SR*seconds),2),level,np.float32),
             advanced.SR, format=container, subtype='VORBIS' if container=='OGG' else 'FLOAT')
    return stream.getvalue()


def bundle_bytes(files, charts):
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as archive:
        for name,content in files:archive.writestr(name,content)
        for name,document in charts:archive.writestr(name,json.dumps(document,ensure_ascii=False))
    return stream.getvalue()


def test_mcz_uses_chart_relative_bgm_instead_of_first_keysound(imported_project):
    api, store, _, _ = imported_project
    document=serialize([Note(1500,0)],'私は雨','Rain test','4K Easy',144)
    document['note'][-1]['sound']='song.wav'
    selected=audio_bytes(2,.3)
    payload=bundle_bytes([('0/click.wav',audio_bytes(1,.05)),('0/song.wav',selected)],
                         [('0/easy.mc',document)])
    path=api.ROOT/'with-keysound.mcz';path.write_bytes(payload)
    result=api.uploaded_project(path,'','')
    assert result['duration']==2
    assert (store.directory(result['id'])/'source-original.wav').read_bytes()==selected
    segment=result['segments'][0]
    revision=store.revision(result['id'],segment['active']['balanced--easy'])
    assert len(revision['events'])==1
    assert revision['events'][0]['start_ms']==pytest.approx(1500,abs=.01)


@pytest.mark.parametrize('second_chart_path,reference',[
    ('0/hard.mc','audio.ogg'),('1/hard.mc','../0/audio.ogg'),
])
def test_mcz_shared_legacy_bgm_allows_multiple_difficulties_and_keeps_offsets(
        imported_project,second_chart_path,reference):
    api, store, _, _ = imported_project
    easy=chart(144,'4K Easy');hard=chart(144,'4K Hard')
    easy['note'][-1]['offset']=-37
    hard['note'][-1].update(sound=reference,offset=23)
    payload=bundle_bytes([('0/click.wav',audio_bytes(.25,.05)),
                          ('0/audio.ogg',audio_bytes(1,.2,'OGG'))],
                         [('0/easy.mc',easy),(second_chart_path,hard)])
    path=api.ROOT/'legacy.mcz';path.write_bytes(payload)
    result=api.uploaded_project(path,'','')
    assert result['duration']==pytest.approx(1,abs=.01)
    segment=result['segments'][0]
    assert len(segment['active'])==2
    for key,expected in [('easy',537),('hard',477)]:
        revision=store.revision(result['id'],segment['active']['balanced--'+key])
        assert revision['events'][0]['start_ms']==pytest.approx(expected,abs=.01)


@pytest.mark.parametrize('reference,message',[
    ('missing.wav','不存在'),('/song.wav','绝对路径'),
    ('C:\\song.wav','绝对路径'),('\\\\server\\song.wav','绝对路径'),
    ('../../song.wav','越过包内目录'),('', '路径无效'),
])
def test_mcz_bad_bgm_references_reject_without_falling_back_to_keysound(
        imported_project,reference,message):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    api, store, _, source = imported_project
    before=store.list();original=source.read_bytes()
    document=chart(144);document['note'][-1]['sound']=reference
    payload=bundle_bytes([('0/click.wav',audio_bytes(1,.05)),
                          ('0/song.wav',audio_bytes(1,.2))],[('0/chart.mc',document)])
    app=FastAPI();app.include_router(api.router)
    with TestClient(app) as client:
        response=client.post('/api/advanced/projects/upload',
                             files={'file':('invalid.mcz',payload,'application/zip')})
    assert response.status_code==400,response.text
    assert message in response.json()['detail']
    assert store.list()==before and source.read_bytes()==original
    assert not list((api.ROOT/'cache').glob('advanced-import-*'))


@pytest.mark.parametrize('case,message',[
    ('different_music','不同背景音乐'),('duplicate_object','不唯一'),
    ('multiple_bgm','一份背景音乐'),('no_bgm','一份背景音乐'),
    ('unsafe_member','越过包内目录'),
])
def test_mcz_rejects_ambiguous_music_identity_before_creating_project(
        imported_project,case,message):
    api,store,_,_=imported_project
    easy=chart(144,'4K Easy');easy['note'][-1]['sound']='song.wav'
    files=[('0/song.wav',audio_bytes(1,.2))];documents=[('0/easy.mc',easy)]
    if case=='different_music':
        hard=chart(144,'4K Hard');hard['note'][-1]['sound']='other.wav'
        documents.append(('0/hard.mc',hard));files.append(('0/other.wav',audio_bytes(1,.3)))
    elif case=='duplicate_object':files.append(('0/./song.wav',audio_bytes(1,.3)))
    elif case=='multiple_bgm':easy['note'].append({'type':1,'sound':'click.wav','beat':[0,0,1],'offset':0})
    elif case=='no_bgm':easy['note'].pop()
    else:files.append(('0/../../outside.wav',audio_bytes(1,.3)))
    before=store.list();path=api.ROOT/'ambiguous.mcz';path.write_bytes(bundle_bytes(files,documents))
    with pytest.raises(ValueError,match=message):api.uploaded_project(path,'','')
    assert store.list()==before
    assert not list((api.ROOT/'cache').glob('advanced-import-*'))
