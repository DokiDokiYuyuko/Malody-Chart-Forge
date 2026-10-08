import json
import pytest
from fastapi.testclient import TestClient
from malody_studio import music, server

def test_search_filters_live_long_and_invalid_results(monkeypatch):
    entries = [{'id':'UKZt1vq8bKI','title':'曲名','duration':321,'channel':'作者'},
               {'id':'AcOhHbutU5o','duration':700}, {'id':'EO2hFz9bMQQ','is_live':True,'live_status':'is_live'},
               {'id':'../../bad','duration':100}]
    monkeypatch.setattr(music, 'command', lambda *args, **kw: json.dumps({'entries':entries}))
    monkeypatch.setattr(music, 'candidates', {})
    results = music.search('曲名')
    assert [x['id'] for x in results] == ['UKZt1vq8bKI']
    assert results[0]['url'] == 'https://www.youtube.com/watch?v=UKZt1vq8bKI'

@pytest.mark.parametrize('query', ['https://127.0.0.1/private','https://youtube.com.evil.test/watch?v=UKZt1vq8bKI','https://youtu.be/../../bad','', 'a'*201])
def test_search_rejects_untrusted_targets(query):
    with pytest.raises(ValueError): music.search(query)

def test_direct_link_is_canonicalized(monkeypatch):
    calls=[]
    def fake(*args, **kw):
        calls.append(args)
        return json.dumps({'id':'UKZt1vq8bKI','title':'Music','duration':321})
    monkeypatch.setattr(music,'command',fake)
    monkeypatch.setattr(music,'candidates',{})
    assert music.search('https://youtu.be/UKZt1vq8bKI?list=ignored')[0]['id'] == 'UKZt1vq8bKI'
    assert calls[0][-1] == 'https://www.youtube.com/watch?v=UKZt1vq8bKI'

def test_import_is_idempotent_and_requires_search(monkeypatch, tmp_path):
    class Queue:
        calls=[]
        def submit(self,*args): self.calls.append(args)
    queue=Queue()
    monkeypatch.setattr(music,'LIBRARY',tmp_path)
    monkeypatch.setattr(music,'tracks',{})
    monkeypatch.setattr(music,'candidates',{'UKZt1vq8bKI':{'id':'UKZt1vq8bKI','title':'Music'}})
    monkeypatch.setattr(music,'pool',queue)
    music.begin('UKZt1vq8bKI'); music.begin('UKZt1vq8bKI')
    assert len(queue.calls)==1
    assert json.loads((tmp_path/'UKZt1vq8bKI/track.json').read_text(encoding='utf-8'))['status']=='downloading'
    with pytest.raises(ValueError): music.begin('AcOhHbutU5o')

def test_download_failure_is_reported(monkeypatch,tmp_path):
    monkeypatch.setattr(music,'ROOT',tmp_path)
    (tmp_path/'logs').mkdir()
    monkeypatch.setattr(music,'LIBRARY',tmp_path)
    monkeypatch.setattr(music,'tracks',{'UKZt1vq8bKI':{'id':'UKZt1vq8bKI','status':'downloading'}})
    (tmp_path/'UKZt1vq8bKI').mkdir()
    monkeypatch.setattr(music,'command',lambda *a,**kw: json.dumps({'is_live': True, 'duration':30}))
    music.download('UKZt1vq8bKI')
    assert music.get('UKZt1vq8bKI')['status']=='failed'

def test_download_uses_best_available_audio_only_format(monkeypatch,tmp_path):
    from malody_studio import audio
    video_id='UKZt1vq8bKI';library=tmp_path/'library';directory=library/video_id;directory.mkdir(parents=True)
    source=directory/'source.webm';source.write_bytes(b'best available audio')
    monkeypatch.setattr(music,'LIBRARY',library)
    monkeypatch.setattr(music,'tracks',{video_id:{'id':video_id,'status':'downloading'}})
    calls=[]
    def fake_command(*args,**kwargs):
        calls.append(args)
        if '--dump-single-json' in args:
            return json.dumps({'id':video_id,'title':'Song','duration':30})
        return str(source)
    monkeypatch.setattr(music,'command',fake_command)
    monkeypatch.setattr(audio,'convert',lambda *args:(None,None,30,None))
    music.download(video_id)
    download_call=next(args for args in calls if '--dump-single-json' not in args)
    assert download_call[download_call.index('-f')+1]=='bestaudio'
    assert music.get(video_id)['status']=='ready'
    record=music.get(video_id)['source_original']
    assert record['file']=='source.webm' and record['sha256']==music._file_hash(source)

def test_generate_uses_downloaded_source_and_provenance(monkeypatch,tmp_path):
    monkeypatch.setattr(server,'ensure_engine',lambda *_:None)
    track={'url':'https://www.youtube.com/watch?v=UKZt1vq8bKI'}
    monkeypatch.setattr(music,'ready_audio',lambda video_id:(tmp_path/'audio.ogg',track))
    captured=[]
    monkeypatch.setattr(server,'new_job',lambda source,opts:captured.append((source,opts)) or {'id':'test'})
    with TestClient(server.app) as client:
        result=client.post('/api/music/UKZt1vq8bKI/generate',data={'title':'曲名','difficulties':'["easy"]'})
    assert result.status_code==200
    assert captured[0][0]==tmp_path/'audio.ogg'
    assert captured[0][1]['source']==track['url']


def test_search_page_scans_twenty_dedupes_and_exposes_cursor(monkeypatch):
    entries=[{'id':f'{i:011d}','title':f'Song {i}','duration':120,'channel':'Artist'} for i in range(20)]
    entries[1]['id']=entries[0]['id']
    calls=[]
    def fake(*args,**kwargs):
        calls.append(args)
        return json.dumps({'entries':entries})
    monkeypatch.setattr(music,'command',fake)
    monkeypatch.setattr(music,'candidates',{})
    result=music.search_page('song',cursor=20,limit=20,min_duration=5,max_duration=600)
    assert len(result['results'])==19 and result['filtered']['duplicate']==1
    assert result['next_cursor']==40 and result['has_more'] is True
    assert '--playlist-start' in calls[0] and calls[0][-1]=='ytsearch40:song'


def test_search_page_reports_duration_live_and_invalid_filters(monkeypatch):
    entries=[{'id':'aaaaaaaaaaa','title':'ok','duration':30},
             {'id':'bbbbbbbbbbb','title':'short','duration':4},
             {'id':'ccccccccccc','title':'live','duration':60,'is_live':True},
             {'id':'../invalid','title':'bad','duration':60}]
    monkeypatch.setattr(music,'command',lambda *a,**kw:json.dumps({'entries':entries}))
    monkeypatch.setattr(music,'candidates',{})
    result=music.search_page('song',min_duration=5,max_duration=60)
    assert [item['id'] for item in result['results']]==['aaaaaaaaaaa']
    assert result['filtered']=={'duration':1,'live':1,'invalid':1,'duplicate':0}


def test_batch_api_uses_one_shared_settings_snapshot(monkeypatch):
    id1,id2='aaaaaaaaaaa','bbbbbbbbbbb'
    monkeypatch.setattr(music,'candidates',{
        id1:{'id':id1,'title':'Track A','artist':'Artist A'},
        id2:{'id':id2,'title':'Track B','artist':'Artist B'}})
    monkeypatch.setattr(server,'ensure_engine',lambda options:None)
    captured=[]
    monkeypatch.setattr(server,'enqueue_job',lambda options,ref:captured.append((options,ref)) or {'id':str(len(captured))})
    settings={'engine':'mug','difficulties':['easy','hard'],'seed':'42','ln_ratio':'0.2',
              'steps':'20','difficulty_rules':{'hard':{'rate':9.5}},'pattern':'stream'}
    with TestClient(server.app) as client:
        response=client.post('/api/batches',json={'video_ids':[id1,id2,id1],'settings':settings})
    assert response.status_code==200 and response.json()['count']==2
    assert [item[0]['title'] for item in captured]==['Track A','Track B']
    assert all(item[0]['difficulties']==['easy','hard'] and item[0]['seed']==42 for item in captured)
    assert captured[0][1]=={'type':'youtube','video_id':id1}


def test_search_api_returns_paginated_contract(monkeypatch):
    monkeypatch.setattr(music,'search_page',lambda *args,**kw:{'results':[{'id':'aaaaaaaaaaa'}],
        'source':'YouTube','next_cursor':20,'has_more':True,'cursor':0,'page_size':20,
        'scanned':20,'filtered':{'duration':0}})
    with TestClient(server.app) as client:
        response=client.get('/api/music/search?q=song&cursor=0&limit=20')
    assert response.status_code==200
    assert response.json()['results'][0]['id']=='aaaaaaaaaaa'
    assert response.json()['has_more'] and response.json()['next_cursor']==20


def test_advanced_original_audio_prefers_recorded_download_and_keeps_ordinary_api(monkeypatch,tmp_path):
    video_id='UKZt1vq8bKI';directory=tmp_path/video_id;directory.mkdir()
    webm=directory/'source.webm';webm.write_bytes(b'first original stream')
    m4a=directory/'source.m4a';m4a.write_bytes(b'chosen original stream')
    converted=directory/'audio.ogg';converted.write_bytes(b'converted ordinary preview')
    record={'file':m4a.name,'bytes':m4a.stat().st_size,'sha256':music._file_hash(m4a),'format_selector':'bestaudio'}
    track={'id':video_id,'status':'ready','source_original':record,'url':music.url_for(video_id)}
    monkeypatch.setattr(music,'LIBRARY',tmp_path);monkeypatch.setattr(music,'tracks',{video_id:track})
    original,selected=music.ready_original_audio(video_id)
    assert original==m4a and selected['source_provenance']['kind']=='downloaded_original'
    assert selected['source_provenance']['filename']==m4a.name and selected['source_provenance']['sha256']==record['sha256']
    assert music.ready_audio(video_id)==(converted,track) and 'source_provenance' not in music.get(video_id)
    m4a.write_bytes(b'changed original stream')
    with pytest.raises(ValueError,match='完整性'):music.ready_original_audio(video_id)


def test_old_download_selects_direct_source_deterministically_and_reports_fallback(monkeypatch,tmp_path):
    video_id='UKZt1vq8bKI';directory=tmp_path/video_id;directory.mkdir()
    converted=directory/'audio.ogg';converted.write_bytes(b'legacy converted preview')
    monkeypatch.setattr(music,'LIBRARY',tmp_path);monkeypatch.setattr(music,'tracks',{video_id:{'id':video_id,'status':'ready'}})
    source,track=music.ready_original_audio(video_id)
    assert source==converted and track['source_provenance']['kind']=='converted_fallback'
    assert track['source_provenance']['direct_original'] is False
    (directory/'source.mp3.part').write_bytes(b'incomplete download')
    assert music.ready_original_audio(video_id)[0]==converted
    mp3=directory/'source.mp3';mp3.write_bytes(b'original mp3')
    webm=directory/'source.webm';webm.write_bytes(b'original webm')
    assert music.ready_original_audio(video_id)[0]==webm
    assert not (directory/'track.json').exists()


def test_original_audio_rejects_untrusted_metadata_path(monkeypatch,tmp_path):
    video_id='UKZt1vq8bKI';directory=tmp_path/video_id;directory.mkdir()
    (directory/'audio.ogg').write_bytes(b'preview')
    monkeypatch.setattr(music,'LIBRARY',tmp_path)
    monkeypatch.setattr(music,'tracks',{video_id:{'id':video_id,'status':'ready','source_original':{'file':'../source.webm'}}})
    with pytest.raises(ValueError,match='路径'):music.ready_original_audio(video_id)


def test_advanced_youtube_import_decodes_original_once_and_records_provenance(monkeypatch,tmp_path):
    import numpy as np
    import soundfile as sf
    from fastapi import FastAPI
    from malody_studio import advanced,advanced_api,artwork
    video_id='UKZt1vq8bKI';source=tmp_path/'source.wav';data=np.full((44100,2),.237,np.float32)
    sf.write(source,data,44100,subtype='FLOAT')
    provenance={'kind':'downloaded_original','direct_original':True,'filename':source.name,'sha256':music._file_hash(source),'bytes':source.stat().st_size,'url':music.url_for(video_id)}
    monkeypatch.setattr(advanced,'audio_metadata',lambda _:([],{'bpm':120,'beat_times':[],'uncertain':True}))
    store=advanced.ProjectStore(tmp_path/'projects');monkeypatch.setattr(advanced_api,'store',store)
    monkeypatch.setattr(music,'ready_original_audio',lambda _:(source,{'title':'Original','artist':'Artist','source_provenance':provenance}))
    monkeypatch.setattr(music,'ready_audio',lambda _:pytest.fail('advanced import used converted preview'))
    monkeypatch.setattr(artwork,'prepare_artwork',lambda _:(None,None))
    app=FastAPI();app.include_router(advanced_api.router)
    with TestClient(app) as client:response=client.post('/api/advanced/projects/from-source',json={'type':'youtube','id':video_id})
    assert response.status_code==200,response.text
    p=response.json();assert p['source_provenance']==provenance and p['revision']==0 and p['samples']==44100
    np.testing.assert_array_equal(sf.read(store.directory(p['id'])/'source.wav',dtype='float32',always_2d=True)[0],data)
    assert (store.directory(p['id'])/'source-original.wav').read_bytes()==source.read_bytes()
    assert store.load(p['id'])==p
    # Existing projects keep their original clock and metadata on load.
    existing=store.create(source,'Existing','');before=(store.directory(existing['id'])/'project.json').read_bytes()
    assert 'source_provenance' not in store.load(existing['id'])
    assert (store.directory(existing['id'])/'project.json').read_bytes()==before
