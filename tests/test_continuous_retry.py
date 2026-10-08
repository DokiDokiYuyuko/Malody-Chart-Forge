import copy

import numpy as np
import pytest
import soundfile as sf
from fastapi import HTTPException

from malody_studio import advanced, advanced_api, server
from malody_studio.generation_context import contract
from malody_studio.separation import resolve_source


@pytest.fixture
def retry_case(tmp_path, monkeypatch):
    source = tmp_path / 'source.wav'
    samples = advanced.SR * 17
    audio = np.full((samples, 2), 0.1, dtype=np.float32)
    sf.write(source, audio, advanced.SR, subtype='FLOAT')

    def decode(source_path, output_path):
        data, rate = sf.read(source_path, dtype='float32', always_2d=True)
        sf.write(output_path, data, rate, subtype='FLOAT')
        return data

    monkeypatch.setattr(advanced, 'decode', decode)
    monkeypatch.setattr(advanced, 'audio_metadata', lambda _: ([], {'bpm': 120, 'uncertain': True}))
    store = advanced.ProjectStore(tmp_path / 'projects')
    project = store.create(source, 'Continuous retry fixture')
    for index in range(17):
        project = store.add_segment(project['id'], {
            'start_sample': index * advanced.SR,
            'end_sample': (index + 1) * advanced.SR,
        })

    members = copy.deepcopy(project['segments'])
    snapshot = {
        'project': {key: project[key] for key in (
            'id', 'title', 'artist', 'duration', 'samples', 'tempo',
            'source_sha256', 'revision', 'tail_trim', 'source_pcm_sha256',
        )},
        'segment': {
            **copy.deepcopy(members[0]),
            'start_sample': members[0]['start_sample'],
            'end_sample': members[-1]['end_sample'],
            'name': 'Continuous span · 17 regions',
        },
        'member_segments': members,
        'input_sources': [resolve_source(store, project['id'], 'original')],
        'generation_context_policy': contract(),
        'settings': copy.deepcopy(project['settings']),
        'variants': copy.deepcopy(project['variants']),
    }
    original = {
        'id': 'a' * 32,
        'status': 'failed',
        'options': {'title': 'Continuous retry fixture', 'engine': 'v32', '_advanced': snapshot},
    }

    monkeypatch.setattr(advanced_api, 'store', store)
    monkeypatch.setattr(server, 'jobs', {})
    monkeypatch.setattr(server, 'ensure_engine', lambda _: None)
    queued = []
    monkeypatch.setattr(server, 'enqueue_job', lambda options, source_ref: (
        queued.append((copy.deepcopy(options), copy.deepcopy(source_ref))) or {'id': 'b' * 32}
    ))
    return store, project, original, queued


def test_retry_accepts_unchanged_continuous_snapshot_and_preserves_it(retry_case):
    store, project, original, queued = retry_case
    frozen_options = copy.deepcopy(original['options'])
    members = copy.deepcopy(original['options']['_advanced']['member_segments'])

    result = server.retry_advanced_job(original)

    assert result == {'id': 'b' * 32}
    assert original['options'] == frozen_options
    retried, source_ref = queued[0]
    retried_snapshot = retried['_advanced']
    assert retried_snapshot['member_segments'] == members
    assert retried_snapshot['generation_context_policy'] == contract()
    assert retried_snapshot['retry_of'] == original['id']
    assert retried_snapshot['segment']['start_sample'] == members[0]['start_sample']
    assert retried_snapshot['segment']['end_sample'] == members[-1]['end_sample']
    assert source_ref == {
        'type': 'project_file',
        'path': f"outputs/advanced/{project['id']}/source.wav",
    }
    assert len(store.load(project['id'])['segments']) == 17
    server.jobs['d' * 32] = {'id': 'd' * 32, 'status': 'running', 'options': retried}
    assert server.retry_advanced_job(original) == {'id': 'd' * 32, 'reused': True}
    assert len(queued) == 1


def test_retry_keeps_single_segment_behavior(retry_case):
    store, project, original, queued = retry_case
    first = copy.deepcopy(project['segments'][0])
    snapshot = original['options']['_advanced']
    snapshot.pop('member_segments')
    snapshot.pop('generation_context_policy')
    snapshot['segment'] = first
    original['id'] = 'c' * 32

    result = server.retry_advanced_job(original)

    assert result == {'id': 'b' * 32}
    assert queued[0][0]['_advanced']['segment'] == first
    assert queued[0][0]['_advanced']['retry_of'] == original['id']


def test_batched_retry_gets_independent_submission_and_revision_identity(retry_case):
    store, project, original, queued = retry_case
    parent = {'batch_id': '1' * 32, 'request_id': '2' * 32,
              'batch_request_hash': '3' * 64, 'initial_plan_id': 'original-plan'}
    original['options']['_advanced'].update(**parent, activate_initial=True)
    frozen = copy.deepcopy(original)
    selections = [copy.deepcopy(part.get('selected_revisions', {}))
                  for part in store.load(project['id'])['segments']]

    server.retry_advanced_job(original)
    options = queued[0][0]
    child = options['_advanced']
    assert child['batch_id'] != parent['batch_id']
    assert child['request_id'] != parent['request_id']
    assert len(child['batch_id']) == len(child['request_id']) == 32
    assert 'batch_request_hash' not in child
    assert 'initial_plan_id' not in child
    assert child['retry_parent_submission'] == parent
    assert child['activate_initial'] is False
    assert original == frozen
    assert child['settings'] == frozen['options']['_advanced']['settings']
    assert child['member_segments'] == frozen['options']['_advanced']['member_segments']

    generated = {'bounds': [0, project['samples']], 'advanced_result': [{
        'variant': project['variants'][0]['key'], 'kind': 'rules',
        'settings': child['settings'], 'activate_initial': True,
        'provenance': {'cache_job': original['id']},
        'events': [{'id': 'synthetic-head', 'start_ms': 100, 'lane': 0, 'end_ms': None}],
    }]}
    saved = advanced_api.commit_generated(options, generated, job_id='b' * 32)
    assert saved
    for rid in saved:
        provenance = store.revision(project['id'], rid)['provenance']
        assert provenance['generation_batch_id'] == child['batch_id']
        assert provenance['generation_job_id'] == 'b' * 32
        assert provenance['cache_job'] == original['id']
    assert [part.get('selected_revisions', {}) for part in store.load(project['id'])['segments']] == selections


@pytest.mark.parametrize('change', [
    'range', 'deleted', 'excluded', 'coverage_gap', 'missing_policy', 'empty_policy',
    'bad_policy', 'empty_members', 'malformed_member', 'source', 'selected_source',
])
def test_retry_rejects_stale_or_ambiguous_continuous_snapshots(retry_case, monkeypatch, change):
    store, project, original, queued = retry_case
    snapshot = original['options']['_advanced']
    current = copy.deepcopy(project)

    if change == 'range':
        current['segments'][6]['end_sample'] -= 1
    elif change == 'deleted':
        current['segments'].pop(6)
    elif change == 'excluded':
        current['segments'][6]['included'] = False
    elif change == 'coverage_gap':
        snapshot['member_segments'][6]['start_sample'] += 1
    elif change == 'missing_policy':
        snapshot.pop('generation_context_policy')
    elif change == 'empty_policy':
        snapshot['generation_context_policy'] = {}
    elif change == 'bad_policy':
        snapshot['generation_context_policy'] = {'version': 'unsupported'}
    elif change == 'empty_members':
        snapshot['member_segments'] = []
    elif change == 'malformed_member':
        snapshot['member_segments'][6] = None
    elif change == 'source':
        current['source_pcm_sha256'] = 'changed-source'
    elif change == 'selected_source':
        snapshot['input_sources'][0]['pcm_sha'] = 'changed-selected-source'

    monkeypatch.setattr(store, 'load', lambda _: current)
    with pytest.raises(HTTPException) as exc:
        server.retry_advanced_job(original)

    assert exc.value.status_code == 409
    assert not queued
