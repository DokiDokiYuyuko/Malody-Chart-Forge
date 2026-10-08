"""Execute the real worker preset loop with CPU-only inference substitutes.

The heavy model/bootstrap code stays outside this test. The loop is compiled
from its source rather than copied so failed optional attempts exercise the
actual exception handling, successful chart retention, and sibling dispatch.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import mapperatorinator_worker as worker
from malody_studio.mapperatorinator import read_worker_charts


def actual_presets(request, args, destination, infer):
    from test_v32_batch_integration import worker_body
    args.parallel=False
    args.max_batch_size=32
    result=worker_body()(request,args,destination,SimpleNamespace(),
        lambda *a, **kw: infer(),
        lambda *_: (SimpleNamespace(),SimpleNamespace()),lambda *_: None)
    return {'results':result['charts'],'failures':result['errors'],
            'retry_records':result['retries']}



@pytest.mark.parametrize('failure_key', ['expert', 'expert__retry',
                                         'expert__density_retry1', 'expert__density_retry2'])
def test_failed_attempt_is_attributed_to_its_actual_output_and_valid_siblings_survive(tmp_path, failure_key):
    preset = {'key': 'expert', 'label': 'Expert', 'sr': 5.9, 'seed': 17,
              'start_time': 0, 'end_time': 1000}
    if failure_key == 'expert__retry':
        preset.update(retry_min_heads=3, retry_condition=10., retry_seed=18)
    else:
        preset.update(density_policy={'version': 'frozen-test'}, density_min_heads=3,
                      density_rounds=[{'condition': 10., 'seed': 18}, {'condition': 1., 'seed': 19}])
    sibling = {'key': 'master', 'label': 'Master', 'sr': 7.2, 'seed': 29}
    request = {'presets': [preset, sibling], 'seed': 17}
    args = SimpleNamespace(seed=17, difficulty=5.9, output_path=str(tmp_path),
                           in_context=[], beatmap_path='', train=SimpleNamespace())
    calls = []
    header = ('[General]\nMode:3\n[Difficulty]\nCircleSize:4\n'
              '[TimingPoints]\n0,500,4,2,1,100,1,0\n[HitObjects]\n')

    def infer(*_):
        folder = Path(args.output_path)
        calls.append(folder.name)
        if folder.name == failure_key:
            raise ValueError('native note has no time/column')
        chart = folder / 'returned.osu'
        chart.write_text(header + '64,192,100,1,0,0:0:0:0:\n', encoding='utf-8')
        return None, chart

    def hit_times(path):
        return [int(row.split(',')[2]) for row in Path(path).read_text().partition('[HitObjects]')[2].splitlines() if row]

    scope = actual_presets(request,args,tmp_path,infer)
    assert calls[-1] == 'master'
    assert set(scope['failures']) == {failure_key}
    charts, meta = read_worker_charts({'charts': scope['results'], 'errors': scope['failures']})
    assert set(meta['rejected_charts']) == {failure_key}
    assert 'master' in charts
    if failure_key != 'expert':
        assert 'expert' in charts and 'expert' not in meta['rejected_charts']
        assert scope['retry_records'][-1]['status'] == 'failed'
        if failure_key == 'expert__density_retry2':
            assert 'expert__density_retry1' in charts


def test_sustain_only_native_stream_rejects_one_preset_and_the_batch_continues(tmp_path):
    """The real serializer turns upstream's IndexError into a ValueError the worker loop isolates."""
    from malody_studio.v32_event_serialization import pack_mania_events
    from tests.test_v32_terminal_fragments import Event, Kind, Sustain, upstream_reader
    presets = [{'key': key, 'label': key, 'sr': 7.0, 'seed': 5 + i, 'start_time': 0, 'end_time': 1000}
               for i, key in enumerate(('first', 'orphan', 'last'))]
    request = {'presets': presets, 'seed': 17}
    args = SimpleNamespace(seed=17, difficulty=5.9, output_path=str(tmp_path), in_context=[], beatmap_path='',
                           train=SimpleNamespace())
    calls = []
    header = ('[General]\nMode:3\n[Difficulty]\nCircleSize:4\n[TimingPoints]\n0,500,4,2,1,100,1,0\n[HitObjects]\n')

    def infer(*_):
        folder = Path(args.output_path)
        calls.append(folder.name)
        if folder.name == 'orphan':
            pack_mania_events([Event(Kind.TIME_SHIFT, 1), Event(Kind.POS_X, 64), Event(Sustain.SUSTAIN, 0)],
                              group_reader=upstream_reader, event_factory=Event, event_types=Kind)
        chart = folder / 'returned.osu'
        chart.write_text(header + '64,192,100,1,0,0:0:0:0:\n', encoding='utf-8')
        return None, chart

    scope = actual_presets(request,args,tmp_path,infer)
    assert calls == ['first', 'orphan', 'last']
    assert set(scope['failures']) == {'orphan'} and '没有任何音符' in scope['failures']['orphan']
    assert set(scope['results']) == {'first', 'last'}
