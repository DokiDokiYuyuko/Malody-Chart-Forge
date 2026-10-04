import copy

import pytest
from fastapi.testclient import TestClient

from malody_studio import advanced, advanced_api, server, task_history


@pytest.fixture
def history_context(tmp_path, monkeypatch):
    store = advanced.ProjectStore(tmp_path / 'outputs' / 'advanced')
    pid, bid = 'a' * 32, 'b' * 32
    settings = advanced.defaults()
    project = {'id': pid, 'title': 'D/N/A', 'artist': 'Singer', 'revision': 1,
               'duration': 7, 'samples': 7 * advanced.SR, 'settings': settings,
               'variants': [{'key': 'balanced--expert'}], 'segments': []}
    for index in range(7):
        project['segments'].append({'id': f'{index + 1:032x}', 'name': f'片段 {index + 1}',
                                    'start_sample': index * advanced.SR,
                                    'end_sample': (index + 1) * advanced.SR,
                                    'included': True, 'versions': {}, 'active': {}})
    advanced.atomic(store.directory(pid) / 'project.json', project)
    jobs = {}
    for index, segment in enumerate(project['segments']):
        jid = f'{index + 21:032x}'
        snapshot = {'project': {'id': pid, 'title': project['title'], 'artist': project['artist']},
                    'segment': copy.deepcopy(segment), 'variants': project['variants'],
                    'batch_id': bid, 'settings': settings, 'auto_fuse': True,
                    'input_sources': [{'source_role': role, 'source_id': 'stem:' + role}
                                      for role in ('vocals', 'accompaniment')]}
        revisions = []
        for role in ('vocals', 'accompaniment', 'fusion'):
            # Historical files deliberately contain misleading cache-derived batch IDs.
            revision = store.add_revision(pid, segment['id'], 'balanced--expert',
                [{'id': advanced.uid(), 'start_ms': index * 1000 + 500, 'end_ms': None, 'lane': 0}],
                settings, 'fusion' if role == 'fusion' else 'stem_raw',
                {'source_role': role, 'source_id': 'stem:' + role,
                 **({'cache_job': jid} if role != 'fusion' else {})}, activate_initial=False)
            revisions.append(revision['id'])
        jobs[jid] = {'id': jid, 'title': 'D/N/A · ' + segment['name'], 'artist': 'Singer',
                     'status': 'completed', 'created': f'2026-10-04T09:38:59.{index:06d}+00:00',
                     'options': {'_advanced': snapshot}, 'advanced_revisions': revisions, 'advanced_errors': []}
        (tmp_path / 'outputs' / jid).mkdir()
    advanced.atomic(store.directory(pid) / 'batches' / ('c' * 32 + '.json'),
                    {'id': bid, 'jobs': [{'id': jid} for jid in jobs]})
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    monkeypatch.setattr(server, 'jobs', jobs)
    monkeypatch.setattr(advanced_api, 'store', store)
    return store, pid, bid, jobs, TestClient(server.app)


def test_historical_twenty_one_candidates_resolve_by_jobs_without_rewriting(history_context):
    store, pid, bid, jobs, client = history_context
    paths = list((store.directory(pid) / 'revisions').glob('*.json'))
    before = {path.name: path.read_bytes() for path in paths}
    response = client.get(f'/api/advanced/projects/{pid}/generation-batches/{bid}')
    assert response.status_code == 200, response.text
    detail = response.json()
    assert len(detail['results']) == 21
    assert sum(row['primary'] for row in detail['results']) == 7
    assert all(row['adoptable'] and row['current_range'] for row in detail['results'])
    assert detail['counts']['completed'] == 7 and detail['status'] == 'completed'
    assert {row['job_id'] for row in detail['results']} == set(jobs)
    assert {path.name: path.read_bytes() for path in paths} == before
    record = client.get('/api/task-history').json()['items'][0]
    assert record['section_count'] == 7 and record['combination_count'] == 1
    assert record['source_label'] == '人声 + 伴奏 → 合并谱面'
    assert record['result_target']['batch_id'] == bid
    assert record['record_id'] == f'batch:{pid}:{bid}'
    assert client.get('/api/history').json()['total'] == 0


def test_missing_fusion_is_partial_even_when_task_is_completed(history_context):
    _, pid, bid, jobs, client = history_context
    first = next(iter(jobs.values()))
    first['advanced_revisions'] = first['advanced_revisions'][:2]
    detail = client.get(f'/api/advanced/projects/{pid}/generation-batches/{bid}').json()
    assert detail['status'] == 'partial'
    assert detail['counts']['completed'] == 6 and detail['counts']['partial'] == 1
    assert detail['jobs'][0]['raw_status'] == 'completed' and detail['jobs'][0]['status'] == 'partial'
    assert detail['jobs'][0]['primary_result_count'] == 0
    record = client.get('/api/task-history').json()['items'][0]
    assert record['success_count'] == 6 and record['partial_count'] == 1


def test_cached_components_link_to_submission_without_mutating_old_revision(history_context):
    store, pid, _, jobs, client = history_context
    original = next(iter(jobs.values()))
    jid, bid = 'd' * 32, 'e' * 32
    cached = original['advanced_revisions'][:2]
    immutable = {rid: (store.directory(pid) / 'revisions' / (rid + '.json')).read_bytes() for rid in cached}
    job = copy.deepcopy(original)
    job.update(id=jid, created='2026-10-04T10:00:00+00:00', advanced_reused_revisions=cached)
    job['options']['_advanced']['batch_id'] = bid
    result = {'bounds': [0, advanced.SR], 'advanced_result': [
        {'variant': 'balanced--expert', 'kind': 'fusion', 'events': [],
         'settings': store.load(pid)['settings'], 'provenance': {'source_role': 'fusion', 'cache_job': 'f' * 32}}]}
    saved = advanced_api.commit_generated(job['options'], result, job_id=jid)
    job['advanced_revisions'] = cached + saved
    jobs[jid] = job
    detail = client.get(f'/api/advanced/projects/{pid}/generation-batches/{bid}').json()
    assert {row['revision_id'] for row in detail['results']} == set(cached + saved)
    assert all(row['reused'] for row in detail['results'] if row['revision_id'] in cached)
    revision = store.revision(pid, saved[0])
    assert revision['provenance']['generation_batch_id'] == bid
    assert revision['provenance']['generation_job_id'] == jid
    assert revision['provenance']['cache_job'] == 'f' * 32
    assert {rid: (store.directory(pid) / 'revisions' / (rid + '.json')).read_bytes() for rid in cached} == immutable


def test_pagination_search_types_and_incomplete_batches(history_context):
    _, pid, _, jobs, client = history_context
    for index in range(12):
        jid = f'{index + 200:032x}'
        jobs[jid] = {'id': jid, 'title': f'Song {index}', 'artist': 'Band', 'status': 'completed',
                     'created': f'2026-10-{index + 5:02d}T10:00:00+00:00', 'options': {}}
    page = client.get('/api/task-history').json()
    assert page['total'] == 13 and len(page['items']) == 10 and page['pages'] == 2
    assert len(client.get('/api/task-history?page=2').json()['items']) == 3
    assert client.get('/api/task-history?type=song').json()['total'] == 12
    assert client.get('/api/task-history?type=advanced').json()['total'] == 1
    assert client.get('/api/task-history?q=D%2FN%2FA').json()['total'] == 1
    assert client.get('/api/task-history?project_id=' + pid).json()['total'] == 1
    assert client.get('/api/task-history?project_id=invalid').status_code == 400
    assert client.get('/api/task-history?type=invalid').status_code == 400
    assert client.get('/api/task-history?page_size=0').status_code == 422
    next(iter(jobs.values()))['status'] = 'running'
    assert client.get('/api/task-history?type=advanced').json()['total'] == 0


def test_latest_failed_retry_does_not_deliver_previous_success(history_context):
    _, pid, bid, jobs, client = history_context
    previous = next(iter(jobs.values()))
    retry = copy.deepcopy(previous)
    retry.update(id='f' * 32, created='2026-10-04T11:00:00+00:00', status='failed',
                 advanced_revisions=[], error='Retry failed')
    jobs[retry['id']] = retry
    detail = client.get(f'/api/advanced/projects/{pid}/generation-batches/{bid}').json()
    assert detail['status'] == 'partial' and detail['counts']['total'] == 7
    assert detail['counts']['completed'] == 6 and detail['counts']['failed'] == 1
    old = [row for row in detail['results'] if row['job_id'] == previous['id']]
    assert old and all(not row['primary'] and not row['current_attempt'] for row in old)
    assert next(row for row in detail['jobs'] if row['id'] == previous['id'])['superseded'] is True


def test_separation_filter_includes_trials_and_links_to_prepare(history_context):
    _, pid, _, jobs, client = history_context
    for index, kind in enumerate(('separation', 'separation_trial')):
        jid = f'{500 + index:032x}'
        jobs[jid] = {'id': jid, 'title': 'D/N/A 分离', 'status': 'completed',
                     'created': '2026-10-04T12:00:00+00:00', 'stem_set_id': 'a' * 64,
                     'options': {'_advanced': {'task_type': kind, 'project': {'id': pid, 'title': 'D/N/A'}}}}
    page = client.get('/api/task-history?type=separation').json()
    assert page['total'] == 2
    assert all(row['result_target']['mode'] == 'prepare' and row['result_target']['stem_set_id'] == 'a' * 64
               for row in page['items'])


def test_legacy_job_fallback_and_stale_range_are_explicit(history_context):
    store, pid, _, jobs, client = history_context
    first = next(iter(jobs.values()))
    del first['options']['_advanced']['batch_id']
    project = store.load(pid)
    project['segments'][0]['end_sample'] -= 100
    store.save(project)
    detail = client.get(f"/api/advanced/projects/{pid}/generation-batches/{first['id']}").json()
    assert len(detail['results']) == 3 and len(detail['jobs']) == 1
    assert all(not row['current_range'] and not row['adoptable'] for row in detail['results'])
    record = next(row for row in client.get('/api/task-history').json()['items'] if row['job_id'] == first['id'])
    assert record['batch_id'] == first['id'] and record['result_target']['job_id'] == first['id']


def test_open_folder_only_resolves_owned_output_ids(history_context, monkeypatch, tmp_path):
    store, pid, bid, jobs, client = history_context
    opened = []
    monkeypatch.setattr(task_history.os, 'startfile', opened.append, raising=False)
    record_id = f'batch:{pid}:{bid}'
    assert client.post('/api/task-history/open-folder', json={'record_id': record_id}).status_code == 200
    assert opened == [str(store.directory(pid).resolve())]
    jid = next(iter(jobs))
    assert client.post('/api/task-history/open-folder', json={'record_id': record_id, 'job_id': jid}).status_code == 200
    assert opened[-1] == str((tmp_path / 'outputs' / jid).resolve())
    assert client.post('/api/task-history/open-folder', json={'record_id': record_id, 'job_id': 'f' * 32}).status_code == 400
    assert client.post('/api/task-history/open-folder', json={'job_id': '../outside'}).status_code == 400
    assert client.post('/api/task-history/open-folder', json={'record_id': record_id, 'path': str(tmp_path.parent)}).status_code == 400
    monkeypatch.setattr(store, 'directory', lambda _: tmp_path.parent)
    assert client.post('/api/task-history/open-folder', json={'record_id': record_id}).status_code == 400
    assert len(opened) == 2
