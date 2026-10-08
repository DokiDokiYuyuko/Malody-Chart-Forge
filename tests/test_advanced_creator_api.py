"""Actual Advanced HTTP creator contracts, with synthetic audio and no model calls."""
import io
import json
import zipfile

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from malody_studio import advanced, advanced_api, music_assets
from malody_studio.server import app


@pytest.fixture
def creator_api(tmp_path, monkeypatch):
    source = tmp_path / 'fixture.wav'
    sf.write(source, np.sin(np.arange(advanced.SR * 2) * .1) * .1,
             advanced.SR, subtype='FLOAT')
    monkeypatch.setattr(advanced, 'audio_metadata', lambda data: (
        [.1] * 2400, {'bpm':120., 'beat_times':[0, .5, 1, 1.5],
                      'beat_variability':0, 'uncertain':False, 'warnings':[]}))
    store = advanced.ProjectStore(tmp_path / 'outputs' / 'advanced')
    (tmp_path / 'cache').mkdir()
    monkeypatch.setattr(advanced_api, 'store', store)
    monkeypatch.setattr(advanced_api, 'ROOT', tmp_path)
    asset = {'id':'creator-fixture', 'title':'Identified fixture', 'artist':'Artist',
             'sha256':'fixture-sha256', 'thumbnail':None}
    monkeypatch.setattr(music_assets, 'identify', lambda *args, **kwargs: None)
    monkeypatch.setattr(music_assets, 'get_asset', lambda asset_id: dict(asset))
    monkeypatch.setattr(music_assets, 'asset_path', lambda asset_id: source)
    monkeypatch.setattr(music_assets, 'asset_cover', lambda asset_id: None)
    # No TestClient lifespan: these requests must not start production workers.
    client = TestClient(app)
    yield client, store, source, asset
    client.close()


MISSING = object()


def create(client, source, route, creator=MISSING):
    if route == 'asset':
        payload = {'type':'asset', 'id':'creator-fixture'}
        if creator is not MISSING:
            payload['creator'] = creator
        return client.post('/api/advanced/projects/from-source', json=payload)
    data = {'title':'Uploaded fixture', 'artist':'Artist'}
    if creator is not MISSING:
        data['creator'] = creator
    return client.post('/api/advanced/projects/upload', data=data,
                       files={'file':('fixture.wav', source.read_bytes(), 'audio/wav')})


def charts(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert archive.testzip() is None
        result = {name:json.loads(archive.read(name)) for name in archive.namelist()
                  if name.endswith('.mc')}
        assert result
        return result


def export(client, base, revision):
    response = client.post(base + '/assemblies', json={
        'preroll':0, 'require_complete':True, 'expected_revision':revision})
    assert response.status_code == 200, response.text
    assembly = response.json()
    delivered = client.get(assembly['download'])
    assert delivered.status_code == 200, delivered.text
    return assembly, delivered.content, charts(delivered.content)


@pytest.mark.parametrize('route', ['upload', 'identified-upload', 'asset'])
def test_http_creation_patch_and_real_export_keep_creator(creator_api, monkeypatch, route):
    client, store, source, asset = creator_api
    if route == 'identified-upload':
        monkeypatch.setattr(music_assets, 'identify', lambda *args, **kwargs: dict(asset))
    response = create(client, source, route, '  首次谱师 / 谱师  ')
    assert response.status_code == 200, response.text
    p = response.json()
    # The first POST itself must persist creator, before any PATCH fallback.
    assert p['revision'] == 0
    assert p['creator'] == '首次谱师 / 谱师'
    directory = store.directory(p['id'])
    assert json.loads((directory / 'project.json').read_text(encoding='utf-8'))['creator'] == p['creator']
    base = '/api/advanced/projects/' + p['id']
    loaded = client.get(base)
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()['creator'] == p['creator']
    if route != 'upload':
        assert p['source_provenance']['asset_id'] == asset['id']

    # Supply fixed chart evidence directly; generation/inference is never invoked.
    p = store.update(p['id'], {'patterns':['balanced'], 'difficulties':['easy']})
    p = store.add_segment(p['id'], {'start_sample':0, 'end_sample':2 * advanced.SR})
    revision = store.add_revision(p['id'], p['segments'][0]['id'], 'balanced--easy', [
        {'id':'fixture-tap', 'start_ms':250, 'end_ms':None, 'lane':0},
        {'id':'fixture-hold', 'start_ms':750, 'end_ms':1250, 'lane':2}],
        p['settings'], 'model_raw', activate_initial=True)
    frozen = {path:path.read_bytes() for path in directory.glob('source*')}
    revision_path = directory / 'revisions' / (revision['id'] + '.json')
    frozen[revision_path] = revision_path.read_bytes()
    first, first_blob, first_charts = export(client, base, store.load(p['id'])['revision'])
    assert first['creator'] == '首次谱师 / 谱师'
    assert all(chart['meta']['creator'] == first['creator'] for chart in first_charts.values())

    before = store.load(p['id'])
    saved = client.patch(base, json={'creator':'  后续自填谱师  ', 'expected_revision':before['revision']})
    assert saved.status_code == 200, saved.text
    assert saved.json()['creator'] == '后续自填谱师'
    assert client.get(base).json()['creator'] == '后续自填谱师'
    assert store.load(p['id'])['creator'] == '后续自填谱师'
    second, _, second_charts = export(client, base, saved.json()['revision'])
    assert second['creator'] == '后续自填谱师'
    assert second['id'] != first['id']
    assert second_charts.keys() == first_charts.keys()
    for name, chart in second_charts.items():
        assert chart['meta']['creator'] == '后续自填谱师'
        for field in ['note', 'time', 'effect']:
            assert chart[field] == first_charts[name][field]
    assert client.get(first['download']).content == first_blob
    assert all(path.read_bytes() == content for path, content in frozen.items())
    stale = client.patch(base, json={'creator':'Stale', 'expected_revision':before['revision']})
    assert stale.status_code == 409
    assert store.load(p['id'])['creator'] == '后续自填谱师'


@pytest.mark.parametrize('route', ['upload', 'asset'])
@pytest.mark.parametrize('creator,expected', [
    (MISSING, 'Startrail'), ('華' * 120, '華' * 120),
    (' ' + '華' * 120 + ' ', '華' * 120)],
    ids=['omitted-default', 'exact-limit', 'trimmed-limit'])
def test_http_creation_default_and_trimmed_limit(creator_api, route, creator, expected):
    client, store, source, _ = creator_api
    response = create(client, source, route, creator)
    assert response.status_code == 200, response.text
    p = response.json()
    assert p['creator'] == store.load(p['id'])['creator'] == expected


@pytest.mark.parametrize('route', ['upload', 'identified-upload', 'asset'])
@pytest.mark.parametrize('creator', ['', '   ', '華' * 121],
                         ids=['empty', 'whitespace', 'over-limit'])
def test_invalid_http_creator_never_creates_project(creator_api, monkeypatch, route, creator):
    client, store, source, asset = creator_api
    if route == 'identified-upload':
        monkeypatch.setattr(music_assets, 'identify', lambda *args, **kwargs: dict(asset))
    before = set(store.root.iterdir())
    response = create(client, source, route, creator)
    assert response.status_code in (400, 422), response.text
    assert set(store.root.iterdir()) == before
    assert not list((source.parent / 'cache').iterdir())


@pytest.mark.parametrize('creator', [None, 42, [], True])
def test_invalid_json_creator_types_never_create_project(creator_api, creator):
    client, store, source, _ = creator_api
    before = set(store.root.iterdir())
    response = create(client, source, 'asset', creator)
    assert response.status_code in (400, 422), response.text
    assert set(store.root.iterdir()) == before
