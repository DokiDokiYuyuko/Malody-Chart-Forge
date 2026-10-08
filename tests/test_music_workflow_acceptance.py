"""Independent transaction checks using the actual batch/queue implementation."""
import copy
import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from malody_studio import advanced, advanced_api, advanced_plans, music_timing, server


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    wave = np.full((advanced.SR * 4, 2), .1, dtype='float32')
    source = tmp_path / 'music.wav'
    sf.write(source, wave, advanced.SR, subtype='FLOAT')
    monkeypatch.setattr(advanced, 'audio_metadata', lambda _: ([], {'bpm': 120, 'points': [[0, 120]], 'uncertain': True}))
    store = advanced.ProjectStore(tmp_path / 'outputs/advanced')
    project = store.create(source, 'Transaction fixture')
    monkeypatch.setattr(advanced_api, 'store', store)
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    (tmp_path / 'outputs').mkdir(exist_ok=True)
    monkeypatch.setattr(server, 'jobs', {})
    monkeypatch.setattr(server, 'dispatch_next', lambda: None)
    monkeypatch.setattr(server, 'ensure_engine', lambda _: None)
    _, clock = music_timing.source_contract(store.directory(project['id']), project)
    timing = music_timing.timing_map({'beat_samples': [], 'downbeat_samples': []}, clock)
    evidence = music_timing.sealed({'schema': music_timing.SCHEMA, 'source': clock, 'policy': {}, 'candidates': [timing], 'selected_timing_id':timing['id']})
    advanced.atomic(store.directory(project['id']) / 'music-evidence' / (evidence['id'] + '.json'), evidence)
    advanced.atomic(store.directory(project['id']) / 'timing-maps' / (timing['id'] + '.json'), timing)
    plan = {'id': 'a' * 64, 'samples': project['samples'], 'source_pcm_sha': project['source_pcm_sha256'],
            'sections': [{'id': 'first', 'core': [0, advanced.SR * 2], 'rhythm_activity': -.5},
                         {'id': 'second', 'core': [advanced.SR * 2, project['samples']], 'rhythm_activity': .5}]}
    advanced.atomic(store.directory(project['id']) / 'section-plans' / (plan['id'] + '.json'), plan)
    monkeypatch.setattr(advanced_plans, 'get_or_build_plan', lambda *args: copy.deepcopy(plan))
    app = FastAPI()
    app.include_router(advanced_api.router)
    with TestClient(app) as client:
        url = '/api/advanced/projects/' + project['id']
        result = client.post(url + '/segmentation-preview', json={
            'expected_revision': 0, 'evidence_id': evidence['id'], 'timing_map_id': timing['id'],
            'source_id': 'original', 'patterns': ['balanced'], 'difficulties': ['hard'], 'cuts':[advanced.SR*2]})
        assert result.status_code == 200, result.text
        draft = result.json()
        payload = {'request_id': 'c' * 32, 'expected_revision': 0, 'draft_id': draft['id'],
                   'arrangement_plan_id': draft['arrangement_plan_id']}
        yield store, project, client, url, draft, payload


@pytest.mark.parametrize('folder,field', [('music-evidence', 'evidence_id'),
                                         ('timing-maps', 'timing_map_id'),
                                         ('arrangement-plans', 'arrangement_plan_id')])
def test_artifact_tamper_is_rejected_before_project_or_queue_mutation(workflow, folder, field):
    store, project, client, url, draft, payload = workflow
    path = store.directory(project['id']) / folder / (draft[field] + '.json')
    value = advanced.read(path)
    value['unexpected_modified_payload'] = True
    advanced.atomic(path, value)
    result = client.post(url + '/confirm-and-generate', json=payload)
    assert result.status_code in (400, 409), result.text
    assert store.load(project['id']) == project
    assert server.jobs == {}


def test_queue_full_rejects_confirmation_before_project_mutation(workflow):
    store, project, client, url, draft, payload = workflow
    server.jobs.update({str(i): {'status': 'queued'} for i in range(100)})
    result = client.post(url + '/confirm-and-generate', json=payload)
    assert result.status_code == 429, result.text
    assert store.load(project['id']) == project
    assert len(server.jobs) == 100


def test_actual_queue_freezes_evidence_and_repeated_confirm_does_not_enqueue_again(workflow):
    store, project, client, url, draft, payload = workflow
    first = client.post(url + '/confirm-and-generate', json=payload)
    assert first.status_code == 200, first.text
    count = len(server.jobs)
    assert count == len(first.json()['batch']['jobs']) == 1
    assert len(first.json()['batch']['jobs'][0]['segment_ids'])==2
    for job in server.jobs.values():
        snapshot = job['options']['_advanced']
        assert snapshot['evidence']['id'] == draft['evidence_id']
        assert snapshot['timing_map']['id'] == draft['timing_map_id']
        assert snapshot['arrangement_plan']['id'] == draft['arrangement_plan_id']
        assert snapshot['initial_plan_id'] == draft['arrangement_plan_id']
    second = client.post(url + '/confirm-and-generate', json=payload)
    assert second.status_code == 200 and second.json() == first.json()
    assert len(server.jobs) == count


def test_crash_after_queue_acceptance_recovers_without_duplicate_jobs(workflow, monkeypatch):
    store, project, client, url, draft, payload = workflow
    real_atomic = advanced_api.atomic
    failed = []
    def fail_once(path, value):
        if path.parent.name == 'batches' and not failed:
            failed.append(True)
            raise OSError('Injected batch manifest write failure')
        return real_atomic(path, value)
    monkeypatch.setattr(advanced_api, 'atomic', fail_once)
    first = client.post(url + '/confirm-and-generate', json=payload)
    assert first.status_code == 400
    accepted = set(server.jobs)
    assert len(accepted) == 1
    retry = client.post(url + '/confirm-and-generate', json=payload)
    assert retry.status_code == 200, retry.text
    assert set(server.jobs) == accepted


def test_initial_raw_is_not_adopted_and_late_user_choice_is_kept(workflow):
    store, project, client, url, draft, payload = workflow
    assert client.post(url + '/confirm-and-generate', json=payload).status_code == 200
    job = next(iter(server.jobs.values()))
    snap = job['options']['_advanced']
    sid = snap['segment']['id']
    variant = snap['variants'][0]['key']
    raw = {'id': 'd' * 32, 'variant': variant, 'events': [{'id': 'n', 'start_ms': 100, 'end_ms': None, 'lane': 0}],
           'settings': snap['settings'], 'kind': 'model_raw', 'provenance': {}, 'activate_initial': False}
    bounds = [snap['segment']['start_sample'], snap['segment']['end_sample']]
    advanced_api.commit_generated(job['options'], {'advanced_result': [raw], 'bounds': bounds})
    assert not store.segment(store.load(project['id']), sid)['active']
    rules = {**raw, 'id': 'e' * 32, 'kind': 'arranged', 'activate_initial': True}
    saved=advanced_api.commit_generated(job['options'], {'advanced_result': [rules], 'bounds': bounds})
    adopted=next(rid for rid in saved if store.revision(project['id'],rid)['segment_id']==sid)
    assert store.segment(store.load(project['id']), sid)['active'][variant] == adopted
    newer = {**rules, 'id': 'f' * 32}
    advanced_api.commit_generated(job['options'], {'advanced_result': [newer], 'bounds': bounds})
    assert store.segment(store.load(project['id']), sid)['active'][variant] == adopted
