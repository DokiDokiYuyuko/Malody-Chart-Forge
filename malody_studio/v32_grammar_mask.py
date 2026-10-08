"""Hard grammar mask for V32 types-last 4K mania MAP decoding.

A legal note is `t snap column hitsound volume (circle|hold_note)`,
`t snap column hold_note_end` or the sustain group `t column hold_note_sustain`.
The model occasionally emits bare fragments (a lone hitsound/hold_note, lane
tokens >= 4, pos/scroll_speed tokens), mostly as the first token of a window
after the lookback mask removed its dominant continuation. Illegal tokens get
-inf before top-p. The state is derived from the last ids only, so garbage in a
prompt cannot trap it. No vendor file is edited; the mask never invents notes.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib
import inspect
import json
import sys

try:
    from transformers import LogitsProcessor, LogitsProcessorList
except ImportError:  # main venv: the FSM stays importable and testable
    LogitsProcessor, LogitsProcessorList = object, list

GRAMMAR_POLICY = 'mania-grammar-mask-v1'
SCHEMA = 'v32-grammar-mask-v1'
ARTIFACT = 'v32-grammar-mask.json'

OTHER, T_, SNAP_, COL_, COLBAD, HS_, VOL_, TYPE_, HEND, SUST, CTRL = range(11)
S0, ST, SSNAP, SCOL, SHS, SVOL, STC = range(7)
STATE_NAMES = ('S0', 'T', 'SNAP', 'COL', 'HS', 'VOL', 'TC')
CLASS_NAMES = ('other', 't', 'snap', 'col', 'colbad', 'hitsound', 'volume', 'type', 'hold_end', 'sustain', 'ctrl')
NEEDED = ('t', 'snap', 'column', 'hitsound', 'volume', 'circle', 'hold_note', 'hold_note_end', 'hold_note_sustain')
HISTORY = 5


def layout_from_tokenizer(tokenizer):
    """Plain, JSON-able id layout read from the real tokenizer object."""
    ranges = {k.value: [int(tokenizer.event_start[k]), int(tokenizer.event_end[k])] for k in tokenizer.event_start}
    ctx = lambda d: {k.value: int(v) for k, v in d.items()}
    return {'ranges': ranges, 'pad': int(tokenizer.pad_id), 'sos': int(tokenizer.sos_id), 'eos': int(tokenizer.eos_id),
            'context_sos': ctx(tokenizer.context_sos), 'context_eos': ctx(tokenizer.context_eos),
            'vocab_in': int(tokenizer.vocab_size_in), 'vocab_out': int(tokenizer.vocab_size_out)}


def layout_problem(layout, keycount=4):
    """Reason string when the expected token classes are absent, else None."""
    try:
        ranges, vocab = layout['ranges'], layout['vocab_out']
        for name in NEEDED:
            start, end = ranges[name]
            if not 0 <= start < end <= vocab:
                return f'token class {name} missing or outside the output vocabulary'
        if ranges['column'][1] - ranges['column'][0] < keycount:
            return 'column tokens fewer than keycount'
        if 'map' not in layout['context_eos']:
            return 'MAP context end token missing'
        if not 0 <= layout['context_eos']['map'] < vocab or not 0 <= layout['eos'] < vocab:
            return 'end tokens outside the output vocabulary'
    except (KeyError, TypeError, ValueError) as exc:
        return f'layout unreadable: {type(exc).__name__}'
    return None


class Tables:
    """Token-class table and per-state legal sets for one tokenizer layout."""

    def __init__(self, layout, keycount=4):
        problem = layout_problem(layout, keycount)
        if problem:
            raise ValueError(problem)
        self.layout, self.keycount = layout, keycount
        r = layout['ranges']
        self.vocab = layout['vocab_out']
        self.cls = [OTHER] * (max(layout['vocab_in'], self.vocab) + 1)

        def span(name):
            return range(*r[name])

        def mark(ids, value):
            for i in ids:
                self.cls[i] = value
        mark(span('t'), T_)
        mark(span('snap'), SNAP_)
        col0 = r['column'][0]
        mark(span('column'), COLBAD)
        mark(range(col0, col0 + keycount), COL_)
        mark(span('hitsound'), HS_)
        mark(span('volume'), VOL_)
        mark(span('circle'), TYPE_)
        mark(span('hold_note'), TYPE_)
        mark(span('hold_note_end'), HEND)
        mark(span('hold_note_sustain'), SUST)
        mark([layout['pad'], layout['sos'], layout['eos'], *layout['context_sos'].values(), *layout['context_eos'].values()], CTRL)
        cols = [col0 + k for k in range(keycount)]
        self.legal_ids = {
            S0: [*span('t'), layout['eos'], layout['context_eos']['map']],
            ST: [*span('snap'), *cols], SSNAP: cols,
            SCOL: [*span('hitsound'), r['hold_note_end'][0]],
            SHS: [*span('volume')], SVOL: [r['circle'][0], r['hold_note'][0]],
            STC: [r['hold_note_sustain'][0]]}
        self.legal_sets = {k: frozenset(v) for k, v in self.legal_ids.items()}
        self.identity = hashlib.sha256(json.dumps([layout, keycount], sort_keys=True).encode()).hexdigest()[:16]
        self._torch = {}

    def state(self, ids):
        """FSM state after `ids` (python sequence); the same rule as the tensor path."""
        c = [self.cls[i] for i in list(ids)[-HISTORY:]]
        return _state_from_classes([CTRL] * (HISTORY - len(c)) + c)

    def is_legal(self, ids, token):
        return token in self.legal_sets[self.state(ids)]

    def on(self, device):
        import torch
        key = str(device)
        if key not in self._torch:
            legal = torch.zeros((len(STATE_NAMES), self.vocab), dtype=torch.bool)
            for state, ids in self.legal_ids.items():
                legal[state, ids] = True
            self._torch[key] = (torch.tensor(self.cls, dtype=torch.long, device=device), legal.to(device))
        return self._torch[key]


def _state_from_classes(c):
    c5, c4, c3, c2, c1 = c[-5:]
    if c1 == T_:
        return ST
    if c1 == SNAP_ and c2 == T_:
        return SSNAP
    if c1 == COL_ and c2 == SNAP_ and c3 == T_:
        return SCOL
    if c1 == COL_ and c2 == T_:
        return STC
    if c1 == HS_ and c2 == COL_ and c3 == SNAP_ and c4 == T_:
        return SHS
    if c1 == VOL_ and c2 == HS_ and c3 == COL_ and c4 == SNAP_ and c5 == T_:
        return SVOL
    return S0


def inactive_reason(policy, context, types_first, gamemode, keycount, cfg_scale):
    """Why the mask must not run for this decode call (None when it may)."""
    if policy != GRAMMAR_POLICY:
        return 'policy_not_requested'
    if context != 'map':
        return 'context_not_map'
    if types_first:
        return 'types_first_tokenizer'
    if gamemode != 3:
        return 'gamemode_not_mania'
    if keycount != 4:
        return 'keycount_not_4'
    if cfg_scale is not None and cfg_scale > 1:
        return 'cfg_scale_above_1'
    return None


class GrammarMaskLogitsProcessor(LogitsProcessor):
    """One instance per decoded window; counters stay on the device until read."""

    def __init__(self, tables, device):
        self.tables = tables
        self.cls, self.legal = tables.on(device)
        self.counts = self.cls.new_zeros(6)  # steps, top masked, first steps, first-step top masked, fallbacks, shape mismatches
        self.first = True

    def state(self, ids):
        import torch
        c = self.cls[ids[:, -HISTORY:]]
        if c.shape[1] < HISTORY:
            c = torch.nn.functional.pad(c, (HISTORY - c.shape[1], 0), value=CTRL)
        c1, c2, c3, c4, c5 = c[:, 4], c[:, 3], c[:, 2], c[:, 1], c[:, 0]
        st = torch.zeros_like(c1)
        st = torch.where(c1 == T_, ST, st)
        st = torch.where((c1 == SNAP_) & (c2 == T_), SSNAP, st)
        st = torch.where((c1 == COL_) & (c2 == SNAP_) & (c3 == T_), SCOL, st)
        st = torch.where((c1 == COL_) & (c2 == T_), STC, st)
        st = torch.where((c1 == HS_) & (c2 == COL_) & (c3 == SNAP_) & (c4 == T_), SHS, st)
        return torch.where((c1 == VOL_) & (c2 == HS_) & (c3 == COL_) & (c4 == SNAP_) & (c5 == T_), SVOL, st)

    def __call__(self, input_ids, scores):
        import torch
        rows, first, self.first = scores.shape[0], self.first, False
        if scores.shape[-1] != self.legal.shape[1]:
            self.counts[5] += rows
            return scores
        legal = self.legal[self.state(input_ids[:rows])]
        top_bad = ~legal.gather(1, scores.argmax(-1, keepdim=True))[:, 0]
        masked = scores.masked_fill(~legal, float('-inf'))
        ok = torch.isfinite(masked).any(-1)
        changed = (top_bad & ok).long().sum()
        self.counts[0] += rows
        self.counts[1] += changed
        if first:
            self.counts[2] += rows
            self.counts[3] += changed
        self.counts[4] += (~ok).long().sum()
        return torch.where(ok[:, None], masked, scores)  # nothing legal left: leave unmasked, counted


class _Request:
    """Statistics of one Processor instance, i.e. one generate call of one request."""

    def __init__(self, policy, args):
        self.policy = policy
        self.output = getattr(args, 'output_path', None)
        self.gamemode, self.keycount = getattr(args, 'gamemode', None), getattr(args, 'keycount', None)
        self.map_windows = self.active_windows = 0
        self.reasons, self.pending = Counter(), []
        self.totals = [0] * 6
        self.tables_identity = None
        self.failure = None

    def absorb(self):
        for proc in self.pending:
            for i, v in enumerate(proc.counts.tolist()):
                self.totals[i] += int(v)
        self.pending = []

    def document(self):
        steps, top, first, first_top, fallbacks, mismatch = self.totals
        return {'schema': SCHEMA, 'policy': self.policy, 'active': self.active_windows > 0 and not self.reasons,
                'inactive_reason': next(iter(self.reasons), None), 'inactive_reasons': dict(self.reasons),
                'map_windows': self.map_windows, 'active_windows': self.active_windows, 'map_decode_steps': steps,
                'steps_top_token_masked': top, 'first_steps': first, 'first_step_top_token_masked': first_top,
                'fallbacks_unmasked': fallbacks, 'shape_mismatch_rows': mismatch,
                'tables_identity': self.tables_identity, 'failure': self.failure,
                'finished_at': datetime.now(timezone.utc).isoformat()}

    def write(self):
        if not self.policy or not self.map_windows or not self.output:
            return
        from pathlib import Path
        from malody_studio.resident import atomic
        self.absorb()
        Path(self.output).mkdir(parents=True, exist_ok=True)
        atomic(Path(self.output) / ARTIFACT, self.document())


_current = None      # (request, context) while a Processor.model_generate call is running
_tables = {}


def _tables_for(tokenizer, keycount):
    key = (id(tokenizer), keycount)
    if key not in _tables:
        _tables.clear()
        _tables[key] = (Tables(layout_from_tokenizer(tokenizer), keycount), tokenizer)
    return _tables[key][0]


def _wrap_builder(original):
    if getattr(original, '_malody_grammar_builder', False):
        return original
    signature = inspect.signature(original)

    def build(*args, **kwargs):
        processors = original(*args, **kwargs)
        current = _current
        if current is None:
            return processors
        request, context = current
        try:
            bound = signature.bind(*args, **kwargs).arguments
            request.map_windows += context == 'map'
            reason = inactive_reason(request.policy, context, bound['types_first'], request.gamemode,
                                     request.keycount, bound['cfg_scale'])
            if reason == 'context_not_map':
                return processors
            if reason is None:
                try:
                    tables = _tables_for(bound['tokenizer'], request.keycount)
                except Exception as exc:
                    reason = f'tokenizer_layout_unusable: {exc}'
            if reason:
                request.reasons[reason] += 1
                return processors
            processor = GrammarMaskLogitsProcessor(tables, bound['device'])
        except Exception as exc:  # the mask is optional; never break decoding
            request.reasons[f'mask_setup_failed: {type(exc).__name__}: {exc}'] += 1
            return processors
        request.tables_identity = tables.identity
        request.active_windows += 1
        request.pending.append(processor)
        merged = LogitsProcessorList()
        merged.extend(processors)
        merged.append(processor)
        return merged
    build._malody_grammar_builder = True
    build._malody_original = original
    return build


def _install_builders():
    for name in ('osuT5.osuT5.inference.server', 'osuT5.osuT5.inference.compiled_decode'):
        module = importlib.import_module(name)
        module.build_logits_processors = _wrap_builder(module.build_logits_processors)


def install(inference_module, *, policy=None):
    """Install once; the per-request policy is switched on every call (None = unmasked)."""
    if policy not in (None, GRAMMAR_POLICY):
        raise ValueError('V32 语法约束策略版本不受支持')
    processor = inference_module.Processor
    owner = next((c for c in processor.__mro__ if '_malody_grammar_mask' in c.__dict__), None)
    if owner is not None:
        owner._malody_grammar_policy = policy
        _install_builders()
        return owner

    class GrammarMaskProcessor(processor):
        _malody_grammar_mask = True
        _malody_grammar_policy = None

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._malody_grammar = _Request(type(self)._malody_grammar_policy, self.args)

        def model_generate(self, model_kwargs, **kwargs):
            global _current
            request = self._malody_grammar
            if not request.policy:
                return super().model_generate(model_kwargs, **kwargs)
            _current = (request, kwargs.get('context_type'))
            try:
                return super().model_generate(model_kwargs, **kwargs)
            finally:
                _current = None
                try:
                    request.absorb()
                except Exception as exc:
                    request.failure = f'{type(exc).__name__}: {exc}'

        def generate(self, *a, **kw):
            try:
                return super().generate(*a, **kw)
            finally:
                try:
                    self._malody_grammar.write()
                except Exception as exc:
                    print('V32 grammar mask statistics error: ' + repr(exc), file=sys.stderr)

    GrammarMaskProcessor._malody_grammar_policy = policy
    inference_module.Processor = GrammarMaskProcessor
    _install_builders()
    return GrammarMaskProcessor
