"""Host-side scheduling for lockstep V32 decode streams (no torch, no vendor imports).

Each preset of one worker request runs its unmodified per-preset body in its own
thread. A baton lets exactly one of them (or the scheduler) execute Python at a
time: a stream yields only when it blocks in the decode hook waiting for its
window, or when it ends. When no stream of the group is runnable, the scheduler
hands every waiting window to the batched decoder and wakes the streams.
"""
import contextvars
import math
import threading

STREAM_LIMIT = 16
DEFAULT_STREAMS = 8
GRAPH_BATCHES = (1, 2, 4, 8, 16)

_local = threading.local()


def batch_identity(settings):
    """Policy identity usable inside the isolated model environment."""
    import hashlib
    from pathlib import Path
    count = clamp_streams(settings.get('parallel_streams', 0))
    if not count:
        return None
    from .v32_batch_decode import BATCH_DECODE_POLICY
    root = Path(__file__).resolve().parents[1]
    paths = ('malody_studio/v32_batch_decode.py', 'malody_studio/v32_batch_streams.py',
             'malody_studio/v32_fast_decode.py', 'malody_studio/v32_grammar_mask.py',
             'malody_studio/v32_generation_diagnostics.py', 'malody_studio/v32_attempt_diagnostics.py',
             'tools/mapperatorinator_worker.py')
    digest = hashlib.sha256()
    for name in paths:
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return dict(policy=BATCH_DECODE_POLICY, parallel_streams=count, code_sha256=digest.hexdigest(),
                bit_exact_to_sequential=False)


def current_stream():
    return getattr(_local, 'stream', None)


def clamp_streams(value):
    """Requested parallel streams -> usable count; 0 means sequential."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not math.isfinite(value):
        return 0
    count = int(value)
    return 0 if count < 2 else min(count, STREAM_LIMIT)


def graph_batch(count):
    """Smallest captured batch size that holds ``count`` rows."""
    return next(size for size in GRAPH_BATCHES if size >= count)


def plan_groups(entries, limit):
    """Cut (index, span, sr) entries into balanced groups of at most ``limit``.

    Sorted by (span, sr, index) so streams with similar window counts and density
    share a batch; balanced sizes avoid a lone trailing stream.
    """
    ordered = sorted(entries, key=lambda e: (e[1], e[2], e[0]))
    if not ordered:
        return []
    count = -(-len(ordered) // max(1, limit))
    base, extra = divmod(len(ordered), count)
    groups, cursor = [], 0
    for index in range(count):
        size = base + (1 if index < extra else 0)
        groups.append([e[0] for e in ordered[cursor:cursor + size]])
        cursor += size
    return groups


class WindowRequest:
    __slots__ = ('window', 'stream', 'result', 'error')

    def __init__(self, window, stream):
        self.window, self.stream, self.result, self.error = window, stream, None, None


NEW, READY, RUNNING, WAITING, DONE = 'new', 'ready', 'running', 'waiting', 'done'


class Stream:
    """One preset body in its own thread; runs only while holding the baton."""

    def __init__(self, index, body, scheduler, context=None, name=None):
        self.index, self.body, self.scheduler = index, body, scheduler
        self.state = NEW
        self.outcome = self.error = self.request = None
        self.rng = None          # per-stream sampler state owned by the GPU layer
        self._go = threading.Event()
        context = context if context is not None else contextvars.copy_context()
        self.thread = threading.Thread(target=context.run, args=(self._main,), daemon=True,
                                       name=name or f'v32-stream-{index}')

    def _main(self):
        _local.stream = self
        self._go.wait()
        self._go.clear()
        try:
            self.outcome = self.body(self)
        except BaseException as exc:
            self.error = exc
        finally:
            self.state = DONE
            self.scheduler._idle.set()

    def submit(self, window):
        """Called from the decode hook in this stream's thread; blocks until decoded."""
        request = self.request = WindowRequest(window, self)
        self.state = WAITING
        self.scheduler._idle.set()
        self._go.wait()
        self._go.clear()
        self.request = None
        if request.error is not None:
            raise request.error
        return request.result


class Scheduler:
    """Run one group of streams in lockstep rounds on the calling thread."""

    def __init__(self, decode):
        self.decode = decode     # decode(list[WindowRequest]) sets .result or .error per request
        self._idle = threading.Event()

    def _resume(self, stream):
        self._idle.clear()
        stream.state = RUNNING
        stream._go.set()
        self._idle.wait()

    def run(self, streams):
        for stream in streams:
            stream.state = READY
            stream.thread.start()
        while True:
            ready = next((s for s in streams if s.state == READY), None)
            if ready is not None:
                self._resume(ready)
                continue
            waiting = [s for s in streams if s.state == WAITING]
            if not waiting:
                break
            requests = [s.request for s in waiting]
            try:
                self.decode(requests)
            except BaseException as exc:
                for request in requests:
                    if request.result is None and request.error is None:
                        request.error = exc
            for request in requests:
                if request.result is None and request.error is None:
                    request.error = RuntimeError('batched decode returned no result')
            for stream in waiting:
                stream.state = READY
        for stream in streams:
            stream.thread.join()


def is_cuda_oom(exc):
    pending, seen = [exc], set()
    while pending:
        error = pending.pop()
        if error is None or id(error) in seen:
            continue
        seen.add(id(error))
        if 'out of memory' in str(error).lower() or type(error).__name__ == 'OutOfMemoryError':
            return True
        pending.extend((error.__cause__, error.__context__))
    return False


def run_lockstep(entries, limit, *, body, decode, is_oom=is_cuda_oom, context_factory=None):
    """Run ``entries`` [(index, key, span, sr)] in lockstep groups.

    ``body(stream)`` runs one preset (``stream.index``) and returns its outcome; any
    exception marks that stream failed. A CUDA OOM halves the group limit and retries
    the failed streams batched; every other failure (and a limit below two) is handed
    back in ``sequential`` for the caller's existing one-at-a-time code.
    Returns dict(outcomes, sequential, groups, fallbacks).
    """
    keys = {e[0]: e[1] for e in entries}
    outcomes, fallbacks, groups_record, sequential = {}, [], [], []
    pending, limit_now = list(entries), limit
    while pending and limit_now >= 2:
        groups = plan_groups([(e[0], e[2], e[3]) for e in pending], limit_now)
        groups_record.append({'limit': limit_now, 'groups': [[keys[i] for i in g] for g in groups]})
        oom_failed = []
        for group in groups:
            scheduler = Scheduler(lambda requests, size=graph_batch(len(group)): decode(requests, size))
            streams = [Stream(i, body, scheduler, context_factory() if context_factory else None) for i in group]
            scheduler.run(streams)
            for stream in streams:
                if stream.error is None:
                    outcomes[stream.index] = stream.outcome
                    continue
                oom = is_oom(stream.error)
                fallbacks.append({'key': keys[stream.index], 'error_type': type(stream.error).__name__,
                                  'error': str(stream.error)[:300], 'cuda_oom': oom,
                                  'action': 'retry_smaller_batch' if oom and limit_now >= 4 else 'sequential'})
                (oom_failed if oom else sequential).append(stream.index)
        by_index = {e[0]: e for e in pending}
        pending = [by_index[i] for i in oom_failed]
        if pending:
            limit_now //= 2
    sequential.extend(e[0] for e in pending)
    return {'outcomes': outcomes, 'sequential': sorted(sequential), 'groups': groups_record,
            'fallbacks': fallbacks}


class ProcessorRouter:
    """Stand-in for ``inference.Processor`` while streams run.

    ``capture_processor_diagnostics`` swaps the module's Processor class per attempt;
    with interleaved streams that must be per thread. The router resolves the class
    of the calling thread, so each stream sees only its own wrapper.
    """
    _malody_stream_router = True

    def __init__(self, base):
        self.base = base
        self._local = threading.local()

    def resolve(self):
        return getattr(self._local, 'cls', None) or self.base

    def push(self, cls):
        previous = getattr(self._local, 'cls', None)
        self._local.cls = cls
        return previous

    def pop(self, previous):
        self._local.cls = previous

    def __call__(self, *args, **kwargs):
        return self.resolve()(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.resolve(), name)


def batching_refusal(request, lean_enabled=True):
    """Why this request must run its presets one after another (None: it may batch).

    Only direct-policy requests batch: any density/retry/timing-reference route keeps
    the sequential loop whose later presets depend on earlier results or recovery state.
    """
    presets = request.get('presets') or []
    if request.get('direct_inference') is not True:
        return 'not_direct_policy'
    if len(presets) < 2:
        return 'single_preset'
    if not lean_enabled:
        return 'lean_decode_disabled'
    cfg = request.get('cfg_scale', 1)
    if isinstance(cfg, bool) or not isinstance(cfg, (int, float)) or not math.isfinite(cfg) or cfg <= 0:
        return 'unsupported_cfg_scale'
    if cfg > 1:
        return 'cfg_scale_above_1'
    if request.get('types_first', False) or request.get('timeshift_bias', 0) != 0 or request.get('top_k', 0) != 0:
        return 'unsupported_sampling_options'
    if request.get('do_sample', True) is not True:
        return 'unsupported_sampling_options'
    if request.get('action') == 'timing_only':
        return 'timing_only'
    for field in ('timing_reference', 'timing_fallback_reference', 'timing_fallback_identity',
                  'experimental_parameter_snapshot'):
        if request.get(field):
            return 'legacy_route_' + field
    for preset in presets:
        for field in ('retry_sections', 'density_policy', 'density_rounds', 'density_sections', 'retry_min_heads',
                      'retry_condition'):
            if preset.get(field):
                return 'legacy_route_' + field
    return None
