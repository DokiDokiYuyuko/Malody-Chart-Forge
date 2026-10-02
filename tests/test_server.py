import pytest
from fastapi.testclient import TestClient
from malody_studio import server

def test_six_difficulties_and_legacy_normal_alias():
    options = server.settings('t', 'a', '["easy","medium","hard","expert","master","lunatic"]', .15, 50, 42, None)
    assert len(options['difficulties']) == 6
    assert server.settings('t','a','["normal"]',.15,50,42,None)['difficulties'] == ['medium']
    with TestClient(server.app) as client:
        assert client.post('/api/reference', data={'difficulties':'["normal","medium"]'}).status_code == 400

def test_history_pagination_keeps_all_records_and_supports_search(monkeypatch):
    records = {str(i): dict(id=str(i),title=f'Song {i}',artist='a',created=f'{i:03}',status='completed',options={'engine':'v32'}) for i in range(70)}
    monkeypatch.setattr(server, 'jobs', records)
    with TestClient(server.app) as client:
        first = client.get('/api/history?page=1&page_size=6').json()
        second = client.get('/api/history?page=2&page_size=6').json()
        last = client.get('/api/history?page=999&page_size=6').json()
        assert first['total'] == 70 and first['pages'] == 12 and len(first['items']) == 6
        assert not set(x['id'] for x in first['items']) & set(x['id'] for x in second['items'])
        assert len(last['items']) == 4 and last['page'] == 12
        assert client.get('/api/history?q=Song%2069').json()['total'] == 1
        assert client.get('/api/history?page=0').status_code == 422

def test_bad_difficulty_input_returns_400():
    with TestClient(server.app) as client:
        for value in ('bad-json', '[]', '[["easy"]]', '[null]', '["easy","easy"]'):
            assert client.post('/api/reference', data={'difficulties': value}).status_code == 400

def test_unknown_engine_and_unavailable_v32_are_rejected(monkeypatch):
    from malody_studio import mapperatorinator
    monkeypatch.setattr(mapperatorinator, 'ready', lambda: False)
    with TestClient(server.app) as client:
        assert client.post('/api/reference', data={'engine': '../../bad'}).status_code == 400
        assert client.post('/api/reference', data={'engine': 'v32'}).status_code == 503

def test_upload_preserves_bytes_and_validates_type(monkeypatch):
    received = []
    def capture(source, options):
        received.append((source, source.read_bytes(), options))
        return {'id': 'test-upload'}
    monkeypatch.setattr(server, 'new_job', capture)
    with TestClient(server.app) as client:
        assert client.post('/api/jobs', files={'file': ('track.exe', b'bad')}, data={'title': 'test'}).status_code == 400
        reply = client.post('/api/jobs', files={'file': ('track.wav', b'RIFFtest')}, data={'title': '音乐'})
        assert reply.status_code == 200
        assert received[0][1] == b'RIFFtest'
        assert received[0][2]['title'] == '音乐'
    received[0][0].unlink()

@pytest.mark.parametrize('header,code,expected', [
    (None, 200, b'0123456789'), ('bytes=3-5', 206, b'345'),
    ('bytes=-3', 206, b'789'), ('bytes=7-', 206, b'789'),
    ('bytes=20-', 416, b''), ('bytes=5-2', 416, b''), ('bytes=bad', 416, b''),
])
def test_ogg_seek_ranges(tmp_path, header, code, expected):
    from fastapi import FastAPI, Request
    app = FastAPI()
    audio = tmp_path / 'test.ogg'
    audio.write_bytes(b'0123456789')
    @app.get('/audio')
    def endpoint(request: Request):
        return server.audio_response(audio, request.headers.get('range'))
    with TestClient(app) as client:
        result = client.get('/audio', headers={'Range': header} if header else {})
        assert result.status_code == code
        assert result.content == expected
