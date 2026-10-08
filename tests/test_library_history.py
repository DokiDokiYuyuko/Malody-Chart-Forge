import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from malody_studio.library_history import SummaryCache


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')


def test_summary_discards_diagnostics_and_survives_restart(tmp_path):
    report = tmp_path / 'report.json'
    write(report, {'title': 'A', 'charts': [{'engine': 'v32', 'difficulty': 'master', 'notes': [1] * 10000}],
                   'events': [1] * 10000})
    checkpoint = tmp_path / 'cache.json'
    cache = SummaryCache(checkpoint)
    reads = []
    def reader(path):
        reads.append(path)
        return json.loads(path.read_text('utf-8'))
    expected = {'title': 'A', 'charts': [{'engine': 'v32', 'difficulty': 'master'}]}
    assert cache.get(report, 'report', reader) == expected
    cache.flush()
    assert checkpoint.stat().st_size < 1000
    restarted = SummaryCache(checkpoint)
    assert restarted.get(report, 'report', reader) == expected
    assert len(reads) == 1
    # Same-size replacement must invalidate, including cached title/filters.
    write(report, {'title': 'B', 'charts': [{'engine': 'mug', 'difficulty': 'expert'}]})
    assert restarted.get(report, 'report', reader)['title'] == 'B'
    report.unlink()
    with pytest.raises(FileNotFoundError):
        restarted.get(report, 'report', reader)


def test_concurrent_pages_read_each_version_once_and_results_are_isolated(tmp_path):
    path = tmp_path / 'report.json'
    write(path, {'charts': [{'difficulty': 'hard'}]})
    cache = SummaryCache(tmp_path / 'cache.json')
    reads = []
    def reader(path):
        reads.append(path)
        return json.loads(path.read_text('utf-8'))
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: cache.get(path, 'report', reader), range(8)))
    assert len(reads) == 1
    rows[0]['charts'][0]['difficulty'] = 'easy'
    assert cache.get(path, 'report', reader)['charts'][0]['difficulty'] == 'hard'


def test_corrupt_checkpoint_and_read_only_checkpoint_do_not_break_listing(tmp_path, monkeypatch):
    checkpoint = tmp_path / 'cache.json'
    checkpoint.write_text('{', encoding='utf-8')
    report = tmp_path / 'report.json'
    write(report, {'charts': []})
    cache = SummaryCache(checkpoint)
    assert cache.get(report, 'report', lambda p: json.loads(p.read_text('utf-8'))) == {'charts': []}
    from pathlib import Path
    original = Path.write_text
    def denied(path, *args, **kwargs):
        if path.name.endswith('.tmp'):
            raise PermissionError('read-only cache')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', denied)
    cache.flush()
    assert cache.get(report, 'report', lambda p: pytest.fail('unnecessary reread')) == {'charts': []}


def test_replacement_during_read_cannot_poison_file_signature(tmp_path):
    path = tmp_path / 'report.json'
    write(path, {'title': 'old', 'charts': []})
    calls = []
    def reader(path):
        value = json.loads(path.read_text('utf-8'))
        if not calls:
            write(path, {'title': 'replacement', 'charts': []})
        calls.append(1)
        return value
    cache = SummaryCache(tmp_path / 'cache.json')
    assert cache.get(path, 'report', reader)['title'] == 'replacement'
    assert len(calls) == 2
    assert cache.get(path, 'report', lambda p: pytest.fail('unnecessary reread'))['title'] == 'replacement'


def test_history_cache_detects_new_exports_updates_deletion_and_missing_media(tmp_path, monkeypatch):
    from malody_studio import server, advanced, advanced_api, library
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    monkeypatch.setattr(server, 'jobs', {})
    store = advanced.ProjectStore(tmp_path / 'advanced')
    monkeypatch.setattr(advanced_api, 'store', store)
    pid, aid, second = 'a' * 32, 'b' * 32, 'c' * 32
    project = {'id': pid, 'title': 'Song', 'artist': 'Singer', 'background': True,
               'assemblies': [{'id': aid, 'created': '2026-10-07', 'mapping': [{}]}]}
    def export(identifier, difficulty='master'):
        folder = store.directory(pid) / 'assemblies' / identifier
        write(folder / 'report.json', {'title': 'Song', 'charts': [{'engine': 'v32', 'difficulty': difficulty}],
                                     'diagnostics': [0] * 10000})
        (folder / 'audio.ogg').write_bytes(b'fixture')
        (folder / '0').mkdir(exist_ok=True)
        (folder / '0' / 'speed.mc').write_text('{}')
        return folder
    export(aid)
    write(store.directory(pid) / 'project.json', project)
    original_read = advanced.read
    reads = []
    def reader(path):
        reads.append(str(path))
        return original_read(path)
    monkeypatch.setattr(advanced, 'read', reader)
    with TestClient(server.app) as client:
        first = client.get('/api/history?page_size=1').json()
        assert first['total'] == 1 and first['items'][0]['segment_count'] == 1
        assert client.get('/api/history?page=2&page_size=1').json()['page'] == 1
        assert len(reads) == 2, 'ordinary page navigation must not reparse project/report JSON'
        # Report changes immediately affect search and engine/difficulty filters.
        write(store.directory(pid) / 'assemblies' / aid / 'report.json',
              {'title': 'Renamed', 'charts': [{'engine': 'mug', 'difficulty': 'expert'}]})
        changed = client.get('/api/history?q=Renamed&engine=mug&difficulty=expert').json()
        assert changed['total'] == 1 and changed['items'][0]['title'] == 'Renamed'
        assert len(reads) == 3
        export(second)
        project['assemblies'].append({'id': second, 'created': '2026-10-08', 'mapping': [{}, {}]})
        write(store.directory(pid) / 'project.json', project)
        new = client.get('/api/history?page_size=1').json()
        assert new['total'] == 2 and new['items'][0]['assembly_id'] == second
        # Frozen published title and tombstones remain authoritative on cache hits.
        write(library.home(tmp_path) / 'index.json', {'advanced-' + second: {'title': 'Published'}})
        assert client.get('/api/history?q=Published').json()['total'] == 1
        write(library.home(tmp_path) / 'deleted.json', {'advanced-' + second: {}})
        assert client.get('/api/history').json()['total'] == 1
        (store.directory(pid) / 'assemblies' / aid / 'audio.ogg').unlink()
        assert client.get('/api/history').json()['total'] == 0
