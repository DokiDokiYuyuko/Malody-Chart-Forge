"""Lean V32 decode loop: the same tokens with far fewer host-side operations.

The upstream CUDA-graph loop replays one captured decoder step per token, but
between replays it launches about sixty tiny GPU operations from Python: the
grammar mask rebuilds its state from tensors, the monotonic mask tracks state on
the device, the end test builds tensors, and so on. On the measured 265 s song
each token cost 2.35 ms of GPU work plus a comparable amount of single-thread
launch overhead, so one CPU core was saturated while the GPU idled between
kernels.

This loop keeps the decoded token on the host (it is synchronised once per
token anyway for the end test), tracks the monotonic and grammar state in plain
Python, and applies the resulting masks with single fills. The masked scores
and the sampling arithmetic are value-identical to the upstream processors, and
``torch.multinomial`` is called exactly as often, so a fixed seed yields the
same tokens. Anything outside the verified configuration is delegated to the
unmodified upstream loop. No vendor file is edited.
"""
import os
import time

LEAN_DECODE_POLICY = 'lean-host-state-v1'
DISABLE_ENV = 'MALODY_V32_LEAN_DECODE'


def enabled():
    return os.environ.get(DISABLE_ENV, '1').strip().lower() not in ('0', 'false', 'off')


class TimeShiftFloor:
    """Host-side twin of the upstream monotonic time-shift mask.

    A time shift may not be smaller than the last emitted one while that shift
    is more recent than the last start token.
    """

    def __init__(self, ts_start, ts_end, sos_ids):
        self.ts_start, self.ts_end = ts_start, ts_end
        self.sos_ids = frozenset(sos_ids)
        self.last = None
        self.active = False

    def reset(self, prompt_ids):
        self.last, self.active = None, False
        for token in prompt_ids:
            self.update(token)
        return self

    def update(self, token):
        if self.ts_start <= token < self.ts_end:
            self.last, self.active = token, True
        elif token in self.sos_ids:
            self.active = False

    def bound(self):
        """First time-shift id that stays allowed (ids below it are forbidden)."""
        return self.last if self.active else self.ts_start


def sample_top_p(scores, top_p):
    """Nucleus sampling with the upstream arithmetic; consumes ``scores``.

    Upstream scatters the kept sorted scores into a fresh ``-inf`` tensor. The
    sort indices are a permutation, so scattering into ``scores`` itself writes
    every position with the same values.
    """
    import torch
    import torch.nn.functional as F
    if 0 < top_p < 1.0:
        sorted_scores, order = torch.sort(scores, descending=True, dim=-1)
        remove = F.softmax(sorted_scores, dim=-1).cumsum(dim=-1) > top_p
        sorted_scores[..., 1:].masked_fill_(remove[..., :-1], float('-inf'))
        scores.scatter_(-1, order, sorted_scores)
    return torch.multinomial(F.softmax(scores, dim=-1), num_samples=1)


def masked_scores(logits, temperature, ts_start, high, illegal_row=None):
    """Scores after temperature, the time-shift prefix mask and the grammar mask.

    ``high`` joins the monotonic floor and the lookback mask: both forbid a
    prefix of the time-shift range, so their union is one slice. Returns the
    scores and, with a grammar row, the top token before that row was applied.
    """
    scores = logits / temperature
    if high > ts_start:
        scores[:, ts_start:high] = float('-inf')
    if illegal_row is None:
        return scores, None
    top = scores.argmax(-1)
    scores.masked_fill_(illegal_row, float('-inf'))
    return scores, top


def _plan(processors, tokenizer, compiled, vocab):
    """Recognise the verified processor chain, else None (upstream order kept).

    Expected: monotonic mask, plain temperature, optional types-last lookback
    mask, optional grammar mask. Returns (temperature, lookback_end, grammar).
    """
    from transformers import TemperatureLogitsWarper
    from osuT5.osuT5.event import EventType
    from osuT5.osuT5.inference.logit_processors import LookbackBiasLogitsWarper
    from .v32_grammar_mask import GrammarMaskLogitsProcessor
    chain = list(processors)
    if not chain or type(chain[0]) is not compiled.MonotonicTimeShiftLogitsProcessor:
        return None
    ts_start = tokenizer.event_start[EventType.TIME_SHIFT]
    ts_end = tokenizer.event_end[EventType.TIME_SHIFT]
    if not 0 <= ts_start < ts_end <= vocab:
        return None
    chain = chain[1:]
    if not chain or type(chain[0]) is not TemperatureLogitsWarper:
        return None
    temperature = chain[0].temperature
    if not isinstance(temperature, (int, float)) or temperature <= 0:
        return None
    chain = chain[1:]
    lookback_end = ts_start
    if chain and type(chain[0]) is LookbackBiasLogitsWarper:
        lookback = chain[0]
        if lookback.types_first or lookback.lookback_start != ts_start \
                or not ts_start <= lookback.lookback_end <= ts_end:
            return None
        lookback_end = lookback.lookback_end
        chain = chain[1:]
    grammar = None
    if chain and type(chain[0]) is GrammarMaskLogitsProcessor:
        grammar = chain[0]
        tables = grammar.tables
        if grammar.legal.shape[1] != vocab:
            return None
        # The only -inf sources before the grammar mask are time-shift ids. A
        # state with a legal id outside that range can never lose every legal
        # score, so the upstream "nothing legal left" fallback cannot trigger.
        if any(all(ts_start <= i < ts_end for i in ids) for ids in tables.legal_ids.values()):
            return None
        chain = chain[1:]
    if chain:
        return None
    return float(temperature), lookback_end, grammar


def generate(model, tokenizer, model_kwargs, generate_kwargs, *, upstream):
    """Drop-in for ``compiled_decode.model_generate_compiled``."""
    if not enabled():
        return upstream(model, tokenizer, model_kwargs, generate_kwargs)
    import torch
    from transformers.modeling_outputs import BaseModelOutput
    from osuT5.osuT5.event import ContextType, EventType
    from osuT5.osuT5.inference import compiled_decode as compiled

    ids = model_kwargs.get('decoder_input_ids')
    if (compiled._capture_unsupported or ids is None or ids.shape[0] != 1
            or generate_kwargs.get('cfg_scale', 1.0) > 1 or generate_kwargs.get('timeshift_bias', 0) != 0
            or generate_kwargs.get('types_first', False) or generate_kwargs.get('top_k', 0) != 0
            or not generate_kwargs.get('do_sample', True)):
        return upstream(model, tokenizer, model_kwargs, generate_kwargs)
    fallback_args = (dict(model_kwargs), dict(generate_kwargs))

    compiled.neutralize_dynamic_rope(model)
    device = model.device
    model_kwargs = {k: v.to(device) if isinstance(v, torch.Tensor) else v for k, v in model_kwargs.items()}
    model_kwargs = {k: v.to(model.dtype) if k != 'inputs' and isinstance(v, torch.Tensor)
                    and v.dtype == torch.float32 else v for k, v in model_kwargs.items()}
    encoder_outputs = BaseModelOutput(last_hidden_state=model_kwargs.pop('encoder_outputs'))
    generate_kwargs = dict(generate_kwargs)
    temperature = generate_kwargs.pop('temperature', 1.0)
    lookback_time = generate_kwargs.pop('lookback_time', 0.0)
    lookahead_time = generate_kwargs.pop('lookahead_time', 0.0)
    context_type = generate_kwargs.pop('context_type', None)
    if context_type is not None:
        context_type = ContextType(context_type)
    top_p = generate_kwargs.pop('top_p', 0.95)
    max_length = generate_kwargs.pop('max_length', 2560)

    decoder_input_ids = model_kwargs['decoder_input_ids']
    eos_ids = frozenset(compiled.get_eos_token_id(tokenizer, lookback_time=lookback_time,
                                                  lookahead_time=lookahead_time, context_type=context_type))
    # Built through the compiled module so the grammar-mask wrapper sees this window.
    processors = compiled.build_logits_processors(
        tokenizer, generate_kwargs.get('cfg_scale', 1.0), 0, False, temperature,
        generate_kwargs.get('timing_temperature', temperature),
        generate_kwargs.get('mania_column_temperature', temperature),
        generate_kwargs.get('taiko_hit_temperature', temperature), lookback_time, device)
    vocab = model.config.vocab_size
    plan = _plan(processors, tokenizer, compiled, vocab)
    ts_start = tokenizer.event_start[EventType.TIME_SHIFT]
    ts_end = tokenizer.event_end[EventType.TIME_SHIFT]
    floor = TimeShiftFloor(ts_start, ts_end, [tokenizer.sos_id, *getattr(tokenizer, 'context_sos', {}).values()])
    if plan is None:
        # Unknown chain: keep upstream's per-step processors, only its host-state savings are lost.
        stock_mask = compiled._IncrementalMonotonicMask(tokenizer, device)
        from transformers import LogitsProcessorList
        rest = LogitsProcessorList([p for p in processors
                                    if not isinstance(p, compiled.MonotonicTimeShiftLogitsProcessor)])
        if len(rest) == len(processors):
            stock_mask = None
    else:
        step_temperature, lookback_end, grammar = plan
        if grammar is not None:
            tables = grammar.tables
            illegal = ~grammar.legal

    prompt = decoder_input_ids[0].tolist()
    prompt_len = len(prompt)
    model_max = model.config.max_target_positions
    hard_cap = min(max_length, model_max)
    positions = torch.arange(model_max, device=device)

    def run_window(cache_len):
        win_max = min(hard_cap, cache_len)
        decoder, cache = compiled._get_decoder(model, 1, generate_kwargs.get('cfg_scale', 1.0),
                                               encoder_outputs.last_hidden_state.shape, model.dtype, cache_len)
        compiled._reset_cache(cache)
        decoder.set_encoder_hidden(encoder_outputs.last_hidden_state)
        prefill_inputs = model.prepare_inputs_for_generation(
            decoder_input_ids, past_key_values=cache, use_cache=True, encoder_outputs=encoder_outputs,
            decoder_attention_mask=model_kwargs.get('decoder_attention_mask'),
            negative_prompt=model_kwargs.get('negative_prompt'),
            negative_prompt_attention_mask=model_kwargs.get('negative_prompt_attention_mask'),
            cache_position=positions[:prompt_len])
        logits = model(**prefill_inputs).logits[:, -1, :].float()

        generated = []
        cur_len = prompt_len
        truncated = True
        if plan is None:
            if stock_mask is not None:
                stock_mask.init_from_prompt(decoder_input_ids)
            id_buffer = torch.empty((1, win_max), dtype=torch.long, device=device)
            id_buffer[:, :prompt_len] = decoder_input_ids
        else:
            floor.reset(prompt)
            history = prompt[-5:]
            states, tops = [], []
        while cur_len < win_max:
            if plan is None:
                if stock_mask is not None:
                    logits = stock_mask.apply(logits)
                token = compiled._sample(rest(id_buffer[:, :cur_len], logits), True, top_p, 0, 1.0)
                id_buffer[:, cur_len] = token.reshape(1)
                if stock_mask is not None:
                    stock_mask.update(token)
            else:
                high = max(lookback_end, floor.bound())
                if grammar is None:
                    scores, _ = masked_scores(logits, step_temperature, ts_start, high)
                else:
                    state = tables.state(history)
                    scores, top = masked_scores(logits, step_temperature, ts_start, high, illegal[state])
                    states.append(state)
                    tops.append(top)
                token = sample_top_p(scores, top_p)
            token_id = token.item()
            generated.append(token_id)
            cur_len += 1
            if plan is not None:
                floor.update(token_id)
                history = history[-4:] + [token_id]
            if token_id in eos_ids:
                truncated = False
                break
            logits = decoder.replay(token, positions[cur_len - 1:cur_len])
        if plan is not None and grammar is not None and states:
            # Same counters the per-step upstream-style mask would have accumulated.
            bad = illegal[torch.tensor(states, device=device), torch.cat(tops)].long()
            first = grammar.first
            grammar.first = False
            grammar.counts += torch.stack([bad.new_tensor(len(states)), bad.sum(), bad.new_tensor(int(first)),
                                           bad[0] if first else bad.new_tensor(0), bad.new_tensor(0),
                                           bad.new_tensor(0)])
        return generated, truncated

    started = time.perf_counter()
    try:
        cache_len = compiled._pick_bucket(prompt_len, model_max)
        generated, truncated = run_window(cache_len)
        if truncated and cache_len < hard_cap:
            generated, truncated = run_window(compiled._bucket_ceil(hard_cap, model_max))
    except compiled.CaptureError as exc:
        compiled._capture_unsupported = True
        print('CUDA graph capture is not supported on this system; '
              f'falling back to the stock generate loop. ({exc})')
        return compiled.model_generate(model, tokenizer, *fallback_args)
    elapsed = time.perf_counter() - started
    result = torch.cat([decoder_input_ids.cpu(), torch.tensor([generated], dtype=torch.long)], dim=1)
    return result, compiled._build_generation_stats(result, model_kwargs, getattr(tokenizer, 'pad_id', None), elapsed)


def install():
    """Route Processor's compiled-loop calls through the lean loop; idempotent."""
    from osuT5.osuT5.inference import processor as processor_module
    current = processor_module.model_generate_compiled
    if getattr(current, '_malody_lean_decode', False):
        return current
    upstream = current

    def model_generate_compiled(model, tokenizer, model_kwargs, generate_kwargs):
        return generate(model, tokenizer, model_kwargs, generate_kwargs, upstream=upstream)
    model_generate_compiled._malody_lean_decode = True
    model_generate_compiled._malody_original = upstream
    processor_module.model_generate_compiled = model_generate_compiled
    return model_generate_compiled
