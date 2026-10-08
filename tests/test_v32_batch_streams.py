import contextvars
import threading
import time
from types import SimpleNamespace

import pytest

from malody_studio import v32_batch_decode as bd
from malody_studio import v32_batch_streams as bs


def test_clamp_streams_and_graph_batch():
    assert [bs.clamp_streams(v) for v in (None, 'x', True, 0, 1, 2, 8, 99, 3.0)] == [0, 0, 0, 0, 0, 2, 8, 16, 3]
    assert [bs.graph_batch(n) for n in (1, 2, 3, 4, 5, 8, 9, 16)] == [1, 2, 4, 4, 8, 8, 16, 16]


def test_plan_groups_sorts_by_span_then_density_and_balances():
    entries = [(0, 70, 5.5), (1, 12, 6.5), (2, 20, 5.5), (3, 65, 7.5), (4, 40, 5.5), (5, 12, 5.5), (6, 33, 7.0),
               (7, 60, 5.5), (8, 31, 5.5)]
    groups = bs.plan_groups(entries, 4)
    assert [len(g) for g in groups] == [3, 3, 3]
    assert groups[0] == [5, 1, 2] and groups[1] == [8, 6, 4] and groups[2] == [7, 3, 0]
    assert bs.plan_groups(entries, 16) == [[5, 1, 2, 8, 6, 4, 7, 3, 0]]
    assert bs.plan_groups(entries[:1], 8) == [[0]] and bs.plan_groups([], 8) == []
    assert all(len(g) <= 2 for g in bs.plan_groups(entries, 2))


class Trace:
    """Detects overlapping execution of stream threads and records decode rounds."""

    def __init__(self):
        self.running = 0
        self.max_running = 0
        self.rounds = []
        self.lock = threading.Lock()

    def enter(self):
        with self.lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)

    def leave(self):
        with self.lock:
            self.running -= 1


def make_body(trace, windows_of, log):
    def body(stream):
        trace.enter()
        try:
            total = windows_of[stream.index]
            seen = []
            for k in range(total):
                time.sleep(0.001)
                trace.leave()
                try:
                    seen.append(stream.submit((stream.index, k)))
                finally:
                    trace.enter()
            log[stream.index] = seen
            return total
        finally:
            trace.leave()
    return body


def fake_decode(trace):
    def decode(requests, batch=None):
        assert trace.running == 0   # every stream is blocked while the scheduler decodes
        trace.rounds.append(sorted(r.window for r in requests))
        for request in requests:
            request.result = ('tok',) + request.window
    return decode


def test_streams_run_one_at_a_time_and_rounds_hold_exactly_the_live_streams():
    trace, log = Trace(), {}
    windows_of = {0: 3, 1: 1, 2: 4, 3: 2}
    out = bs.run_lockstep([(i, f'k{i}', 1, 1) for i in windows_of], 8, body=make_body(trace, windows_of, log),
                          decode=fake_decode(trace))
    assert trace.max_running == 1
    assert out['outcomes'] == windows_of and out['sequential'] == [] and out['fallbacks'] == []
    assert out['groups'] == [{'limit': 8, 'groups': [['k0', 'k1', 'k2', 'k3']]}]
    # Round r holds exactly the streams that still have a window r (deterministic, no timing dependence).
    assert [[w[0] for w in rnd] for rnd in trace.rounds] == [[0, 1, 2, 3], [0, 2, 3], [0, 2], [2]]
    assert log[2] == [('tok', 2, k) for k in range(4)]


def test_decode_failure_reaches_every_waiting_stream_and_nothing_hangs():
    trace, log = Trace(), {}

    def decode(requests, batch):
        raise RuntimeError('boom')
    out = bs.run_lockstep([(0, 'a', 1, 1), (1, 'b', 1, 1)], 4, body=make_body(trace, {0: 2, 1: 2}, log), decode=decode)
    assert out['outcomes'] == {} and out['sequential'] == [0, 1]
    assert [f['error'] for f in out['fallbacks']] == ['boom', 'boom'] and not any(f['cuda_oom'] for f in out['fallbacks'])


def test_one_failing_stream_leaves_the_others_untouched():
    trace, log = Trace(), {}
    inner = make_body(trace, {0: 3, 1: 3, 2: 3}, log)

    def body(stream):
        if stream.index == 1:
            stream.submit((1, 0))
            raise ValueError('preset failed')
        return inner(stream)
    out = bs.run_lockstep([(i, f'k{i}', 1, 1) for i in range(3)], 4, body=body, decode=fake_decode(trace))
    assert sorted(out['outcomes']) == [0, 2] and out['sequential'] == [1]
    assert log[0] == [('tok', 0, k) for k in range(3)] and log[2] == [('tok', 2, k) for k in range(3)]


def test_oom_halves_the_group_limit_and_retries_then_goes_sequential():
    trace, log = Trace(), {}
    limits = []

    def decode(requests, batch):
        limits.append((len(requests), batch))
        if len(requests) > 2:
            raise RuntimeError('CUDA out of memory. Tried to allocate 2.00 GiB')
        for request in requests:
            request.result = ('tok',) + request.window
    entries = [(i, f'k{i}', i, 1) for i in range(4)]
    out = bs.run_lockstep(entries, 8, body=make_body(trace, {i: 2 for i in range(4)}, log), decode=decode)
    assert [g['limit'] for g in out['groups']] == [8, 4, 2]
    assert sorted(out['outcomes']) == [0, 1, 2, 3] and out['sequential'] == []
    assert all(f['cuda_oom'] for f in out['fallbacks'])
    assert limits[0] == (4, 4) and limits[-1][0] <= 2
    # Failing at every size ends in the sequential list, never in a lost preset.
    out = bs.run_lockstep(entries[:2], 2, body=make_body(trace, {0: 1, 1: 1}, {}),
                          decode=lambda requests, batch: (_ for _ in ()).throw(RuntimeError('out of memory')))
    assert out['outcomes'] == {} and out['sequential'] == [0, 1]


def test_stream_threads_inherit_a_copy_of_the_callers_context():
    var = contextvars.ContextVar('trace_stage', default='none')
    var.set('request')
    seen = {}

    def body(stream):
        seen[stream.index] = var.get()
        var.set(f'stream-{stream.index}')   # must not leak to siblings or to the caller
        stream.submit((stream.index, 0))
        return var.get()
    out = bs.run_lockstep([(0, 'a', 1, 1), (1, 'b', 1, 1)], 4, body=body, decode=fake_decode(Trace()),
                          context_factory=contextvars.copy_context)
    assert seen == {0: 'request', 1: 'request'} and var.get() == 'request'
    assert out['outcomes'] == {0: 'stream-0', 1: 'stream-1'}


def test_processor_router_resolves_per_thread():
    class Base:
        marker = 'base'

        def __init__(self, value):
            self.value = value
    router = bs.ProcessorRouter(Base)
    traced = type('Traced', (Base,), {'marker': 'traced'})
    assert router(1).value == 1 and router.marker == 'base' and type(router(1)) is Base
    previous = router.push(traced)
    assert type(router(2)) is traced and router.marker == 'traced'
    seen = []
    thread = threading.Thread(target=lambda: seen.append(type(router(3))))
    thread.start()
    thread.join()
    assert seen == [Base]                      # another thread still builds the base class
    router.pop(previous)
    assert type(router(4)) is Base


def test_current_stream_is_none_outside_stream_threads():
    assert bs.current_stream() is None
    seen = []

    def body(stream):
        seen.append(bs.current_stream() is stream)
    bs.run_lockstep([(0, 'a', 1, 1)], 2, body=body, decode=fake_decode(Trace()))
    assert seen == [True] and bs.current_stream() is None


# --- pure-host parts of the batched decoder -------------------------------------------

def window(n, hard_cap=2560, bucket=512, model_max=2560):
    return SimpleNamespace(n=n, prompt=[1] * n, ts_start=10, ts_end=20, sos_ids=[1], hard_cap=hard_cap,
                           bucket=bucket, model_max=model_max)


def test_row_caps_and_floor_follow_the_lean_loop():
    row = bd.Row(window(5, hard_cap=700, bucket=512), 512)
    assert (row.win_max, row.cur_len, row.pad, row.truncated, row.done) == (512, 5, 0, True, False)
    assert bd.Row(window(5, hard_cap=300, bucket=512), 512).win_max == 300
    assert row.floor.bound() == 10       # prompt holds only start tokens: no time-shift floor yet


def test_oversized_pad_rows_run_alone_and_truncated_windows_retry_once(monkeypatch):
    engine = bd.BatchEngine()
    passes = []

    def run_pass(rows, batch):
        passes.append(([r.n for r in rows], batch))
        for row in rows:
            row.generated = [row.n]
            row.truncated = row.bucket < row.window.hard_cap and row.n == 100
    monkeypatch.setattr(engine, '_run_pass', run_pass)
    monkeypatch.setattr(bd.BatchEngine, '_bucket_ceil', staticmethod(lambda need, model_max: model_max))
    # n=100 has a short bucket and truncates; n=2500 forces (2500-100)+win_max over the model max for n=100.
    windows = [window(100, hard_cap=2560, bucket=512), window(2500, hard_cap=2560, bucket=2560)]
    outcomes = engine._decode_windows(windows, 4)
    assert passes[0] == ([2500], 1) and passes[1] == ([100], 1)      # each only needs one GPU row
    assert passes[2] == ([100], 1) and len(passes) == 3
    assert [g for g, _ in outcomes] == [[100], [2500]]
    assert engine.stats['solo_passes'] == 1 and engine.stats['retry_passes'] == 1


def test_sample_row_matches_the_lean_sampler_with_its_own_generator():
    torch = pytest.importorskip('torch')
    from malody_studio import v32_fast_decode as fd
    source = torch.Generator().manual_seed(4)
    for top_p in (0.9, 0.5, 1.0):
        for _ in range(50):
            logits = (torch.randn((1, 300), generator=source) * 4).to(torch.bfloat16).float()
            torch.manual_seed(123)
            expected = fd.sample_top_p(logits.clone(), top_p)
            assert torch.equal(bd._sample_row(logits.clone(), top_p, torch.Generator().manual_seed(123)), expected)


def test_full_prompt_consumes_no_rng_and_never_runs_a_graph(monkeypatch):
    engine = bd.BatchEngine()
    monkeypatch.setattr(engine, '_run_pass', lambda *_: pytest.fail('full prompt must not decode'))
    results = engine._decode_windows([window(2560, bucket=2560)], 4)
    assert results[0][0] == []


def test_vectorized_nucleus_probabilities_match_individual_rows():
    torch = pytest.importorskip('torch')
    source = torch.Generator().manual_seed(44)
    scores = torch.randn((4, 300), generator=source)
    for top_p in (0.5, 0.9, 1.0):
        expected = torch.cat([bd._sampling_probabilities(row[None].clone(), top_p) for row in scores])
        assert torch.equal(bd._sampling_probabilities(scores.clone(), top_p), expected)


def test_batching_requires_explicit_direct_policy_and_safe_conditions():
    request = {'presets': [{'key': 'a'}, {'key': 'b'}]}
    assert bs.batching_refusal(request) == 'not_direct_policy'
    request['direct_inference'] = True
    assert bs.batching_refusal(request) is None
    assert bs.batching_refusal({**request, 'cfg_scale': 2}) == 'cfg_scale_above_1'
    assert bs.batching_refusal({**request, 'timing_reference': 'clock.osu'}).startswith('legacy_route')
    assert bs.batching_refusal({**request, 'presets': [{'density_rounds': [1]}, {}]}).startswith('legacy_route')
    assert bs.batching_refusal({**request, 'presets': [{}]}) == 'single_preset'
    assert bs.clamp_streams(float('nan')) == bs.clamp_streams(float('inf')) == 0

@pytest.mark.parametrize('fields,reason', [
    ({'cfg_scale':None}, 'unsupported_cfg_scale'),
    ({'cfg_scale':'2'}, 'unsupported_cfg_scale'),
    ({'cfg_scale':True}, 'unsupported_cfg_scale'),
    ({'cfg_scale':float('nan')}, 'unsupported_cfg_scale'),
    ({'cfg_scale':0}, 'unsupported_cfg_scale'),
    ({'types_first':True}, 'unsupported_sampling_options'),
    ({'timeshift_bias':1}, 'unsupported_sampling_options'),
    ({'top_k':10}, 'unsupported_sampling_options'),
    ({'do_sample':False}, 'unsupported_sampling_options'),
])
def test_unsupported_request_options_refuse_batching(fields, reason):
    assert bs.batching_refusal({'direct_inference':True,'presets':[{},{}],**fields}) == reason

def test_oom_classification_follows_wrapped_causes_without_cycles():
    root=RuntimeError('CUDA out of memory')
    wrapper=RuntimeError('capture failed')
    wrapper.__cause__=root
    root.__context__=wrapper
    assert bs.is_cuda_oom(wrapper)
    root.args=('other error',)
    assert not bs.is_cuda_oom(wrapper)

def test_batch_identity_has_no_application_audio_dependencies(monkeypatch):
    import builtins
    original=builtins.__import__
    def guarded(name, *args, **kwargs):
        if name in ('soundfile','numpy') or name.endswith('direct_v32'):
            pytest.fail('worker identity must not import application audio dependencies')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',guarded)
    identity=bs.batch_identity({'parallel_streams':8})
    assert identity['parallel_streams']==8 and len(identity['code_sha256'])==64
