import pytest
from fastapi.testclient import TestClient
from malody_studio import server

@pytest.fixture
def empty_advanced_library(monkeypatch, tmp_path):
    from malody_studio import advanced, advanced_api
    store = advanced.ProjectStore(tmp_path / 'advanced')
    monkeypatch.setattr(advanced_api, 'store', store)
    return store


def test_six_difficulties_and_legacy_normal_alias():
    options = server.settings('t', 'a', '["easy","medium","hard","expert","master","lunatic"]', .15, 50, 42, None)
    assert len(options['difficulties']) == 6
    assert server.settings('t','a','["normal"]',.15,50,42,None)['difficulties'] == ['medium']
    with TestClient(server.app) as client:
        assert client.post('/api/reference', data={'difficulties':'["normal","medium"]'}).status_code == 400


def test_health_reports_engine_readiness_independently(monkeypatch, tmp_path):
    import torch
    from malody_studio import mapperatorinator
    monkeypatch.setattr(server, 'WEIGHTS', tmp_path / 'missing-mug.ckpt')
    monkeypatch.setattr(mapperatorinator, 'ready', lambda: True)
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: True)
    monkeypatch.setattr(torch.cuda, 'get_device_name', lambda index: 'Test GPU')
    with TestClient(server.app) as client:
        health = client.get('/api/health').json()
    assert health['api_version'] == 2
    assert health['ready'] is True
    assert health['engines']['mug']['ready'] is False
    assert health['engines']['mug']['reason']
    assert health['engines']['v32']['ready'] is True




def test_mapperatorinator_manifest_endpoint_omits_url_credentials():
    from tools.download_mapperatorinator import public_endpoint
    safe = public_endpoint('https://user:secret@mirror.example/hf?token=private')
    assert safe == 'https://mirror.example/hf'
    assert 'secret' not in safe and 'private' not in safe

def test_mug_readiness_requires_pinned_sha256_and_engine_rejects_bad_file(monkeypatch, tmp_path):
    import hashlib
    import os
    weight = tmp_path / 'model.ckpt'
    weight.write_bytes(b'good')
    monkeypatch.setattr(server, 'WEIGHTS', weight)
    monkeypatch.setattr(server, 'MUG_WEIGHT_BYTES', 4)
    monkeypatch.setattr(server, 'MUG_WEIGHT_SHA256', hashlib.sha256(b'good').hexdigest())
    assert server.mug_weights_ready() is True
    previous = weight.stat()
    weight.write_bytes(b'evil')
    os.utime(weight, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000))
    assert server.mug_weights_ready() is False
    with pytest.raises(server.HTTPException) as error:
        server.ensure_engine({'engine':'mug'})
    assert error.value.status_code == 503

def test_history_filters_engine_status_difficulty_and_sort(monkeypatch, empty_advanced_library):
    records = {
        'a': dict(id='a', title='Alpha', artist='Singer', created='2025-01', status='completed',
                  options={'engine':'mug','difficulties':['easy']}, report={'difficulties':[{'key':'easy','label':'Easy'}]}),
        'b': dict(id='b', title='Beta', artist='Band', created='2025-02', status='failed',
                  options={'engine':'v32','difficulties':['hard']}, report={}),
    }
    monkeypatch.setattr(server, 'jobs', records)
    with TestClient(server.app) as client:
        assert client.get('/api/history?engine=v32').json()['items'][0]['id'] == 'b'
        assert client.get('/api/history?status=failed').json()['total'] == 1
        assert client.get('/api/history?difficulty=hard').json()['total'] == 1
        assert client.get('/api/history?sort=oldest').json()['items'][0]['id'] == 'a'
        assert client.get('/api/history?engine=invalid').status_code == 400


def test_storage_reports_output_and_upload_sizes(monkeypatch, tmp_path):
    (tmp_path / 'outputs' / 'job').mkdir(parents=True)
    (tmp_path / 'uploads').mkdir()
    (tmp_path / 'outputs' / 'job' / 'chart.mcz').write_bytes(b'12345')
    (tmp_path / 'uploads' / 'track.ogg').write_bytes(b'123')
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    with TestClient(server.app) as client:
        result = client.get('/api/storage').json()
    assert result['outputs_bytes'] == 5
    assert result['uploads_bytes'] == 3
    assert result['total_bytes'] == 8
    assert result['free_bytes'] > 0


def test_delete_job_removes_only_finished_job_directory(monkeypatch, tmp_path):
    job_id = 'a' * 32
    output = tmp_path / 'outputs' / job_id
    output.mkdir(parents=True)
    (output / 'chart.mcz').write_bytes(b'chart')
    uploads = tmp_path / 'uploads'
    uploads.mkdir()
    source_name = job_id + '.mp3'
    source = uploads / source_name
    source.write_bytes(b'original upload')
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    monkeypatch.setattr(server, 'jobs', {job_id: {'id':job_id,'status':'completed','options':{'_source_upload':source_name}}})
    with TestClient(server.app) as client:
        assert client.delete('/api/jobs/' + job_id).status_code == 200
        assert not output.exists()
        assert not source.exists()
        assert client.delete('/api/jobs/../invalid').status_code == 404


def test_running_job_cannot_be_deleted(monkeypatch, tmp_path):
    job_id = 'b' * 32
    output = tmp_path / 'outputs' / job_id
    output.mkdir(parents=True)
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    monkeypatch.setattr(server, 'jobs', {job_id: {'id':job_id,'status':'running','options':{}}})
    with TestClient(server.app) as client:
        assert client.delete('/api/jobs/' + job_id).status_code == 409
        assert output.exists()


def test_regenerate_reuses_completed_package_audio(monkeypatch, tmp_path):
    from malody_studio import server as module
    job_id = 'c' * 32
    source = tmp_path / 'outputs' / job_id / '0' / 'audio.ogg'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'audio')
    options = {'title':'Song','artist':'Artist','engine':'mug','difficulties':['easy'],'_source_upload':job_id+'.mp3'}
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'jobs', {job_id: {'id':job_id,'status':'completed','title':'Song','artist':'Artist','options':options}})
    monkeypatch.setattr(module, 'ensure_engine', lambda opts: None)
    seen = []
    monkeypatch.setattr(module, 'new_job', lambda path, opts: seen.append((path, opts.copy())) or {'id':'new'})
    with TestClient(module.app) as client:
        assert client.post('/api/jobs/' + job_id + '/regenerate').json() == {'id':'new'}
    assert seen[0][0] == source
    assert seen[0][1]['_reuse_source_job_id'] == job_id
    assert '_source_upload' not in seen[0][1]
    assert options.keys() == {'title','artist','engine','difficulties','_source_upload'}



def test_failed_upload_job_can_retry_without_sharing_cleanup_owner(monkeypatch, tmp_path):
    job_id = 'd' * 32
    source_name = job_id + '.wav'
    source = tmp_path / 'uploads' / source_name
    source.parent.mkdir(parents=True)
    source.write_bytes(b'audio')
    options = {'title':'Song','artist':'Artist','engine':'mug','difficulties':['easy'], '_source_upload':source_name}
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    monkeypatch.setattr(server, 'jobs', {job_id: {'id':job_id,'status':'failed','title':'Song','artist':'Artist','options':options}})
    monkeypatch.setattr(server, 'ensure_engine', lambda opts: None)
    seen = []
    monkeypatch.setattr(server, 'new_job', lambda path, opts: seen.append((path, opts.copy())) or {'id':'retry'})
    with TestClient(server.app) as client:
        assert client.post('/api/jobs/' + job_id + '/regenerate').json() == {'id':'retry'}
    assert seen[0][0] == source
    assert seen[0][1]['_reuse_source_job_id'] == job_id
    assert '_source_upload' not in seen[0][1]
    assert options['_source_upload'] == source_name

def test_history_pagination_keeps_all_records_and_supports_search(monkeypatch, empty_advanced_library):
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


def test_dispatch_is_fifo_and_keeps_one_active_by_default(monkeypatch,tmp_path):
    class Pool:
        def __init__(self):self.calls=[]
        def submit(self,*args):self.calls.append(args)
    pool=Pool();monkeypatch.setattr(server,'pool',pool);monkeypatch.setattr(server,'jobs',{})
    monkeypatch.setattr(server,'scheduler_active',set())
    monkeypatch.setattr(server,'ROOT',tmp_path)
    monkeypatch.setattr(server,'configured_concurrency',lambda:1)
    for index in (2,1):
        job_id=f'{index:032x}';(tmp_path/'outputs'/job_id).mkdir(parents=True)
        server.jobs[job_id]={'id':job_id,'title':str(index),'artist':'a','status':'queued','queue_order':index,
            'options':{},'created':f'2026-01-0{index}'}
    server.dispatch_next()
    assert pool.calls[0][1]=='0'*31+'1'
    assert server.jobs['0'*31+'1']['status']=='running'
    assert server.jobs['0'*31+'2']['status']=='queued'


def test_queue_resume_and_waiting_cancellation(monkeypatch,tmp_path):
    job_id='e'*32;out=tmp_path/'outputs'/job_id;out.mkdir(parents=True)
    (tmp_path/'uploads').mkdir()
    source_name='f'*32+'.wav';(tmp_path/'uploads'/source_name).write_bytes(b'audio')
    monkeypatch.setattr(server,'ROOT',tmp_path)
    monkeypatch.setattr(server,'jobs',{job_id:{'id':job_id,'title':'Paused','artist':'A','status':'paused',
        'message':'restart','progress':0,'queue_order':4,'created':'now','options':{},
        'source_ref':{'type':'upload','name':source_name}}})
    class Pool:
        def __init__(self):self.calls=[]
        def submit(self,*args):self.calls.append(args)
    pool=Pool();monkeypatch.setattr(server,'pool',pool);monkeypatch.setattr(server,'scheduler_active',set())
    monkeypatch.setattr(server,'configured_concurrency',lambda:1)
    with TestClient(server.app) as client:
        response=client.post('/api/queue/resume')
        assert response.status_code==200 and response.json()['resumed']==[job_id]
        assert server.jobs[job_id]['status']=='running'
        server.jobs[job_id]['status']='queued'
        cancelled=client.delete('/api/jobs/'+job_id)
        assert cancelled.status_code==200 and cancelled.json()['cancelled']==job_id
    assert server.jobs[job_id]['status']=='cancelled'


def test_queue_failure_does_not_block_next_job(monkeypatch,tmp_path):
    ids=['1'*32,'2'*32]
    for item in ids:(tmp_path/'outputs'/item).mkdir(parents=True)
    monkeypatch.setattr(server,'ROOT',tmp_path)
    monkeypatch.setattr(server,'jobs',{
        ids[0]:{'id':ids[0],'status':'running','title':'bad','artist':'','queue_order':1,'options':{}},
        ids[1]:{'id':ids[1],'status':'queued','title':'next','artist':'','queue_order':2,'options':{}}})
    monkeypatch.setattr(server,'scheduler_active',{ids[0]})
    monkeypatch.setattr(server,'resolve_source_ref',lambda job:(_ for _ in ()).throw(ValueError('source missing')))
    class Pool:
        def __init__(self):self.calls=[]
        def submit(self,*args):self.calls.append(args)
    pool=Pool();monkeypatch.setattr(server,'pool',pool);monkeypatch.setattr(server,'configured_concurrency',lambda:1)
    server.run_queued_job(ids[0])
    assert server.jobs[ids[0]]['status']=='failed'
    assert server.jobs[ids[1]]['status']=='running'
    assert pool.calls[-1][1]==ids[1]


def test_failed_structural_job_keeps_audio_for_diagnostic_preview(monkeypatch,tmp_path):
    job_id='c'*32;audio=tmp_path/'outputs'/job_id/'0'/'audio.ogg';audio.parent.mkdir(parents=True)
    audio.write_bytes(b'OggSdiagnostic')
    monkeypatch.setattr(server,'ROOT',tmp_path)
    monkeypatch.setattr(server,'jobs',{job_id:{'id':job_id,'status':'failed','options':{}}})
    with TestClient(server.app) as client:
        response=client.get(f'/api/jobs/{job_id}/files/audio.ogg')
        assert response.status_code==200 and response.content==b'OggSdiagnostic'
        assert client.get(f'/api/jobs/{job_id}/files/easy.mc').status_code==409
from types import SimpleNamespace

def test_parallel_control_is_hidden_until_full_hardware_gate(monkeypatch,tmp_path):
    import torch
    from malody_studio import server
    monkeypatch.setattr(server,'ROOT',tmp_path)
    monkeypatch.setattr(torch.cuda,'is_available',lambda:True)
    monkeypatch.setattr(torch.cuda,'get_device_name',lambda index:'Test GPU')
    monkeypatch.setattr(server.subprocess,'run',lambda *a,**kw:SimpleNamespace(stdout='555.1'))
    singles={f'{engine}:{length}':{'success':True,'artifact_valid':True}
        for engine in ('mug','v32') for length in ('short','medium','long')}
    pairs={key:{'success':True,'artifacts_valid':True,'min_free_vram_bytes':3*1024**3}
        for key in ('mug+mug','mug+v32','v32+v32')}
    benchmark=tmp_path/'benchmarks'/'latest.json';benchmark.parent.mkdir(parents=True)
    import json
    for item in pairs.values():item['faster_than_serial']=True
    benchmark.write_text(json.dumps({'device_name':'Test GPU','driver_version':'555.1',
        'v32_inference_policy':server.V32_INFERENCE_POLICY,
        'single_tests':singles,'parallel_tests':pairs}),encoding='utf-8')
    assert server.parallel_benchmark_gate()[0] is True
    assert server.configured_concurrency()==1
    with TestClient(server.app) as client:
        assert client.get('/api/queue').json()['parallel_enabled'] is True
        assert client.post('/api/queue/concurrency',json={'value':2}).status_code==200
        assert client.get('/api/queue').json()['concurrency']==2
    pairs['mug+mug']['min_free_vram_bytes']=1024**3
    benchmark.write_text(json.dumps({'device_name':'Test GPU','driver_version':'555.1',
        'v32_inference_policy':server.V32_INFERENCE_POLICY,
        'single_tests':singles,'parallel_tests':pairs}),encoding='utf-8')
    assert server.parallel_benchmark_gate()[0] is False
    assert server.configured_concurrency()==1
    pairs['mug+mug']['min_free_vram_bytes']=3*1024**3
    pairs['v32+v32']['faster_than_serial']=False
    benchmark.write_text(json.dumps({'device_name':'Test GPU','driver_version':'555.1',
        'v32_inference_policy':server.V32_INFERENCE_POLICY,
        'single_tests':singles,'parallel_tests':pairs}),encoding='utf-8')
    ready,reason=server.parallel_benchmark_gate()
    assert not ready and '比串行更快' in reason

def test_parallel_gate_rejects_stale_inference_and_slow_pairs(monkeypatch,tmp_path):
    import json
    import torch
    from types import SimpleNamespace
    from malody_studio import server
    monkeypatch.setattr(server,'ROOT',tmp_path)
    monkeypatch.setattr(torch.cuda,'is_available',lambda:True)
    monkeypatch.setattr(torch.cuda,'get_device_name',lambda index:'Test GPU')
    monkeypatch.setattr(server.subprocess,'run',lambda *a,**kw:SimpleNamespace(stdout='555.1'))
    path=tmp_path/'benchmarks'/'latest.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'device_name':'Test GPU','driver_version':'555.1'}),encoding='utf-8')
    ready,reason=server.parallel_benchmark_gate()
    assert not ready and '推理策略已更新' in reason


def test_history_merges_advanced_products_with_stable_preview_and_filtered_counts(
        monkeypatch, empty_advanced_library):
    from malody_studio import advanced
    store = empty_advanced_library
    pid, aid = 'a' * 32, 'b' * 32
    project = {'id': pid, 'title': 'Advanced song', 'artist': 'Singer', 'background': True,
               'assemblies': [{'id': aid, 'created': '2026-10-04T12:00:00+08:00',
                               'duration': 3, 'mapping': [{'segment_id': 'segment'}]},
                              {'id': None}]}
    advanced.atomic(store.directory(pid) / 'project.json', project)
    folder = store.directory(pid) / 'assemblies' / aid
    advanced.atomic(folder / 'report.json', {'charts': [{'engine': 'v32', 'difficulty': 'hard'}]})
    (folder / 'audio.ogg').write_bytes(b'fixture-audio')
    (folder / '0').mkdir()
    (folder / '0' / 'balanced--hard.mc').write_text('{}', encoding='utf-8')
    # Corrupt historical records must not break the complete package list.
    advanced.atomic(store.directory('c' * 32) / 'project.json', [])
    bad = store.directory('d' * 32) / 'project.json'
    bad.parent.mkdir()
    bad.write_text('{', encoding='utf-8')
    monkeypatch.setattr(server, 'jobs', {'ordinary': {
        'id': 'ordinary', 'title': 'Alpha', 'artist': 'Band', 'created': '2026-10-02',
        'status': 'failed', 'options': {'engine': 'mug', 'difficulties': ['easy']}}})
    with TestClient(server.app) as client:
        result = client.get('/api/history?page_size=1').json()
        assert result['total'] == result['total_all'] == 2
        assert result['pages'] == 2
        item = result['items'][0]
        assert item['type'] == item['engine'] == 'advanced'
        assert (item['project_id'], item['assembly_id']) == (pid, aid)
        assert item['cover'] == f'/api/advanced/projects/{pid}/background'
        assert item['download'] == f'/api/advanced/projects/{pid}/assemblies/{aid}/download'
        assert item['difficulty_labels'] == ['Hard']
        assert client.get('/api/history?page=2&page_size=1').json()['items'][0]['id'] == 'ordinary'
        for query in ('engine=advanced', 'engine=v32', 'difficulty=hard'):
            data = client.get('/api/history?' + query).json()
            assert data['total'] == 1 and data['total_all'] == 2
            assert data['items'][0]['assembly_id'] == aid
        data = client.get('/api/history?q=Alpha').json()
        assert data['total'] == 1 and data['total_all'] == 2
        assert data['items'][0]['id'] == 'ordinary'
        assert client.get('/api/history?status=failed').json()['total'] == 1
