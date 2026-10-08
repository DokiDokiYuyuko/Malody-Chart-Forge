"""Independent CPU acceptance for Simple creator settings, HTTP and artifacts."""
import copy
import io
import json
import shutil
import zipfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from malody_studio import server
from malody_studio.charts import Note, serialize, chart_stats, package


def test_settings_creator_default_trim_and_limit():
    args = ('Song', 'Artist', '["easy"]', .15, 50, 42, None)
    assert server.settings(*args)['creator'] == 'Startrail'
    assert server.settings(*args, creator='  自定义 / 谱师  ')['creator'] == '自定义 / 谱师'
    assert server.settings(*args, creator='華' * 120)['creator'] == '華' * 120
    assert server.settings(*args, creator='  ' + '華' * 120 + '  ')['creator'] == '華' * 120


@pytest.mark.parametrize('creator', ['', '   ', '華' * 121, None, 42, True, []])
def test_settings_invalid_creator(creator):
    with pytest.raises(server.HTTPException) as error:
        server.settings('Song', 'Artist', '["easy"]', .15, 50, 42, None, creator=creator)
    assert error.value.status_code == 400


@pytest.fixture
def submission(tmp_path, monkeypatch):
    from malody_studio import music, music_assets
    (tmp_path / 'uploads').mkdir()
    source = tmp_path / 'uploads' / 'reference-sirius.m4a'
    source.write_bytes(b'fixture audio; never sent to inference')
    tags = tmp_path / 'vendor' / 'Mapperatorinator' / 'datasets' / 'tags_2026.json'
    tags.parent.mkdir(parents=True)
    shutil.copyfile(server.ROOT / 'vendor' / 'Mapperatorinator' / 'datasets' / 'tags_2026.json', tags)
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    monkeypatch.setattr(server, 'ensure_engine', lambda _: None)
    monkeypatch.setattr(music_assets, 'identify', lambda *_: None)
    monkeypatch.setattr(music, 'ready_audio', lambda _: (source, {'url':'https://example.com/music'}))
    monkeypatch.setattr(music, 'candidates', {'abcdefghijk':{'title':'Batch Song','artist':'Artist'}})
    monkeypatch.setattr(music, 'tracks', {})
    seen = []
    def capture(source, options):
        seen.append(copy.deepcopy(options))
        return {'id':str(len(seen))}
    monkeypatch.setattr(server, 'new_job', capture)
    monkeypatch.setattr(server, 'enqueue_job', lambda options, ref: capture(ref, options))
    return TestClient(server.app), seen


@pytest.mark.parametrize('route', ['upload', 'reference', 'music', 'batch'])
@pytest.mark.parametrize('creator', [None, '  HTTP 自定义谱师  ', '', '   ', '華' * 121,
                                   '  ' + '華' * 120 + '  '])
def test_actual_http_creator_submission(submission, route, creator):
    client, seen = submission
    fields = {'title':'Song', 'difficulties':'["easy"]'}
    if creator is not None:
        fields['creator'] = creator
    if route == 'upload':
        response = client.post('/api/jobs', data=fields, files={'file':('song.wav', b'audio', 'audio/wav')})
    elif route == 'reference':
        response = client.post('/api/reference', data=fields)
    elif route == 'music':
        response = client.post('/api/music/abcdefghijk/generate', data=fields)
    else:
        response = client.post('/api/batches', json={'video_ids':['abcdefghijk'], 'settings':fields})
    if creator is not None and (not creator.strip() or len(creator.strip()) > 120):
        assert response.status_code == 400, response.text
        assert seen == []
    else:
        assert response.status_code == 200, response.text
        assert len(seen) == 1
        assert seen[0]['creator'] == (creator.strip() if creator is not None else 'Startrail')


@pytest.mark.parametrize('status', ['completed', 'failed', 'interrupted'])
@pytest.mark.parametrize('creator', [None, '冻结署名'])
def test_http_retry_preserves_frozen_creator_and_missing_history(tmp_path, monkeypatch, status, creator):
    job_id = 'c' * 32
    source = tmp_path / ('outputs/' + job_id + '/0/audio.ogg' if status == 'completed'
                         else 'uploads/' + job_id + '.wav')
    source.parent.mkdir(parents=True)
    source.write_bytes(b'audio')
    options = {'title':'Song', 'artist':'Artist', 'engine':'mug', 'difficulties':['easy'],
               '_source_upload':job_id + '.wav'}
    if creator is not None:
        options['creator'] = creator
    frozen = copy.deepcopy(options)
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    monkeypatch.setattr(server, 'jobs', {job_id:{'id':job_id,'status':status,'title':'Song',
                                              'artist':'Artist','options':options}})
    monkeypatch.setattr(server, 'ensure_engine', lambda _: None)
    monkeypatch.setattr(server, 'queue_reservations', {})
    monkeypatch.setattr(server, 'store', lambda *_: None)
    monkeypatch.setattr(server, 'dispatch_next', lambda: None)
    (tmp_path / 'outputs').mkdir(exist_ok=True)
    # Keep the real queue freezer, which must not inject today's default on retry.
    with TestClient(server.app) as client:
        response = client.post('/api/jobs/' + job_id + '/regenerate')
    assert response.status_code == 200, response.text
    child = server.jobs[response.json()['id']]['options']
    if creator is None:
        assert 'creator' not in child
    else:
        assert child['creator'] == creator
    assert options == frozen and source.read_bytes() == b'audio'


@pytest.mark.parametrize('creator', [None, '单档冻结谱师'])
@pytest.mark.parametrize('new_creator', [None, '  单档新署名 / 谱师  '])
def test_http_regenerate_difficulty_zip_keeps_creator_and_old_version(tmp_path, monkeypatch, creator, new_creator):
    job_id = '9' * 32
    directory = tmp_path / 'outputs' / job_id
    directory.mkdir(parents=True)
    audio = tmp_path / 'audio.ogg'
    audio.write_bytes(b'ogg-test-data')
    notes = [Note(500 + i * 700, i % 4) for i in range(20)]
    expected = creator or 'Malody Chart Forge / MuG Diffusion v1.0.0'
    charts = {key:serialize(notes, 'Song', 'Artist', key, 120) for key in ('easy', 'medium')}
    for chart in charts.values():
        chart['meta']['creator'] = expected
    report = {'title':'Song','artist':'Artist','duration':20,'bpm':120,
              'engine':'MuG Diffusion v1.0.0','warnings':[],'quality_alerts':[],
              'previews':{},'artwork':{},'difficulties':[
                  {'key':key,'label':key,**chart_stats(notes,20),
                   'difficulty_adjustment':{'target_active_nps':2.5},'validation':{'valid':True}}
                  for key in charts]}
    if creator is not None:
        report['creator'] = creator
    package(directory, charts, audio, report)
    original = (directory / 'malody-4k.mcz').read_bytes()
    untouched = (directory / '0' / 'medium.mc').read_bytes()
    cache = {'format':1,'duration_ms':20000,'bpm':120,'engine':'mug',
             'model_version':'MuG Diffusion v1.0.0','timings':{},'raw_counts':{'easy':47},
             'candidates':[[i*400,1.,[[i*400,i%4,None]]] for i in range(1,48)]}
    (directory / 'generation-cache.json').write_text(json.dumps(cache), encoding='utf-8')
    options = {'title':'Song','artist':'Artist','engine':'mug','difficulties':['easy','medium'],
               'seed':7,'ln_ratio':.15,'difficulty_rules':{}}
    if creator is not None:
        options['creator'] = creator
    job = {'id':job_id,'title':'Song','artist':'Artist','status':'completed',
           'created':'now','options':options,'report':report}
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    monkeypatch.setattr(server, 'jobs', {job_id:job})
    with TestClient(server.app) as client:
        files = {path:path.read_bytes() for path in directory.rglob('*') if path.is_file()}
        frozen_job = copy.deepcopy(job)
        for invalid in ('', '   ', '華' * 121, None, 42, True, []):
            invalid_response = client.post(f'/api/jobs/{job_id}/difficulties/easy/regenerate',
                                           json={'seed':21, 'creator':invalid})
            assert invalid_response.status_code == 400, invalid_response.text
            assert {path:path.read_bytes() for path in directory.rglob('*') if path.is_file()} == files
            assert server.jobs[job_id] == frozen_job
        payload = {'seed':21}
        if new_creator is not None:
            payload['creator'] = new_creator
        response = client.post(f'/api/jobs/{job_id}/difficulties/easy/regenerate', json=payload)
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(directory / 'malody-4k.mcz') as archive:
        assert archive.testzip() is None
        assert json.loads(archive.read('0/easy.mc'))['meta']['creator'] == (new_creator.strip() if new_creator else expected)
        assert archive.read('0/medium.mc') == untouched
    assert (directory / 'versions' / 'easy' / 'v1.mcz').read_bytes() == original
    assert ('creator' in job['options']) == (creator is not None)
    if creator is not None:
        assert job['options']['creator'] == creator
    if creator is None:
        assert 'creator' not in response.json()['report']
    if new_creator:
        rows = response.json()['report']['difficulties']
        assert next(row for row in rows if row['key'] == 'easy')['creator'] == new_creator.strip()
        assert next(row for row in rows if row['key'] == 'medium').get('creator') != new_creator.strip()
        with TestClient(server.app) as client:
            continued = client.post(f'/api/jobs/{job_id}/difficulties/easy/regenerate', json={'seed':22})
        assert continued.status_code == 200, continued.text
        assert continued.json()['active'] == 3
        continued_rows = continued.json()['report']['difficulties']
        assert next(row for row in continued_rows if row['key'] == 'easy')['creator'] == new_creator.strip()
        with zipfile.ZipFile(directory / 'malody-4k.mcz') as archive:
            assert archive.testzip() is None
            assert json.loads(archive.read('0/easy.mc'))['meta']['creator'] == new_creator.strip()
            assert archive.read('0/medium.mc') == untouched
        assert (directory / 'versions' / 'easy' / 'v1.mcz').read_bytes() == original
    with TestClient(server.app) as client:
        restored = client.post(f'/api/jobs/{job_id}/difficulties/easy/restore', json={'version':1})
    assert restored.status_code == 200, restored.text
    with zipfile.ZipFile(directory / 'malody-4k.mcz') as archive:
        assert archive.testzip() is None
        assert json.loads(archive.read('0/easy.mc'))['meta']['creator'] == expected
        assert archive.read('0/medium.mc') == untouched
    restored_rows = restored.json()['report']['difficulties']
    assert next(row for row in restored_rows if row['key'] == 'easy')['creator'] == expected
    assert (directory / 'versions' / 'easy' / 'v1.mcz').read_bytes() == original


@pytest.mark.parametrize('route', ['legacy', 'direct'])
@pytest.mark.parametrize('creator', [None, '  模型输出署名  '])
def test_simple_pipeline_real_zip_creator_and_historical_fallback(tmp_path, monkeypatch, route, creator):
    from malody_studio import (pipeline, simple_generation, mapperatorinator, resident,
                               direct_v32, quality, quality_workflow, library, artwork)
    source = tmp_path / 'input.wav'
    sf.write(source, np.full((44100 * 8, 2), .1, np.float32), 44100, subtype='FLOAT')
    def worker(engine, message, progress):
        request = json.loads(Path(message['request_path']).read_text(encoding='utf-8'))
        paths = {}
        for preset in request['presets']:
            path = Path(request['output']) / (preset['key'] + '.osu')
            path.write_text('[General]\nMode:3\n[Difficulty]\nCircleSize:4\n'
                            '[TimingPoints]\n0,500,4,2,0,100,1,0\n[HitObjects]\n' +
                            '\n'.join(f'{64+(i%4)*128},192,{500+i*400},1,0,0:0:0:0:'
                                      for i in range(16)) + '\n', encoding='utf-8')
            paths[preset['key']] = str(path)
        return {'charts':paths, 'device':'test'}
    monkeypatch.setattr(resident, 'call', worker)
    monkeypatch.setattr(mapperatorinator, 'ready', lambda: True)
    monkeypatch.setattr(pipeline.engine, 'unload', lambda: None)
    monkeypatch.setattr(quality, 'assess', lambda *_: [])
    monkeypatch.setattr(quality_workflow, 'evidence_for', lambda *_: {'sources':{}})
    monkeypatch.setattr(library, 'publish', lambda *args, **kwargs: None)
    monkeypatch.setattr(artwork, 'prepare_artwork', lambda *_: (None, {}))
    monkeypatch.setattr(direct_v32, 'make_simple_plan', lambda *args: (
        {'id':'plan','samples':8*44100,'sections':[{'id':'full','core':[0,8*44100]}]},
        {'source':{'effective_end_sample':8*44100},'provenance':{'adapter':'beat_this_final1'}}))
    monkeypatch.setattr(direct_v32, 'quality_events', lambda events, *args: {
        'events':copy.deepcopy(events),'summary':{}})
    options = {'engine':'v32','title':'Song','artist':'Artist','seed':42,'ln_ratio':.15,
               'v32_difficulty':6,'steps':50,'patterns':['balanced'],
               'difficulties':['easy','hard'],'tail_trim_enabled':False}
    if route == 'legacy':
        options['simple_generation_policy'] = simple_generation.contract()
    if creator is not None:
        options['creator'] = creator
    options = simple_generation.freeze(options)
    frozen = copy.deepcopy(options)
    report, archive = pipeline.run(source, tmp_path / 'out', options, lambda *_: None)
    assert options == frozen
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.testzip() is None
        charts = [json.loads(zipped.read(name)) for name in zipped.namelist() if name.endswith('.mc')]
    assert len(charts) == 2
    if creator is None:
        assert 'creator' not in report
        historical = ('Malody Chart Forge / Mapperatorinator V32 (AI)' if route == 'legacy'
                      else 'Malody Studio / Mapperatorinator V32 (AI)')
        assert all(chart['meta']['creator'] == historical for chart in charts)
    else:
        assert report['creator'] == creator.strip()
        assert all(chart['meta']['creator'] == creator.strip() for chart in charts)


@pytest.mark.parametrize('top_creator', [None, '整包署名'])
def test_library_creator_identity_and_per_chart_precedence(tmp_path, top_creator):
    from malody_studio import library
    audio = tmp_path / 'audio.ogg'
    sf.write(audio, np.full((44100 * 2, 2), .1, np.float32), 44100, format='OGG', subtype='VORBIS')
    charts = {}
    for key in ('easy', 'hard'):
        charts[key] = serialize([Note(500, 0)], 'Song', 'Artist', key, 120)
        charts[key]['meta']['creator'] = '历史原谱师 / ' + key
    report = {'title':'Song','artist':'Artist','duration':2,
              'charts':[{'key':key,'pattern':'balanced','difficulty':key} for key in charts]}
    (tmp_path / 'original').mkdir()
    archive = package(tmp_path / 'original', charts, audio, report)
    frozen = archive.read_bytes()
    first_report = copy.deepcopy(report)
    if top_creator is not None:
        first_report['creator'] = top_creator
    first_report['charts'][0]['creator'] = '单谱甲'
    unchanged_report = copy.deepcopy(first_report)
    _, first_manifest, first_archive = library.publish(tmp_path, 'creator-identity', archive, first_report)
    assert first_report == unchanged_report
    def read_charts(path):
        with zipfile.ZipFile(path) as zipped:
            assert zipped.testzip() is None
            return {name:json.loads(zipped.read(name)) for name in zipped.namelist() if name.endswith('.mc')}
    published = read_charts(first_archive)
    assert published['0/balanced--easy.mc']['meta']['creator'] == '单谱甲'
    assert published['0/balanced--hard.mc']['meta']['creator'] == (top_creator or '历史原谱师 / hard')
    _, reused_manifest, reused_archive = library.publish(tmp_path, 'creator-identity', archive, first_report)
    assert reused_manifest['identity'] == first_manifest['identity']
    assert reused_archive.read_bytes() == first_archive.read_bytes()
    second_report = copy.deepcopy(first_report)
    second_report['charts'][0]['creator'] = '单谱乙'
    _, second_manifest, second_archive = library.publish(tmp_path, 'creator-identity', archive, second_report)
    assert second_manifest['identity'] != first_manifest['identity']
    updated = read_charts(second_archive)
    assert updated['0/balanced--easy.mc']['meta']['creator'] == '单谱乙'
    assert updated['0/balanced--hard.mc'] == published['0/balanced--hard.mc']
    assert updated['0/balanced--easy.mc']['note'] == published['0/balanced--easy.mc']['note']
    if top_creator is not None:
        third_report = copy.deepcopy(second_report)
        third_report['creator'] = '新整包署名'
        _, third_manifest, third_archive = library.publish(tmp_path, 'creator-identity', archive, third_report)
        assert third_manifest['identity'] != second_manifest['identity']
        latest = read_charts(third_archive)
        assert latest['0/balanced--easy.mc']['meta']['creator'] == '单谱乙'
        assert latest['0/balanced--hard.mc']['meta']['creator'] == '新整包署名'
    assert archive.read_bytes() == frozen


@pytest.mark.parametrize('top_creator', [None, '整包当前署名'])
def test_historical_single_version_download_uses_archived_creator(tmp_path, monkeypatch, top_creator):
    from malody_studio import library
    job_id = 'a' * 32
    directory = tmp_path / 'outputs' / job_id
    directory.mkdir(parents=True)
    audio = tmp_path / 'audio.ogg'
    sf.write(audio, np.full((44100 * 2, 2), .1, np.float32), 44100,
             format='OGG', subtype='VORBIS')
    original = serialize([Note(500, 0)], 'Song', 'Artist', 'Balanced Easy', 120)
    original['meta']['creator'] = 'v1 历史谱师'
    current = copy.deepcopy(original)
    current['meta']['creator'] = 'v2 当前谱师'
    row = {'key':'easy','difficulty':'easy','pattern':'balanced',
           'creator':current['meta']['creator'],**chart_stats([Note(500, 0)], 2)}
    report = {'title':'Song','artist':'Artist','duration':2,'charts':[row],'difficulties':[row]}
    if top_creator is not None:
        report['creator'] = top_creator
    package(directory, {'easy':current}, audio, report)
    versions = directory / 'versions' / 'easy'
    versions.mkdir(parents=True)
    for number, chart in ((1, original), (2, current)):
        (versions / f'v{number}.mc').write_text(json.dumps(chart, ensure_ascii=False), encoding='utf-8')
    job = {'id':job_id,'title':'Song','artist':'Artist','status':'completed',
           'created':'2026-10-07T00:00:00+00:00','report':report,
           'options':{'creator':current['meta']['creator']}}
    frozen_job = copy.deepcopy(job)
    frozen_files = {path:path.read_bytes() for path in directory.rglob('*') if path.is_file()}
    captured = []
    real_publish = library.publish
    def capture(root, record_id, archive, published_report, *args, **kwargs):
        captured.append((record_id, Path(archive).read_bytes(), copy.deepcopy(published_report)))
        return real_publish(root, record_id, archive, published_report, *args, **kwargs)
    monkeypatch.setattr(library, 'publish', capture)
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    monkeypatch.setattr(server, 'jobs', {job_id:job})
    with TestClient(server.app) as client:
        # Download v2 first, then v1: a current row or a previous publication
        # must never supply the historical file's creator.
        for number, expected in ((2, 'v2 当前谱师'), (1, 'v1 历史谱师'), (1, 'v1 历史谱师')):
            response = client.get(f'/api/jobs/{job_id}/difficulties/easy/versions/v{number}.mcz')
            assert response.status_code == 200, response.text
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                assert archive.testzip() is None
                charts = [json.loads(archive.read(name)) for name in archive.namelist() if name.endswith('.mc')]
            assert len(charts) == 1 and charts[0]['meta']['creator'] == expected
            record_id, data, published_report = captured[-1]
            assert record_id == job_id + f':easy:v{number}'
            assert published_report['creator'] == expected
            assert published_report['charts'][0]['creator'] == expected
            assert published_report['difficulties'][0]['creator'] == expected
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                assert json.loads(archive.read('0/easy.mc'))['meta']['creator'] == expected
            assert server.jobs[job_id] == frozen_job
            assert all(path.read_bytes() == content for path, content in frozen_files.items())
