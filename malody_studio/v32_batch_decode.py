"""Lockstep batched V32 decode: one captured step serves several windows of one request.

The lean loop (``v32_fast_decode``) is latency-bound at batch 1: a decode step costs
about the same for 1 to 4 rows. Presets of one request are independent, so their
windows can share every step. Rows are left-padded to a common prompt length; the
captured graph rebuilds each row's position ids and attention mask from two static
buffers (shared ``cache_position`` and per-row ``pad_len``), so a padded row sees
exactly the positions and keys it would see alone. Per-row host state, grammar
counters, end tests and nucleus sampling are the lean loop's, with one
``torch.Generator`` per stream standing in for the global CUDA generator.
"""
from collections import OrderedDict
import time

from . import v32_fast_decode as fd

BATCH_DECODE_POLICY = 'lockstep-v1'
GRAPH_BYTES_BUDGET = 4 * 1024 ** 3   # captured step graphs + static caches kept alive at once


class Unsupported(RuntimeError):
    """A window the batched loop cannot reproduce; the stream is rerun sequentially."""


class Window:
    """Everything one decode call needs, prepared in the calling stream's thread."""

    def __init__(self, **fields):
        self.__dict__.update(fields)


def prepare_window(model, tokenizer, model_kwargs, generate_kwargs, generator):
    """Per-window setup of the lean loop. None: use the unbatched hook instead.

    Runs in the stream thread while it holds the baton, because the grammar mask
    registers its per-window processor through a module global that is only valid
    during the calling Processor.model_generate.
    """
    if not fd.enabled():
        return None
    import torch
    from osuT5.osuT5.event import ContextType, EventType
    from osuT5.osuT5.inference import compiled_decode as compiled

    ids = model_kwargs.get('decoder_input_ids')
    mask = model_kwargs.get('decoder_attention_mask')
    if (compiled._capture_unsupported or ids is None or ids.shape[0] != 1
            or generate_kwargs.get('cfg_scale', 1.0) > 1 or generate_kwargs.get('timeshift_bias', 0) != 0
            or generate_kwargs.get('types_first', False) or generate_kwargs.get('top_k', 0) != 0
            or not generate_kwargs.get('do_sample', True)
            or model_kwargs.get('negative_prompt') is not None
            or (mask is not None and not bool(mask.all()))):
        return None
    compiled.neutralize_dynamic_rope(model)
    device = model.device
    encoder = model_kwargs['encoder_outputs']
    encoder = encoder.to(device)
    if encoder.dtype == torch.float32:
        encoder = encoder.to(model.dtype)
    kwargs = dict(generate_kwargs)
    temperature = kwargs.pop('temperature', 1.0)
    lookback_time = kwargs.pop('lookback_time', 0.0)
    lookahead_time = kwargs.pop('lookahead_time', 0.0)
    context_type = kwargs.pop('context_type', None)
    if context_type is not None:
        context_type = ContextType(context_type)
    top_p = kwargs.pop('top_p', 0.95)
    max_length = kwargs.pop('max_length', 2560)
    eos_ids = frozenset(compiled.get_eos_token_id(tokenizer, lookback_time=lookback_time,
                                                  lookahead_time=lookahead_time, context_type=context_type))
    # Built through the compiled module so the grammar-mask wrapper sees this window.
    processors = compiled.build_logits_processors(
        tokenizer, generate_kwargs.get('cfg_scale', 1.0), 0, False, temperature,
        generate_kwargs.get('timing_temperature', temperature),
        generate_kwargs.get('mania_column_temperature', temperature),
        generate_kwargs.get('taiko_hit_temperature', temperature), lookback_time, device)
    vocab = model.config.vocab_size
    plan = fd._plan(processors, tokenizer, compiled, vocab)
    if plan is None:
        raise Unsupported('unrecognised logits processor chain')
    step_temperature, lookback_end, grammar = plan
    ts_start = tokenizer.event_start[EventType.TIME_SHIFT]
    ts_end = tokenizer.event_end[EventType.TIME_SHIFT]
    sos_ids = [tokenizer.sos_id, *getattr(tokenizer, 'context_sos', {}).values()]
    prompt = ids[0].tolist()
    model_max = model.config.max_target_positions
    return Window(
        model=model, tokenizer=tokenizer, model_kwargs=model_kwargs, ids=ids, prompt=prompt, n=len(prompt),
        encoder=encoder, eos=eos_ids, top_p=top_p, temperature=step_temperature, lookback_end=lookback_end,
        grammar=grammar, ts_start=ts_start, ts_end=ts_end, sos_ids=sos_ids, model_max=model_max,
        hard_cap=min(max_length, model_max), bucket=compiled._pick_bucket(len(prompt), model_max),
        generator=generator, vocab=vocab)


class Row:
    """Per-window decode state of one pass (host side)."""

    def __init__(self, window, bucket):
        self.window, self.bucket = window, bucket
        self.n = window.n
        self.win_max = min(window.hard_cap, bucket)
        self.floor = fd.TimeShiftFloor(window.ts_start, window.ts_end, window.sos_ids).reset(window.prompt)
        self.history = window.prompt[-5:]
        self.cur_len = window.n
        self.generated, self.states = [], []
        self.done = False
        self.truncated = True
        self.pad = 0


def decoder_bytes(model, batch, cache_len):
    """Static self-attention + cross-attention cache bytes of one captured decoder (estimate)."""
    try:
        import torch
        config = model.transformer.config
        head_dim = config.d_model // config.decoder_attention_heads
        per_token = 2 * config.decoder_layers * config.decoder_attention_heads * head_dim * (torch.finfo(model.dtype).bits // 8)
        return batch * per_token * (cache_len + model.config.max_source_positions)
    except Exception:
        return 1024 ** 3


class MaskedGraphDecoder:
    """Captured one-token decode step for ``batch`` rows with per-row left padding.

    Only ``token``, ``cache_position`` and ``pad_len`` change between replays; the
    row's position ids (``cache_position - pad_len``) and its 4D attention mask
    (columns in ``[pad_len, cache_position]``) are computed inside the graph.
    """

    def __init__(self, model, batch, enc_shape, cache_len):
        import torch
        from osuT5.osuT5.inference import compiled_decode as compiled
        from osuT5.osuT5.inference.cache_utils import get_cache
        compiled.neutralize_dynamic_rope(model)
        self.model, self.batch, self.cache_len = model, batch, cache_len
        device, dtype = model.device, model.dtype
        self.cache = get_cache(model, batch, 1, 1.0, max_cache_len=cache_len)
        self.token = torch.zeros((batch, 1), dtype=torch.long, device=device)
        self.cache_position = torch.zeros((1,), dtype=torch.long, device=device)
        self.pad_len = torch.zeros((batch, 1), dtype=torch.long, device=device)
        self.enc = torch.zeros((batch, *enc_shape[1:]), dtype=dtype, device=device)
        self.logits = torch.zeros((batch, model.config.vocab_size), dtype=torch.float32, device=device)
        self.positions = torch.arange(model.config.max_target_positions, device=device)
        self._cols = torch.arange(cache_len, device=device)[None, :]
        self._zero = torch.zeros((), dtype=dtype, device=device)
        self._neg = torch.tensor(torch.finfo(dtype).min, dtype=dtype, device=device)
        self._graph = None
        try:
            self._capture()
        except Exception as exc:
            raise compiled.CaptureError(str(exc)) from exc

    def _forward(self):
        import torch
        from transformers.modeling_outputs import BaseModelOutput
        position = (self.cache_position - self.pad_len).clamp_(min=0)
        valid = (self._cols >= self.pad_len) & (self._cols <= self.cache_position)
        mask = torch.where(valid, self._zero, self._neg).view(self.batch, 1, 1, self.cache_len)
        out = self.model(decoder_input_ids=self.token, encoder_outputs=BaseModelOutput(last_hidden_state=self.enc),
                         past_key_values=self.cache, use_cache=True, decoder_attention_mask=mask,
                         decoder_position_ids=position, cache_position=self.cache_position)
        self.logits.copy_(out.logits[:, -1, :].float())

    def _capture(self):
        import torch
        side = torch.cuda.Stream()
        side.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(side):
            for _ in range(3):
                self._forward()
        torch.cuda.current_stream().wait_stream(side)
        self._graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(self._graph):
            self._forward()

    def replay(self, cache_position):
        self.cache_position.copy_(cache_position)
        self._graph.replay()
        return self.logits


def _sampling_probabilities(scores, top_p):
    """Nucleus probabilities for equally configured rows, without consuming RNG."""
    import torch
    import torch.nn.functional as F
    if 0 < top_p < 1.0:
        sorted_scores, order = torch.sort(scores, descending=True, dim=-1)
        remove = F.softmax(sorted_scores, dim=-1).cumsum(dim=-1) > top_p
        sorted_scores[..., 1:].masked_fill_(remove[..., :-1], float('-inf'))
        scores.scatter_(-1, order, sorted_scores)
    return F.softmax(scores, dim=-1)


def _sample_row(scores, top_p, generator):
    """The lean sampler with a stream-local generator."""
    import torch
    return torch.multinomial(_sampling_probabilities(scores, top_p), num_samples=1, generator=generator)


class BatchEngine:
    """Decodes the waiting windows of one stream group; owns the graph cache."""

    def __init__(self):
        self._decoders = OrderedDict()
        self._bytes = 0
        self._illegal = {}
        self.stats = {'rounds': 0, 'rows': 0, 'steps': 0, 'captures': 0, 'capture_seconds': 0.,
                      'solo_passes': 0, 'retry_passes': 0}

    # -- graph cache -------------------------------------------------------------
    def decoder(self, model, batch, enc_shape, cache_len):
        key = (id(model), batch, tuple(enc_shape[1:]), cache_len)
        if key in self._decoders:
            self._decoders.move_to_end(key)
            return self._decoders[key][0]
        size = decoder_bytes(model, batch, cache_len)
        while self._decoders and self._bytes + size > GRAPH_BYTES_BUDGET:
            _, (_, evicted) = self._decoders.popitem(last=False)
            self._bytes -= evicted
        started = time.perf_counter()
        decoder = MaskedGraphDecoder(model, batch, enc_shape, cache_len)
        self.stats['capture_seconds'] = round(self.stats['capture_seconds'] + time.perf_counter() - started, 3)
        self._decoders[key] = (decoder, size)
        self._bytes += size
        self.stats['captures'] += 1
        return decoder

    def release(self):
        self._decoders.clear()
        self._bytes = 0
        self._illegal.clear()
        import torch
        torch.cuda.empty_cache()

    # -- public entry --------------------------------------------------------------
    def decode(self, requests, batch):
        """Decode every request's window; sets ``request.result`` = (tokens, stats)."""
        by_model = OrderedDict()
        for request in requests:
            window = request.window
            by_model.setdefault((id(window.model), window.temperature, window.top_p), []).append(request)
        import torch
        for group in by_model.values():
            started = time.perf_counter()
            windows = [r.window for r in group]
            with torch.no_grad():
                outcomes = self._decode_windows(windows, batch)
            elapsed = time.perf_counter() - started
            self.stats['rounds'] += 1
            self.stats['rows'] += len(group)
            for request, (generated, window) in zip(group, outcomes):
                request.result = self._result(window, generated, elapsed)

    @staticmethod
    def _result(window, generated, elapsed):
        import torch
        from osuT5.osuT5.inference import compiled_decode as compiled
        result = torch.cat([window.ids.cpu(), torch.tensor([generated], dtype=torch.long)], dim=1)
        return result, compiled._build_generation_stats(
            result, window.model_kwargs, getattr(window.tokenizer, 'pad_id', None), elapsed)

    # -- decoding ----------------------------------------------------------------------
    def _decode_windows(self, windows, batch):
        rows = [Row(w, w.bucket) for w in windows]
        self._run_rows(rows, batch)
        retry = [r for r in rows if r.truncated and r.bucket < r.window.hard_cap]
        # Same rule as the lean loop: a window that filled its bucket is rerun once on a
        # cache sized for the full budget. The generator keeps its position, as upstream's does.
        second = [Row(r.window, self._bucket_ceil(r.window.hard_cap, r.window.model_max)) for r in retry]
        if second:
            self.stats['retry_passes'] += 1
            self._run_rows(second, batch)
        final = {id(r.window): r for r in rows}
        final.update({id(r.window): r for r in second})
        return [(final[id(w)].generated, w) for w in windows]

    @staticmethod
    def _bucket_ceil(need, model_max):
        from osuT5.osuT5.inference import compiled_decode as compiled
        return compiled._bucket_ceil(need, model_max)

    def _run_rows(self, rows, batch):
        from .v32_batch_streams import graph_batch
        for row in rows:
            if row.cur_len >= row.win_max:
                row.done = True
        rows = [row for row in rows if not row.done]
        if not rows:
            return
        fit = list(rows)
        solo = []
        model_max = rows[0].window.model_max
        while fit:
            longest = max(r.n for r in fit)
            over = [r for r in fit if (longest - r.n) + r.win_max > model_max]
            if not over:
                break
            solo.extend(over)
            fit = [r for r in fit if r not in over]
        if fit:
            self._run_pass(fit, min(batch, graph_batch(len(fit))))
        for row in solo:
            self.stats['solo_passes'] += 1
            self._run_pass([row], 1)

    @staticmethod
    def prefill(model, decoder, rows, batch, longest, pad_id):
        """Left-padded prompts, per-row position ids from the model's own cumsum rule."""
        import torch
        from transformers.modeling_outputs import BaseModelOutput
        first = rows[0].window
        device, enc_shape = model.device, first.encoder.shape
        ids = torch.full((batch, longest), pad_id, dtype=torch.long)
        attention = torch.zeros((batch, longest), dtype=torch.bool)
        pads = torch.zeros((batch, 1), dtype=torch.long)
        encoder = torch.empty((batch, *enc_shape[1:]), dtype=first.encoder.dtype, device=device)
        for i in range(batch):
            row = rows[i] if i < len(rows) else rows[0]   # idle rows replay row 0
            ids[i, row.pad:] = torch.tensor(row.window.prompt, dtype=torch.long)
            attention[i, row.pad:] = True
            pads[i, 0] = row.pad
            encoder[i] = row.window.encoder[0]
        ids, attention, pads = ids.to(device), attention.to(device), pads.to(device)
        decoder.cache.reset()
        decoder.enc.copy_(encoder)
        decoder.pad_len.copy_(pads)
        inputs = model.prepare_inputs_for_generation(
            ids, past_key_values=decoder.cache, use_cache=True,
            encoder_outputs=BaseModelOutput(last_hidden_state=encoder), decoder_attention_mask=attention,
            negative_prompt=None, negative_prompt_attention_mask=None,
            cache_position=decoder.positions[:longest])
        return model(**inputs).logits[:, -1, :].float()

    def _illegal_table(self, grammar, device):
        import torch
        key = (grammar.tables.identity, str(device))
        if key not in self._illegal:
            illegal = ~grammar.legal
            self._illegal[key] = (grammar.legal, torch.cat([illegal, torch.zeros_like(illegal[:1])]))
        return self._illegal[key][1]

    def _run_pass(self, rows, batch):
        import torch
        from transformers.modeling_outputs import BaseModelOutput
        from osuT5.osuT5.inference import compiled_decode as compiled
        first = rows[0].window
        model, tokenizer = first.model, first.tokenizer
        device, model_max = model.device, first.model_max
        longest = max(r.n for r in rows)
        for row in rows:
            row.pad = longest - row.n
        need = max(r.pad + r.win_max for r in rows)
        cache_len = self._bucket_ceil(need, model_max)
        enc_shape = first.encoder.shape
        decoder = self.decoder(model, batch, enc_shape, cache_len)
        pad_id = getattr(tokenizer, 'pad_id', 0) or 0

        logits = self.prefill(model, decoder, rows, batch, longest, pad_id)

        grammar_rows = [r for r in rows if r.window.grammar is not None]
        illegal = self._illegal_table(grammar_rows[0].window.grammar, device) if grammar_rows else None
        none_state = illegal.shape[0] - 1 if illegal is not None else 0
        ts_start, ts_end = first.ts_start, first.ts_end
        ts_index = torch.arange(ts_start, ts_end, device=device)
        tops = []
        step = 0
        while True:
            temperature = first.temperature
            scores = logits / temperature
            highs = [ts_start] * batch
            states = [none_state] * batch
            for i, row in enumerate(rows):
                if row.done:
                    continue
                highs[i] = max(row.window.lookback_end, row.floor.bound())
                if row.window.grammar is not None:
                    states[i] = row.window.grammar.tables.state(row.history)
            aux = torch.tensor([highs, states], device=device)
            scores[:, ts_start:ts_end].masked_fill_(ts_index[None, :] < aux[0][:, None], float('-inf'))
            if grammar_rows:
                tops.append(scores.argmax(-1))
                scores.masked_fill_(illegal[aux[1]], float('-inf'))
            probabilities = _sampling_probabilities(scores, first.top_p)
            draws = [decoder.token[i:i + 1] for i in range(batch)]
            for i, row in enumerate(rows):
                if not row.done:
                    draws[i] = torch.multinomial(probabilities[i:i + 1], num_samples=1,
                                                generator=row.window.generator)
                    if row.window.grammar is not None:
                        row.states.append(states[i])
            chosen = torch.cat(draws)
            values = chosen.view(-1).tolist()
            for i, row in enumerate(rows):
                if row.done:
                    continue
                token = values[i]
                row.generated.append(token)
                row.cur_len += 1
                row.floor.update(token)
                row.history = row.history[-4:] + [token]
                if token in row.window.eos:
                    row.done, row.truncated = True, False
                elif row.cur_len >= row.win_max:
                    row.done = True
            if all(row.done for row in rows):
                break
            decoder.token.copy_(chosen)
            logits = decoder.replay(decoder.positions[longest + step:longest + step + 1])
            step += 1
        self.stats['steps'] += step + 1
        self._count_grammar(rows, tops, illegal, device)

    @staticmethod
    def _count_grammar(rows, tops, illegal, device):
        """Same counters the per-step upstream mask would have accumulated, per row."""
        import torch
        for i, row in enumerate(rows):
            grammar = row.window.grammar
            if grammar is None or not row.states:
                continue
            count = len(row.states)
            top = torch.stack([tops[k][i] for k in range(count)])
            bad = illegal[torch.tensor(row.states, device=device), top].long()
            first = grammar.first
            grammar.first = False
            grammar.counts += torch.stack([bad.new_tensor(count), bad.sum(), bad.new_tensor(int(first)),
                                           bad[0] if first else bad.new_tensor(0), bad.new_tensor(0),
                                           bad.new_tensor(0)])


class RngSwap:
    """Run unbatched fallback windows with the stream's generator state on the global CUDA generator."""

    def __init__(self, generator, device):
        import torch
        self.generator = generator
        self.default = torch.cuda.default_generators[device.index or 0]

    def __enter__(self):
        self.saved = self.default.get_state()
        self.default.set_state(self.generator.get_state())

    def __exit__(self, *exc):
        self.generator.set_state(self.default.get_state())
        self.default.set_state(self.saved)


def make_generator(seed, device):
    import torch
    generator = torch.Generator(device=device)
    generator.manual_seed(int(seed))
    return generator


def install_hook(stream_module):
    """Route stream threads' window decodes to the scheduler; other callers keep the old hook."""
    from osuT5.osuT5.inference import processor as processor_module
    previous = processor_module.model_generate_compiled

    def model_generate_compiled(model, tokenizer, model_kwargs, generate_kwargs):
        stream = stream_module.current_stream()
        if stream is None:
            return previous(model, tokenizer, model_kwargs, generate_kwargs)
        window = prepare_window(model, tokenizer, model_kwargs, generate_kwargs, stream.rng)
        if window is None:
            with RngSwap(stream.rng, model.device):
                return previous(model, tokenizer, model_kwargs, generate_kwargs)
        return stream.submit(window)
    model_generate_compiled._malody_batch_hook = True
    processor_module.model_generate_compiled = model_generate_compiled

    def uninstall():
        processor_module.model_generate_compiled = previous
    return uninstall
