"""CPU integration of the production preset body, scheduler, hooks and diagnostics.

Only model/bootstrap/GPU decode are substituted; the worker's actual orchestration
and per-preset code are compiled from source to avoid loading deployed weights.
"""
import ast
import copy
from contextlib import nullcontext
from dataclasses import dataclass
import inspect
import json
from pathlib import Path
import sys
import textwrap
from types import ModuleType, SimpleNamespace

import pytest
import torch

from tools import mapperatorinator_worker as worker
from malody_studio import mapperatorinator, v32_batch_decode as bd, v32_batch_streams as bs
from malody_studio import v32_grammar_mask as grammar


@dataclass
class GenerationConfig:
    seed: int
    sr: float


def worker_body():
    parsed = ast.parse(textwrap.dedent(inspect.getsource(worker._run_request))).body[0]
    start = next(i for i, n in enumerate(parsed.body) if isinstance(n, ast.ClassDef) and n.name == 'Record')
    stop = next(i for i, n in enumerate(parsed.body) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'result' for t in n.targets))
    shell = ast.parse('''
def execute(request, args, destination, upstream_inference, generate, get_config, setup_inference_environment):
    original_context = list(args.in_context)
    original_beatmap = args.beatmap_path
    reference_policy = 'timing-context-only-v1'
    timing_attempted_keys = set()
    isolate_attempts = False
    attempt_records = []
    sequential_fallback = False
    batch_fallbacks = 0
    model = tokenizer = timing_model = timing_tokenizer = None
''').body[0]
    shell.body += parsed.body[start:stop]
    shell.body += ast.parse('return dict(charts=results, errors=failures, retries=retry_records, batching=batching_record, attempts=attempt_records, conditions=condition_records)').body
    scope = dict(vars(worker), torch=torch, status=lambda *_: None,
                 stage=lambda *a, **k: nullcontext(), v32_fast_decode=SimpleNamespace(enabled=lambda: True))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[shell], type_ignores=[])), worker.__file__, 'exec'), scope)
    return scope['execute']


@pytest.fixture
def cpu_worker(tmp_path, monkeypatch):
    source = tmp_path / 'audio.wav'
    source.write_bytes(b'CPU fixture: identity only')
    calls, rounds, releases, live_args = [], [], [], []
    options = SimpleNamespace(fail=None, oom_above=None, reject_seq=None, oom_tensor_refs=[],
                              sequential_error='injected sequential failure')
    upstream = SimpleNamespace()
    processor_module = ModuleType('osuT5.osuT5.inference.processor')
    package = ModuleType('osuT5.osuT5.inference')
    package.processor = processor_module
    monkeypatch.setitem(sys.modules, 'osuT5.osuT5.inference', package)
    monkeypatch.setitem(sys.modules, 'osuT5.osuT5.inference.processor', processor_module)
    sequential_decode = lambda *a: 17
    processor_module.model_generate_compiled = sequential_decode
    monkeypatch.setattr(bd, 'prepare_window', lambda model, tokenizer, model_kwargs, generate_kwargs, generator:
                        SimpleNamespace(generator=generator, **model_kwargs))
    monkeypatch.setattr(bd, 'make_generator', lambda seed, device: torch.Generator(device='cpu').manual_seed(seed))
    monkeypatch.setattr(torch.cuda, 'empty_cache', lambda: None)
    monkeypatch.setattr(grammar, '_current', None)

    class Processor:
        def __init__(self, args):
            self.args = args

        def generate(self, **kwargs):
            stream = bs.current_stream()
            current = (self.args.seed, 'map')
            grammar._current = current
            try:
                for number in range(2 if self.args.seed % 2 else 1):
                    result = processor_module.model_generate_compiled(None, None,
                        dict(seed=self.args.seed, number=number), {})
                    if stream is not None:
                        assert grammar._current is current
                        assert not torch.is_grad_enabled()
                        assert torch.is_autocast_enabled('cpu')
                        assert torch.get_autocast_dtype('cpu') == torch.bfloat16
                    self.args.train.values.append(result)
            finally:
                grammar._current = None
            return [([], [])]

    upstream.Processor = Processor

    class Engine:
        def __init__(self):
            self.stats = {'rounds': 0}

        def decode(self, requests, batch):
            assert grammar._current is None
            assert not torch.is_grad_enabled()
            assert torch.is_autocast_enabled('cpu')
            rounds.append((batch, [r.window.seed for r in requests]))
            self.stats['rounds'] += 1
            if options.oom_above is not None and batch > options.oom_above:
                import weakref
                tensor = torch.ones(8)
                options.oom_tensor_refs.append(weakref.ref(tensor))
                raise RuntimeError('CUDA out of memory: CPU injected')
            for req in requests:
                req.result = int(torch.randint(1, 1000, (1,), generator=req.window.generator).item())

        def release(self):
            # No error frame should retain the injected tensor at retry time.
            releases.append((len(rounds), all(ref() is None for ref in options.oom_tensor_refs)))

    monkeypatch.setattr(bd, 'BatchEngine', Engine)

    def generate(args, **kwargs):
        batched = bs.current_stream() is not None
        assert args.parallel is False
        if batched:
            assert args.train.values == []
        live_args.append(args)
        path = Path(args.output_path)
        calls.append((args.seed, batched, path))
        upstream.Processor(args).generate(out_context=['map'], generation_config=kwargs['generation_config'])
        if options.fail == args.seed and batched:
            (path / 'model-events.json').write_text(json.dumps({'failed_seed': args.seed}))
            raise ValueError('injected serialization failure')
        if options.reject_seq == args.seed and not batched:
            raise RuntimeError(options.sequential_error)
        (path / 'model-events.json').write_text(json.dumps({'seed': args.seed}))
        chart = path / 'returned.osu'
        chart.write_text('[HitObjects]\n64,192,100,1,0,0:0:0:0:\n', encoding='utf-8')
        return '', chart

    def execute(count=3, streams=4, direct=True, extra=None):
        request = dict(audio=str(source), seed=91, parallel_streams=streams, direct_inference=direct,
                       capture_native_trace=True, presets=[dict(key=f'p{i}', label=f'P{i}', sr=5+i,
                       seed=91+i, start_time=i*100, end_time=2000+i*100) for i in range(count)])
        request.update(extra or {})
        args = SimpleNamespace(in_context=[], beatmap_path='', device=torch.device('cpu'),
                               audio_path=str(source), output_path=str(tmp_path), parallel=False,
                               max_batch_size=32, seed=91, train=SimpleNamespace(values=[]))
        with torch.enable_grad(), torch.autocast('cpu', dtype=torch.bfloat16):
            result = worker_body()(request, args, tmp_path, upstream, generate,
                lambda a: (GenerationConfig(seed=a.seed, sr=a.difficulty), SimpleNamespace(mode=3)), lambda seed: None)
            assert torch.is_grad_enabled()  # stream state did not change its caller
        assert upstream.Processor is Processor
        assert processor_module.model_generate_compiled is sequential_decode
        assert grammar._current is None
        return result, args

    return SimpleNamespace(run=execute, calls=calls, rounds=rounds, releases=releases,
                           options=options, live_args=live_args, root=tmp_path)


def test_real_body_keeps_args_rng_and_diagnostics_isolated(cpu_worker):
    result, args = cpu_worker.run()
    assert set(result['charts']) == {'p0', 'p1', 'p2'} and not result['errors']
    assert args.train.values == []
    assert len({id(a) for a in cpu_worker.live_args}) == 3
    assert len({id(a.train) for a in cpu_worker.live_args}) == 3
    for args in cpu_worker.live_args:
        generator = torch.Generator().manual_seed(args.seed)
        assert args.train.values == [int(torch.randint(1, 1000, (1,), generator=generator).item())
                                     for _ in args.train.values]
    assert cpu_worker.rounds == [(4, [91, 92, 93]), (4, [91, 93])]
    assert [(c['executed_condition'], c['seed']) for c in result['conditions']] == [(5,91),(6,92),(7,93)]
    for index, chart in enumerate(result['charts'].values()):
        output = Path(chart).parent
        snapshot = json.loads((output/'resolved-parameters.json').read_text())
        assert snapshot['args']['seed'] == index+91 and snapshot['generation_config']['sr'] == index+5
        assert snapshot['args']['parallel'] is False
        trace = json.loads(next(output.glob('inference-attempts/*/v32-generation-diagnostics.json')).read_text())
        assert trace['status'] == 'complete' and len(trace['processor_calls']) == 1
        assert trace['processor_calls'][0]['generation_config']['seed'] == index+91
        assert json.loads((output/'model-events.json').read_text()) == {'seed': index+91}


def test_failed_stream_full_sequential_rerun_has_distinct_evidence(cpu_worker):
    cpu_worker.options.fail = 91
    result, _ = cpu_worker.run()
    assert result['batching']['sequential_reruns'] == ['p0']
    assert [seed for seed, batch, _ in cpu_worker.calls if not batch] == [91]
    attempts = [a for a in result['attempts'] if a['key'] == 'p0']
    assert [a['status'] for a in attempts] == ['failed', 'completed']
    assert [a['mode'] for a in attempts] == ['batch', 'sequential']
    assert attempts[0]['output'] != attempts[1]['output']
    assert json.loads((Path(attempts[0]['output'])/'model-events.json').read_text()) == {'failed_seed':91}
    assert Path(result['charts']['p0']).parent == Path(attempts[1]['output'])
    assert not result['errors']


def test_oom_releases_tensors_and_graphs_before_halved_retry(cpu_worker):
    cpu_worker.options.oom_above = 2
    result, args = cpu_worker.run(count=4)
    assert [a['limit'] for a in result['batching']['attempts']] == [4, 2]
    assert all(released for _, released in cpu_worker.releases)
    assert cpu_worker.releases[0][0] == 1
    assert not result['batching']['sequential_reruns']
    assert set(result['charts']) == {'p0', 'p1', 'p2', 'p3'}
    assert args.max_batch_size == 32
    assert all(len(list((cpu_worker.root/f'p{i}'/'decode-attempts').iterdir())) == 2 for i in range(4))


def test_terminal_sequential_failure_does_not_discard_successful_siblings(cpu_worker):
    cpu_worker.options.fail = cpu_worker.options.reject_seq = 91
    result, _ = cpu_worker.run()
    assert result['errors'] == {'p0':'injected sequential failure'}
    assert set(result['charts']) == {'p1', 'p2'}


def test_enabled_single_preset_is_noop_with_explicit_reason(cpu_worker):
    result, _ = cpu_worker.run(count=1, streams=8)
    assert result['batching']['reason'] == 'single_preset'
    assert result['batching']['requested'] == 8
    assert not cpu_worker.rounds and not result['attempts']
    assert set(result['charts']) == {'p0'}


def test_terminal_encoder_oom_at_size_one_retains_sibling_success(cpu_worker):
    cpu_worker.options.fail = cpu_worker.options.reject_seq = 91
    cpu_worker.options.sequential_error = 'CUDA out of memory: terminal encoder allocation'
    result, args = cpu_worker.run()
    assert set(result['charts']) == {'p1', 'p2'}
    assert result['errors'] == {'p0': cpu_worker.options.sequential_error}
    attempts = [a for a in result['attempts'] if a['key'] == 'p0']
    assert len(attempts) == 7  # failed batch plus encoder 32/16/8/4/2/1
    assert all(a['status'] == 'failed' for a in attempts)
    assert args.max_batch_size == 32


@pytest.mark.parametrize('streams,direct,reason', [(0,True,'disabled'), (4,False,'not_direct_policy')])
def test_default_and_frozen_routes_stay_sequential(cpu_worker, streams, direct, reason):
    result, _ = cpu_worker.run(streams=streams, direct=direct)
    assert not cpu_worker.rounds and all(not batch for _, batch, _ in cpu_worker.calls)
    assert result['batching']['reason'] == reason and not result['attempts']
    assert not list(cpu_worker.root.glob('*/decode-attempts'))


def test_worker_request_validates_direct_marker_and_caps_opt_in(tmp_path):
    from malody_studio.nps_star_calibration import freeze_policy
    base = dict(title='x', artist='y', seed=7, ln_ratio=.15, direct_inference=True,
                parallel_streams=100, _advanced_presets=[dict(key='a',label='a',sr=5),dict(key='b',label='b',sr=6)])
    request = mapperatorinator.build_worker_request('source', tmp_path, base)
    assert request['parallel_streams'] == 16 and 'direct_inference' not in request
    request = mapperatorinator.build_worker_request('source', tmp_path, {**base,'direct_v32_policy':freeze_policy()})
    assert request['direct_inference'] is True
    with pytest.raises(ValueError):
        mapperatorinator.build_worker_request('source', tmp_path, {**base,'direct_v32_policy':{'version':'bad'}})
    assert mapperatorinator.build_worker_request('source', tmp_path, {k:v for k,v in base.items() if k!='parallel_streams'})['parallel_streams'] == 0


def test_cache_identity_separates_batch_policy_and_detects_code_change(tmp_path, monkeypatch):
    from malody_studio import direct_v32
    assert direct_v32.batch_identity({}) is None
    identity = direct_v32.batch_identity({'parallel_streams':4})
    assert identity != direct_v32.batch_identity({'parallel_streams':8})
    parent='b'*32
    folder=tmp_path/'outputs'/parent
    folder.mkdir(parents=True)
    snapshot=dict(retry_of=parent,settings={'parallel_streams':4},direct_v32_policy={'version':'frozen'},variants=[dict(key='v')])
    requests=[dict(key='r',variant='v')]
    cache=dict(policy=snapshot['direct_v32_policy'], source_pcm_sha256='pcm',failed_variants=[],errors=[],
               requests=[dict(**requests[0],status='generated',owned_heads=1)],raw={'v':[{'id':'h'}]})
    (folder/'queue-worker.json').write_text(json.dumps(dict(options=dict(_advanced=snapshot))))
    path=folder/'direct-generation-cache.json'
    path.write_text(json.dumps(cache))
    monkeypatch.setattr(direct_v32,'ROOT',tmp_path)
    assert direct_v32.reusable_cache(snapshot,requests,'pcm') is None
    cache['decode_batching_identity']=identity
    path.write_text(json.dumps(cache))
    assert direct_v32.reusable_cache(snapshot,requests,'pcm') is not None
    cache['decode_batching_identity']['code_sha256']='older-code'
    path.write_text(json.dumps(cache))
    assert direct_v32.reusable_cache(snapshot,requests,'pcm') is None

def test_unsupported_window_uses_original_hook_under_rng_swap(cpu_worker, monkeypatch):
    calls=[]
    model=SimpleNamespace(device='fake-device')
    stream=SimpleNamespace(rng='independent-rng',submit=lambda _:pytest.fail('unsupported window must not enter batch'))
    monkeypatch.setattr(bs._local,'stream',stream,raising=False)
    monkeypatch.setattr(bd,'prepare_window',lambda *_:None)
    monkeypatch.setattr(bd,'RngSwap',lambda rng,device:calls.append((rng,device)) or nullcontext())
    processor=sys.modules['osuT5.osuT5.inference.processor']
    previous=processor.model_generate_compiled
    uninstall=bd.install_hook(bs)
    try:
        assert processor.model_generate_compiled(model,None,{}, {}) == 17
    finally:
        uninstall()
    assert processor.model_generate_compiled is previous
    assert calls == [('independent-rng','fake-device')]
