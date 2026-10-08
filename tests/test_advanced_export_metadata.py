"""Advanced song metadata survives export without mutating earlier packages."""
import hashlib
import json
import zipfile

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from malody_studio import advanced, advanced_api, library
from malody_studio.server import app


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def contents(blob):
    import io
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        charts = {name: json.loads(archive.read(name))
                  for name in archive.namelist() if name.endswith('.mc')}
        return charts, archive.read('0/audio.ogg')


@pytest.mark.parametrize('artist', ['自定义音乐人', ''])
def test_saved_metadata_reexports_new_package_and_keeps_historical_download(tmp_path, monkeypatch, artist):
    source = tmp_path / 'source.wav'
    sf.write(source, np.sin(np.arange(advanced.SR * 3) * .1) * .1, advanced.SR)
    monkeypatch.setattr(advanced, 'audio_metadata', lambda data: (
        [.1] * 2400, {'bpm':120., 'beat_times':[0, .5, 1, 1.5, 2],
                      'beat_variability':0, 'uncertain':False, 'warnings':[]}))
    store = advanced.ProjectStore(tmp_path / 'outputs' / 'advanced')
    monkeypatch.setattr(advanced_api, 'store', store)
    monkeypatch.setattr(advanced_api, 'ROOT', tmp_path)
    p = store.create(source, '下载默认曲名', '下载频道名')
    p = store.update(p['id'], {'patterns':['balanced'], 'difficulties':['easy']})
    p = store.add_segment(p['id'], {'start_sample':0, 'end_sample':2 * advanced.SR})
    segment = p['segments'][0]
    revision = store.add_revision(p['id'], segment['id'], 'balanced--easy', [
        {'id':'fixture-head', 'start_ms':500, 'end_ms':None, 'lane':0}],
        p['settings'], 'model_raw', activate_initial=True)
    client = TestClient(app)
    base = '/api/advanced/projects/' + p['id']
    first = client.post(base + '/assemblies', json={'preroll':0, 'require_complete':True}).json()
    old_download = client.get(first['download'])
    assert old_download.status_code == 200
    old_charts, old_audio = contents(old_download.content)
    old_folder, _, old_archive = library.locate(tmp_path, 'advanced-' + first['id'])
    old_files = {path: digest(path) for path in old_folder.iterdir() if path.is_file()}
    revision_path = store.directory(p['id']) / 'revisions' / (revision['id'] + '.json')
    original_revision = revision_path.read_bytes()
    original_source = digest(store.directory(p['id']) / 'source.wav')

    current = store.load(p['id'])
    saved = client.patch(base, json={'title':'手填曲名：新版本', 'artist':artist,
                                    'expected_revision':current['revision']})
    assert saved.status_code == 200
    second_response = client.post(base + '/assemblies', json={
        'preroll':0, 'require_complete':True, 'expected_revision':saved.json()['revision']})
    assert second_response.status_code == 200
    second = second_response.json()
    assert second['id'] != first['id']
    delivered = client.get(second['download'])
    assert delivered.status_code == 200
    charts, audio = contents(delivered.content)
    assert set(charts) == set(old_charts)
    for name, chart in charts.items():
        assert chart['meta']['song']['title'] == '手填曲名：新版本'
        assert chart['meta']['song']['artist'] == artist
        assert chart['note'] == old_charts[name]['note']
        assert chart['time'] == old_charts[name]['time']
    # OGG serial numbers vary on a new export; decoded samples must not change.
    import io
    new_pcm, new_rate = sf.read(io.BytesIO(audio), dtype='float32', always_2d=True)
    old_pcm, old_rate = sf.read(io.BytesIO(old_audio), dtype='float32', always_2d=True)
    assert new_rate == old_rate
    np.testing.assert_array_equal(new_pcm, old_pcm)
    assert 'filename*=' in delivered.headers['content-disposition']
    assert client.get(first['download']).content == old_download.content
    assert all(digest(path) == expected for path, expected in old_files.items())
    assert old_archive.is_file() and revision_path.read_bytes() == original_revision
    assert digest(store.directory(p['id']) / 'source.wav') == original_source

    stale = client.patch(base, json={'title':'过期请求', 'expected_revision':current['revision']})
    assert stale.status_code == 409
    assert store.load(p['id'])['title'] == '手填曲名：新版本'
