import json
import subprocess
from pathlib import Path
import pytest
from malody_studio import music, music_assets as assets


def candidate(i=1):
    return {'id': str(i).zfill(11), 'video_id': str(i).zfill(11), 'platform': 'youtube',
            'part': 1, 'url': music.url_for(str(i).zfill(11)), 'title': 'Song', 'artist': 'Artist'}


def test_three_search_pages_advance_actual_scanned_position(monkeypatch):
    rows = [{'id': str(i).zfill(11), 'duration': 3 if i % 5 == 0 else 90} for i in range(140)]
    calls = []
    def command(*args, **kwargs):
        start, end = int(args[args.index('--playlist-start') + 1]), int(args[args.index('--playlist-end') + 1])
        calls.append((start, end))
        return json.dumps({'entries': rows[start-1:end]})
    monkeypatch.setattr(music, 'command', command)
    cursor, pages = 0, []
    for _ in range(3):
        page = music.search_page('songs', cursor=cursor, limit=24)
        assert len(page['results']) == 24
        assert page['scanned'] == 30
        pages.append({r['id'] for r in page['results']})
        cursor = page['next_cursor']
    assert not pages[0] & pages[1] and not pages[1] & pages[2]
    assert calls == [(1, 96), (31, 126), (61, 156)]


def test_search_insufficient_filters_consumes_max_96(monkeypatch):
    rows = [{'id': str(i).zfill(11), 'duration': 3} for i in range(96)]
    rows[-1]['duration'] = 30
    monkeypatch.setattr(music, 'command', lambda *a, **k: json.dumps({'entries': rows}))
    page = music.search_page('filtered', limit=24)
    assert len(page['results']) == 1 and page['scanned'] == page['next_cursor'] == 96
    monkeypatch.setattr(music, 'command', lambda *a, **k: json.dumps({'entries': rows[:15]}))
    page = music.search_page('filtered', limit=24)
    assert not page['has_more'] and page['next_cursor'] is None


def test_search_cursor_upper_bound_never_advertises_invalid_next(monkeypatch):
    calls = []
    def command(*args, **kwargs):
        calls.append(args)
        return json.dumps({'entries': [{'id': str(i).zfill(11), 'duration': 60} for i in range(20)]})
    monkeypatch.setattr(music, 'command', command)
    page = music.search_page('edge', cursor=9980, limit=24)
    assert len(page['results']) == 20 and page['scanned'] == 20
    assert not page['has_more'] and page['next_cursor'] is None
    assert calls[0][calls[0].index('--playlist-end') + 1] == '10000'
    assert music.search_page('edge', cursor=10000, limit=24)['scanned'] == 0
    assert len(calls) == 1


def test_excluded_previous_page_ids_consume_real_cursor(monkeypatch):
    rows = [{'id': str(i).zfill(11), 'duration': 60} for i in range(96)]
    monkeypatch.setattr(music, 'command', lambda *a, **kw: json.dumps({'entries': rows}))
    excluded = [str(i).zfill(11) for i in range(3)]
    page = music.search_page('shifting ranks', cursor=27, limit=24, exclude_ids=excluded)
    assert len(page['results']) == 24 and page['filtered']['duplicate'] == 3
    assert page['scanned'] == 27 and page['next_cursor'] == 54
    assert not set(excluded).intersection(row['id'] for row in page['results'])


def test_name_units_artist_preservation_reserved_and_collision():
    name = assets.filename('曲😀' * 300, '音楽人', 'webm', '（2）')
    assert assets.units(Path(name).stem) <= 180 and ' - 音楽人（2）' in name
    assert assets.filename('CON', '', 'aac') == '_CON.aac'
    assert '/' not in assets.filename('A/B:*?', '', 'aac')


def test_store_idempotency_renaming_collision_hash_and_restart(monkeypatch, tmp_path):
    class Queue:
        calls = []
        def submit(self, *args): self.calls.append(args)
    queue = Queue()
    monkeypatch.setattr(assets, 'pool', queue)
    store = assets.AssetStore(tmp_path)
    row = candidate()
    store.state['candidates'][row['id']] = row
    source = tmp_path / 'original.webm'
    source.write_bytes(b'untouched original')
    meta = {'extension': 'webm', 'format': 'matroska', 'codec': 'opus', 'duration': 60}
    downloads = []
    def download(row):
        downloads.append(row['id'])
        directory = store.registry / 'sources' / row['id']
        directory.mkdir(parents=True, exist_ok=True)
        saved = directory / 'audio.webm'
        saved.write_bytes(source.read_bytes())
        return saved, meta
    monkeypatch.setattr(store, 'download_source', download)
    first = store.begin(row['id'], '曲名', '音楽人')
    assert store.begin(row['id'], '曲名', '音楽人')['id'] == first['id'] and len(queue.calls) == 1
    store.run(first['id'])
    renamed = store.begin(row['id'], '別の曲名', '')
    store.run(renamed['id'])
    assert len(downloads) == 1 and len(store.assets()) == 2
    row2 = candidate(2)
    collision = store.save(row2, '曲名', '音楽人', source, meta)
    assert collision['filename'] == '曲名 - 音楽人（2）.webm'
    assert store.identify(source)['title'] == '曲名'
    assert store.identify(source,'別の曲名.webm')['title']=='別の曲名'
    assert source.read_bytes() == b'untouched original'
    interrupted = store.begin(row['id'], 'pending', '')
    restarted = assets.AssetStore(tmp_path)
    assert restarted.task(first['id'])['status'] == 'ready'
    assert restarted.task(interrupted['id'])['status'] == 'failed'
    assert restarted.begin(row['id'], '曲名', '音楽人')['id'] == first['id']
    saved = restarted.task(first['id'])['asset']
    Path(saved['path']).write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='完整性'):
        restarted.begin(row['id'], '曲名', '音楽人')
    with pytest.raises(ValueError, match='完整性'):
        restarted.save(row, '曲名', '音楽人', source, meta)


def bili_payload(**updates):
    return {'code': 0, 'data': {'bvid': 'BV17PTt6fEJJ', 'title': '音乐', 'duration': 60,
                               'author': {'nickname': '作者'}, 'content': {'play_url': 'https://cdn.example/music.mp4'}, **updates}}


def test_bili_primary_and_fallback_do_not_clear_metadata(monkeypatch):
    responses = [bili_payload(content={}), bili_payload(title='', author={})]
    urls = []
    def request(url):
        urls.append(url)
        return responses.pop(0)
    monkeypatch.setattr(assets, 'json_request', request)
    row = assets.resolve_bili('BV17PTt6fEJJ')
    assert row['title'] == '音乐' and row['artist'] == '作者'
    assert 'vrc-json' in urls[0] and 'kfc-json' in urls[1]
    assert row['platform'] == 'bilibili' and row['part'] == 1


def test_bili_unverified_part_and_parser_failure(monkeypatch):
    monkeypatch.setattr(assets, 'json_request', lambda url: bili_payload())
    with pytest.raises(ValueError, match='未验证所选分P'):
        assets.resolve_bili('https://www.bilibili.com/video/BV17PTt6fEJJ?p=2')
    monkeypatch.setattr(assets, 'json_request', lambda url: {'error': True, 'code': 'PARSE_ERROR', 'message': '解析失败细节'})
    with pytest.raises(music.MusicSourceError, match='解析失败细节') as caught:
        assets.resolve_bili('BV17PTt6fEJJ')
    assert caught.value.diagnostic_id


def test_resource_expiry_refreshes_once_preserves_confirmed_metadata(monkeypatch, tmp_path):
    store = assets.AssetStore(tmp_path)
    row = {**candidate(), 'id': 'bili-BV17PTt6fEJJ-p1', 'video_id': 'BV17PTt6fEJJ',
           'platform': 'bilibili', 'url': 'https://www.bilibili.com/video/BV17PTt6fEJJ',
           'resource_url': 'https://cdn.example/expired'}
    calls, reparsed = [], []
    def fetch(url, path):
        calls.append(url)
        raise assets.ResourceExpired('expired')
    monkeypatch.setattr(assets, 'fetch_resource', fetch)
    monkeypatch.setattr(assets, 'resolve_bili', lambda url, previous: reparsed.append(previous) or {**previous, 'resource_url': 'https://cdn.example/new'})
    with pytest.raises(assets.ResourceExpired): store.download_source(row)
    assert len(calls) == 2 and len(reparsed) == 1 and reparsed[0]['title'] == 'Song'


def test_mixed_video_is_stream_copied_and_webm_stays_exact(tmp_path):
    from malody_studio.audio import ffmpeg
    video = tmp_path / 'source.mp4'
    result = subprocess.run([ffmpeg(), '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                             'color=c=black:s=32x32:r=10', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=6',
                             '-shortest', '-c:v', 'libx264', '-c:a', 'aac', str(video)], capture_output=True)
    assert result.returncode == 0
    target, meta = assets.source_audio(video, tmp_path)
    assert target.suffix == '.m4a' and not meta['has_video'] and meta['codec'] == 'aac'
    # Compressed elementary audio packets remain byte-for-byte identical.
    packets = []
    for path in (video, target):
        result = subprocess.run([ffmpeg(), '-hide_banner', '-loglevel', 'error', '-i', str(path),
                                 '-map', '0:a:0', '-c:a', 'copy', '-f', 'adts', '-'], capture_output=True)
        assert result.returncode == 0
        packets.append(result.stdout)
    assert packets[0] == packets[1]
    webm = tmp_path / 'original.webm'
    result = subprocess.run([ffmpeg(), '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                             'sine=frequency=440:duration=6', '-c:a', 'libopus', str(webm)], capture_output=True)
    assert result.returncode == 0
    destination, meta = assets.source_audio(webm, tmp_path)
    assert destination.read_bytes() == webm.read_bytes() and destination.suffix == '.webm'


def test_unsupported_audio_and_invalid_duration(tmp_path):
    source = tmp_path / 'bad.bin'
    source.write_bytes(b'not audio')
    with pytest.raises(ValueError, match='音轨验证失败'): assets.probe(source)


def test_api_missing_invalid_paths_and_folder_guard(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from malody_studio.music_assets_api import router
    store = assets.AssetStore(tmp_path)
    monkeypatch.setattr(assets, 'store', store)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get('/api/music-assets/downloads/missing').status_code == 404
        assert client.get('/api/music-assets/missing/file').status_code == 404
        assert client.post('/api/music-assets/downloads', json={'source_id': {}, 'title': 'Song'}).status_code == 400
        assert client.post('/api/music-assets/resolve', json={'input': 'keywords'}).status_code == 400
        assert client.post('/api/music-assets/open-folder', json={'path': str(tmp_path)}).status_code == 400
        file = tmp_path / 'source.webm'
        file.write_bytes(b'audio hash')
        row = store.save(candidate(), 'Music', 'Artist', file, {'extension': 'webm', 'format': 'webm', 'codec': 'opus', 'duration': 60})
        response = client.post('/api/music-assets/identify', files={'file': ('name.webm', b'audio hash')})
        assert response.status_code == 200 and response.json()['asset']['id'] == row['id']
        assert client.get(row['file_url']).content == b'audio hash'
        Path(row['path']).write_bytes(b'changed')
        assert client.get(row['file_url']).status_code == 409


def test_simultaneous_names_share_single_source_download(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    class Queue:
        def submit(self, *args): pass
    monkeypatch.setattr(assets, 'pool', Queue())
    store = assets.AssetStore(tmp_path)
    row = candidate()
    store.state['candidates'][row['id']] = row
    started, proceed = threading.Event(), threading.Event()
    calls = []
    def download(row):
        calls.append(row['id'])
        started.set()
        assert proceed.wait(5)
        directory = store.registry / 'sources' / row['id']
        directory.mkdir(parents=True)
        source = directory / 'audio.webm'
        source.write_bytes(b'complete audio')
        return source, {'extension': 'webm', 'format': 'webm', 'codec': 'opus', 'duration': 60}
    monkeypatch.setattr(store, 'download_source', download)
    first = store.begin(row['id'], 'First', '')
    second = store.begin(row['id'], 'Second', '')
    with ThreadPoolExecutor(max_workers=2) as workers:
        a = workers.submit(store.run, first['id'])
        assert started.wait(5)
        b = workers.submit(store.run, second['id'])
        proceed.set()
        a.result(timeout=10)
        b.result(timeout=10)
    assert len(calls) == 1 and len(store.assets()) == 2
    assert all(t['status'] == 'ready' for t in store.tasks())
