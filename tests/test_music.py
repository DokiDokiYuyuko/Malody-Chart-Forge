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

def test_generate_uses_downloaded_source_and_provenance(monkeypatch,tmp_path):
    track={'url':'https://www.youtube.com/watch?v=UKZt1vq8bKI'}
    monkeypatch.setattr(music,'ready_audio',lambda video_id:(tmp_path/'audio.ogg',track))
    captured=[]
    monkeypatch.setattr(server,'new_job',lambda source,opts:captured.append((source,opts)) or {'id':'test'})
    with TestClient(server.app) as client:
        result=client.post('/api/music/UKZt1vq8bKI/generate',data={'title':'曲名','difficulties':'["easy"]'})
    assert result.status_code==200
    assert captured[0][0]==tmp_path/'audio.ogg'
    assert captured[0][1]['source']==track['url']
