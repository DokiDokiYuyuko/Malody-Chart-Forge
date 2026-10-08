import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import torch

from malody_studio import v32_grammar_mask as gm

FIXTURES = Path(__file__).parent / 'fixtures'
LAYOUT = json.loads((FIXTURES / 'v32_mania_token_layout.json').read_text(encoding='utf-8'))
WINDOWS = {w['name']: w for w in json.loads((FIXTURES / 'v32_grammar_windows.json').read_text(encoding='utf-8'))['windows']}
CLEAN = [n for n, w in WINDOWS.items() if w['kind'] == 'clean']
VIOLATIONS = [n for n, w in WINDOWS.items() if w['kind'] == 'violation']
R = LAYOUT['ranges']
T0, SNAP0, COL0 = R['t'][0], R['snap'][0], R['column'][0]
HS0, VOL0 = R['hitsound'][0], R['volume'][0]
CIRCLE, HOLD, HEND, SUSTAIN = R['circle'][0], R['hold_note'][0], R['hold_note_end'][0], R['hold_note_sustain'][0]
EOS, EOS_MAP = LAYOUT['eos'], LAYOUT['context_eos']['map']
SOS_MAP = LAYOUT['context_sos']['map']
V = LAYOUT['vocab_out']


@pytest.fixture(scope='module')
def tables():
    return gm.Tables(LAYOUT, 4)


def scores_peaking_at(token, rows=1):
    s = torch.full((rows, V), -5.0)
    s[:, token] = 5.0
    return s


def run(proc, ids, scores):
    return proc(torch.tensor([ids]), scores)


def test_layout_comes_from_the_real_tokenizer_classes(tables):
    assert gm.layout_problem(LAYOUT) is None
    assert tables.cls[T0] == gm.T_ and tables.cls[COL0 + 3] == gm.COL_ and tables.cls[COL0 + 4] == gm.COLBAD
    assert tables.cls[CIRCLE] == tables.cls[HOLD] == gm.TYPE_ and tables.cls[HEND] == gm.HEND and tables.cls[SUSTAIN] == gm.SUST
    assert tables.cls[EOS_MAP] == gm.CTRL and tables.cls[SOS_MAP] == gm.CTRL


@pytest.mark.parametrize('broken', [lambda l: l['ranges'].pop('hold_note_end'),
                                    lambda l: l['ranges'].update(column=[2877, 2879]),
                                    lambda l: l['context_eos'].pop('map'),
                                    lambda l: l['ranges'].update(snap=[5000, 5001])])
def test_missing_token_classes_disable_with_a_reason(broken):
    layout = json.loads(json.dumps(LAYOUT))
    broken(layout)
    assert gm.layout_problem(layout)
    with pytest.raises(ValueError):
        gm.Tables(layout, 4)


@pytest.mark.parametrize('name', CLEAN)
def test_clean_recorded_windows_are_never_masked(tables, name):
    w = WINDOWS[name]
    proc = gm.GrammarMaskLogitsProcessor(tables, 'cpu')
    for k, token in enumerate(w['gen']):
        ids = w['ptail'] + w['gen'][:k]
        assert tables.is_legal(ids, token), (name, k)
        out = run(proc, ids, scores_peaking_at(token))
        assert torch.isfinite(out[0, token])
        assert out.argmax().item() == token and out[0, token] == 5.0
    steps, top_masked, _, _, fallbacks, _ = proc.counts.tolist()
    assert (steps, top_masked, fallbacks) == (len(w['gen']), 0, 0)


@pytest.mark.parametrize('name', VIOLATIONS)
def test_first_illegal_token_of_each_violation_kind_is_masked_but_a_legal_token_remains(tables, name):
    w = WINDOWS[name]
    k = w['first_illegal']
    for before in range(k):
        assert tables.is_legal(w['ptail'] + w['gen'][:before], w['gen'][before])
    ids, bad = w['ptail'] + w['gen'][:k], w['gen'][k]
    assert not tables.is_legal(ids, bad)
    proc = gm.GrammarMaskLogitsProcessor(tables, 'cpu')
    out = run(proc, ids, scores_peaking_at(bad))
    assert out[0, bad] == float('-inf')
    assert torch.isfinite(out).any()
    assert out.argmax().item() in tables.legal_sets[tables.state(ids)]
    assert proc.counts.tolist()[1] == 1 and proc.counts.tolist()[4] == 0


def test_torch_and_python_state_agree_on_every_recorded_step(tables):
    proc = gm.GrammarMaskLogitsProcessor(tables, 'cpu')
    for w in WINDOWS.values():
        for k in range(len(w['gen']) + 1):
            ids = w['ptail'] + w['gen'][:k]
            assert proc.state(torch.tensor([ids])).item() == tables.state(ids)


def test_legal_note_grammar_walk(tables):
    legal_note = [T0 + 40, SNAP0 + 2, COL0 + 3, HS0 + 1, VOL0 + 50, CIRCLE]
    tail_end = [T0 + 80, SNAP0, COL0, HEND]
    sustain = [T0 + 90, COL0 + 1, SUSTAIN]
    stream = [SOS_MAP] + legal_note + tail_end + sustain + [EOS_MAP]
    for k in range(1, len(stream)):
        assert tables.is_legal(stream[:k], stream[k]), k


@pytest.mark.parametrize('tail,state', [
    ([HS0, VOL0], gm.S0),                                     # bare hitsound/volume: nothing opened
    ([COL0 + 6, HEND], gm.S0),                                # bare out-of-range lane then tail
    ([T0 + 5, SNAP0, COL0, HS0, EOS_MAP, SNAP0], gm.S0),      # control token reset, then stray snap
    ([T0 + 5, SNAP0, COL0, HS0, EOS_MAP, SOS_MAP], gm.S0),
    ([HOLD, T0 + 9], gm.ST),                                  # garbage then a fresh t
    ([CIRCLE, HS0, T0, SNAP0], gm.SSNAP),
    ([COL0 + 5, T0, SNAP0, COL0], gm.SCOL),                   # lane 5 earlier does not matter
    ([T0, SNAP0, COL0, HS0], gm.SHS),
    ([T0, SNAP0, COL0, HS0, VOL0], gm.SVOL),
    ([HS0, COL0, SNAP0, T0 + 3], gm.ST),
    ([T0, COL0], gm.STC),
    ([COL0], gm.S0), ([], gm.S0), ([SNAP0, COL0], gm.S0),
])
def test_state_is_stateless_and_resets_on_dirty_prompt_tails(tables, tail, state):
    assert tables.state(tail) == state
    assert gm.GrammarMaskLogitsProcessor(tables, 'cpu').state(torch.tensor([tail or [0]])).item() == (state if tail else gm.S0)


def test_state_never_traps_after_garbage_and_always_has_legal_end_or_time(tables):
    for garbage in ([COL0 + 7], [HS0], [VOL0, VOL0], [HEND], [CIRCLE, CIRCLE], [SNAP0, SNAP0, SNAP0]):
        assert {EOS, EOS_MAP, T0} <= tables.legal_sets[tables.state(garbage)]


def test_fallback_when_nothing_legal_is_left_leaves_scores_unmasked_and_counts(tables):
    proc = gm.GrammarMaskLogitsProcessor(tables, 'cpu')
    scores = torch.full((1, V), float('-inf'))
    scores[0, CIRCLE] = 1.0  # only an illegal token (state S0) survives the other processors
    out = run(proc, [SOS_MAP], scores.clone())
    assert torch.equal(out, scores)
    counts = proc.counts.tolist()
    assert counts[4] == 1 and counts[1] == 0


def test_batched_rows_have_independent_states_and_cfg_row_count(tables):
    proc = gm.GrammarMaskLogitsProcessor(tables, 'cpu')
    ids = torch.tensor([[SOS_MAP, T0, SNAP0, COL0, HS0], [SOS_MAP, T0 + 1, SNAP0, COL0, HEND], [3, 3, 3, 3, 3], [3, 3, 3, 3, 3]])
    scores = torch.zeros((2, V))   # CFG-style: more id rows than score rows
    out = proc(ids, scores)
    assert torch.isfinite(out[0]).sum() == len(tables.legal_ids[gm.SHS])
    assert torch.isfinite(out[1]).sum() == len(tables.legal_ids[gm.S0])


def test_short_prompt_is_padded_with_control_class(tables):
    proc = gm.GrammarMaskLogitsProcessor(tables, 'cpu')
    out = proc(torch.tensor([[T0]]), torch.zeros((1, V)))
    assert torch.isfinite(out[0]).sum() == len(tables.legal_ids[gm.ST])


def test_wrong_score_width_is_left_alone_and_counted(tables):
    proc = gm.GrammarMaskLogitsProcessor(tables, 'cpu')
    scores = torch.zeros((1, V + 7))
    assert torch.equal(run(proc, [T0], scores), scores)
    assert proc.counts.tolist()[5] == 1


@pytest.mark.parametrize('args,reason', [
    ((None, 'map', False, 3, 4, 1.0), 'policy_not_requested'),
    (('mania-grammar-mask-v0', 'map', False, 3, 4, 1.0), 'policy_not_requested'),
    ((gm.GRAMMAR_POLICY, 'timing', False, 3, 4, 1.0), 'context_not_map'),
    ((gm.GRAMMAR_POLICY, 'map', True, 3, 4, 1.0), 'types_first_tokenizer'),
    ((gm.GRAMMAR_POLICY, 'map', False, 0, 4, 1.0), 'gamemode_not_mania'),
    ((gm.GRAMMAR_POLICY, 'map', False, 3, 7, 1.0), 'keycount_not_4'),
    ((gm.GRAMMAR_POLICY, 'map', False, 3, 4, 2.0), 'cfg_scale_above_1'),
    ((gm.GRAMMAR_POLICY, 'map', False, 3, 4, 1.0), None),
    ((gm.GRAMMAR_POLICY, 'map', False, 3, 4, None), None),
])
def test_activation_conditions(args, reason):
    assert gm.inactive_reason(*args) == reason


# --- install / builder behaviour with stand-ins for the upstream modules -------------------
class Key:
    def __init__(self, value):
        self.value = value


class FakeTokenizer:
    def __init__(self):
        keys = {k: Key(k) for k in R}
        self.event_start = {keys[k]: v[0] for k, v in R.items()}
        self.event_end = {keys[k]: v[1] for k, v in R.items()}
        self.context_sos = {Key(k): v for k, v in LAYOUT['context_sos'].items()}
        self.context_eos = {Key(k): v for k, v in LAYOUT['context_eos'].items()}
        self.pad_id, self.sos_id, self.eos_id = LAYOUT['pad'], LAYOUT['sos'], LAYOUT['eos']
        self.vocab_size_in, self.vocab_size_out = LAYOUT['vocab_in'], V


@pytest.fixture
def upstream(monkeypatch):
    def build_logits_processors(tokenizer, cfg_scale, timeshift_bias, types_first, temperature, timing_temperature,
                                mania_column_temperature, taiko_hit_temperature, lookback_time, device):
        return ['stock']
    server, compiled = ModuleType('server'), ModuleType('compiled_decode')
    server.build_logits_processors = compiled.build_logits_processors = build_logits_processors
    monkeypatch.setitem(sys.modules, 'osuT5.osuT5.inference.server', server)
    monkeypatch.setitem(sys.modules, 'osuT5.osuT5.inference.compiled_decode', compiled)
    monkeypatch.setattr(gm, '_current', None)
    monkeypatch.setattr(gm, '_tables', {})
    scripted = {'steps': [], 'cfg': 1.0, 'types_first': False}

    class Processor:
        def __init__(self, args, model, tokenizer, cfg_scale=None):
            self.args, self.tokenizer = args, tokenizer

        def model_generate(self, model_kwargs, **kwargs):
            built = sys.modules['osuT5.osuT5.inference.server'].build_logits_processors(
                self.tokenizer, scripted['cfg'], 0, scripted['types_first'], .9, .9, .8, .8, 8000, 'cpu')
            ids, outputs = list(model_kwargs['prompt']), []
            for token in scripted['steps']:
                scores = scores_peaking_at(token)
                for processor in built:
                    if callable(processor):
                        scores = processor(torch.tensor([ids]), scores)
                outputs.append(scores.argmax().item())
                ids.append(token)
            return outputs

        def generate(self, **kwargs):
            return [self.model_generate({'prompt': [SOS_MAP, T0 + 5]}, **c) for c in kwargs['calls']]

    module = SimpleNamespace(Processor=Processor)
    return module, scripted, server, compiled


def args_for(tmp_path, **kw):
    return SimpleNamespace(output_path=str(tmp_path), gamemode=3, keycount=4, **kw)


def generate(module, scripted, tmp_path, calls=(({'context_type': 'timing'}), ({'context_type': 'map'}), ({'context_type': 'map'})), **kw):
    processor = module.Processor(args_for(tmp_path), None, FakeTokenizer())
    return processor.generate(calls=list(calls)), processor


def test_install_is_idempotent_and_wraps_both_builder_references_once(upstream):
    module, scripted, server, compiled = upstream
    first = gm.install(module, policy=gm.GRAMMAR_POLICY)
    builder = server.build_logits_processors
    assert gm.install(module, policy=gm.GRAMMAR_POLICY) is first and module.Processor is first
    assert server.build_logits_processors is builder and compiled.build_logits_processors._malody_grammar_builder
    assert builder._malody_original('x', 1, 0, False, 1, 1, 1, 1, 1, 'cpu') == ['stock'] and not hasattr(builder._malody_original, '_malody_grammar_builder')


def test_unknown_policy_is_rejected(upstream):
    with pytest.raises(ValueError):
        gm.install(upstream[0], policy='other')


def test_masks_only_map_windows_and_writes_statistics(upstream, tmp_path):
    module, scripted, *_ = upstream
    gm.install(module, policy=gm.GRAMMAR_POLICY)
    scripted['steps'] = [CIRCLE, T0 + 9, SNAP0, COL0 + 5]   # illegal at S0 (first step), legal t/snap, illegal lane
    results, _ = generate(module, scripted, tmp_path)
    assert results[0] == [CIRCLE, T0 + 9, SNAP0, COL0 + 5]    # timing call is untouched
    assert results[1][0] != CIRCLE and results[1][3] != COL0 + 5 and results[1][1:3] == [T0 + 9, SNAP0]
    doc = json.loads((tmp_path / gm.ARTIFACT).read_text(encoding='utf-8'))
    assert doc['policy'] == gm.GRAMMAR_POLICY and doc['active'] is True and doc['inactive_reason'] is None
    assert doc['map_windows'] == doc['active_windows'] == 2 and doc['map_decode_steps'] == 8
    assert doc['steps_top_token_masked'] == 4 and doc['first_steps'] == 2 and doc['first_step_top_token_masked'] == 2
    assert doc['fallbacks_unmasked'] == 0 and doc['tables_identity'] and doc['schema'] == gm.SCHEMA


@pytest.mark.parametrize('setting,reason', [({'types_first': True}, 'types_first_tokenizer'), ({'cfg': 2.0}, 'cfg_scale_above_1')])
def test_inactive_decode_leaves_scores_untouched_and_records_reason(upstream, tmp_path, setting, reason):
    module, scripted, *_ = upstream
    gm.install(module, policy=gm.GRAMMAR_POLICY)
    scripted.update(setting, steps=[CIRCLE, COL0 + 5])
    results, _ = generate(module, scripted, tmp_path)
    assert results[1] == results[2] == [CIRCLE, COL0 + 5]
    doc = json.loads((tmp_path / gm.ARTIFACT).read_text(encoding='utf-8'))
    assert doc['active'] is False and doc['inactive_reason'] == reason and doc['active_windows'] == 0 and doc['map_decode_steps'] == 0


def test_keycount_and_gamemode_other_than_mania_4k_are_inactive(upstream, tmp_path):
    module, scripted, *_ = upstream
    gm.install(module, policy=gm.GRAMMAR_POLICY)
    scripted['steps'] = [CIRCLE]
    for kw, reason in (({'keycount': 7}, 'keycount_not_4'), ({'gamemode': 0}, 'gamemode_not_mania')):
        processor = module.Processor(SimpleNamespace(output_path=str(tmp_path / reason), **{'gamemode': 3, 'keycount': 4, **kw}), None, FakeTokenizer())
        assert processor.generate(calls=[{'context_type': 'map'}]) == [[CIRCLE]]
        assert json.loads((tmp_path / reason / gm.ARTIFACT).read_text(encoding='utf-8'))['inactive_reason'] == reason


def test_policy_none_in_a_reused_process_is_completely_unmasked_and_writes_nothing(upstream, tmp_path):
    module, scripted, server, _ = upstream
    gm.install(module, policy=gm.GRAMMAR_POLICY)
    scripted['steps'] = [CIRCLE, COL0 + 5]
    masked_dir, plain_dir = tmp_path / 'masked', tmp_path / 'plain'
    masked, _ = generate(module, scripted, masked_dir)
    assert masked[1][0] != CIRCLE
    assert gm.install(module, policy=None) is module.Processor
    plain, processor = generate(module, scripted, plain_dir)
    assert plain[1] == plain[2] == [CIRCLE, COL0 + 5] and not (plain_dir / gm.ARTIFACT).exists()
    assert gm._current is None
    assert server.build_logits_processors(FakeTokenizer(), 1.0, 0, False, .9, .9, .8, .8, 8000, 'cpu') == ['stock']
    gm.install(module, policy=gm.GRAMMAR_POLICY)   # and it switches back on, with fresh statistics
    again, _ = generate(module, scripted, tmp_path / 'again')
    assert again[1][0] != CIRCLE
    assert json.loads((tmp_path / 'again' / gm.ARTIFACT).read_text(encoding='utf-8'))['map_windows'] == 2


def test_statistics_do_not_leak_between_processor_instances(upstream, tmp_path):
    module, scripted, *_ = upstream
    gm.install(module, policy=gm.GRAMMAR_POLICY)
    scripted['steps'] = [CIRCLE]
    generate(module, scripted, tmp_path / 'a')
    generate(module, scripted, tmp_path / 'b', calls=[{'context_type': 'map'}])
    assert json.loads((tmp_path / 'a' / gm.ARTIFACT).read_text(encoding='utf-8'))['map_decode_steps'] == 2
    assert json.loads((tmp_path / 'b' / gm.ARTIFACT).read_text(encoding='utf-8'))['map_decode_steps'] == 1


def test_timing_only_processor_writes_no_statistics(upstream, tmp_path):
    module, scripted, *_ = upstream
    gm.install(module, policy=gm.GRAMMAR_POLICY)
    scripted['steps'] = [T0]
    generate(module, scripted, tmp_path, calls=[{'context_type': 'timing'}])
    assert not (tmp_path / gm.ARTIFACT).exists()


def test_unusable_tokenizer_layout_disables_with_a_recorded_reason(upstream, tmp_path):
    module, scripted, *_ = upstream
    gm.install(module, policy=gm.GRAMMAR_POLICY)
    scripted['steps'] = [CIRCLE]
    tokenizer = FakeTokenizer()
    tokenizer.event_start = {k: v for k, v in tokenizer.event_start.items() if k.value != 'hold_note_end'}
    processor = module.Processor(args_for(tmp_path), None, tokenizer)
    assert processor.generate(calls=[{'context_type': 'map'}]) == [[CIRCLE]]
    doc = json.loads((tmp_path / gm.ARTIFACT).read_text(encoding='utf-8'))
    assert doc['active'] is False and doc['inactive_reason'].startswith('tokenizer_layout_unusable')


def test_installed_processor_subclass_composes_with_other_processor_wrappers(upstream, tmp_path):
    module, scripted, *_ = upstream
    base = module.Processor

    class Other(base):
        _malody_parallel_clock = True
    module.Processor = Other
    installed = gm.install(module, policy=gm.GRAMMAR_POLICY)
    assert issubclass(installed, Other) and module.Processor is installed

    class Traced(installed):
        pass
    module.Processor = Traced
    assert gm.install(module, policy=None) is installed and installed._malody_grammar_policy is None
