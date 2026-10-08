"""Preserve V32's simultaneous mania holds across the pinned OSU serializer.

The upstream parser/model uses time-ordered heads and tails. Its serializer has
one pending hold, so parallel columns overwrite each other. Pair by column and
present each complete pair as an atomic block to that serializer. This changes
neither model head times nor release times and creates no new heads.
"""
from collections import Counter
from pathlib import Path
import inspect
import math


POLICY = 'mania-column-hold-pairing-v1'
TERMINAL_POLICY = 'mania-terminal-fragments-v1'
FRAGMENT_POLICY = 'mania-unlocated-fragments-v2'
TIME_POLICY = 'processor-parallel-same-time-v1'


class NativeGroupError(ValueError):
    def __init__(self, message, malformed, last_located_head, terminal_policy):
        super().__init__(message)
        self.details = {'malformed_groups': list(malformed.values()),
                        'last_located_head_group': last_located_head,
                        'terminal_policy': terminal_policy}


class NativeStreamError(ValueError):
    """The native stream has tokens but no note group the pinned reader can return."""
    def __init__(self, message, details):
        super().__init__(message)
        self.details = details


def _kind(group):
    return group.event_type.value


def _scalar(value):
    return value.item() if hasattr(value, 'item') else value


class NativeTimedEvents(list):
    """Carry the Processor's parallel clock through upstream context reduction.

    This is request-local metadata, not a global last-result cache. Never derive
    a missing clock from the group reader's default zero or a later event.
    """
    def __init__(self, events, event_times):
        super().__init__(events)
        if len(self) != len(event_times):
            raise ValueError('V32 原生事件与并行时间数量不一致')
        self.native_event_times = tuple(_scalar(t) for t in event_times)
        if not all(isinstance(t, (int, float)) and math.isfinite(t) for t in self.native_event_times):
            raise ValueError('V32 原生并行时间无效')
        self._native_signature = tuple((e.type.value, _scalar(e.value)) for e in self)

    def checked_times(self):
        if tuple((e.type.value, _scalar(e.value)) for e in self) != self._native_signature:
            raise ValueError('V32 原生事件已变化，拒绝使用过期并行时间')
        return self.native_event_times


def _install_parallel_clock(inference_module):
    processor = getattr(inference_module, 'Processor', None)
    if processor is not None and not getattr(processor, '_malody_parallel_clock', False):
        class TimedProcessor(processor):
            _malody_parallel_clock = True

            def generate(self, *args, **kwargs):
                return [(NativeTimedEvents(events, times), times)
                        for events, times in super().generate(*args, **kwargs)]
        inference_module.Processor = TimedProcessor
    merge = getattr(inference_module, 'merge_events', None)
    if merge is not None and not getattr(merge, '_malody_parallel_clock', False):
        def timed_merge(first, second):
            events, times = merge(first, second)
            return NativeTimedEvents(events, times), times
        timed_merge._malody_parallel_clock = True
        inference_module.merge_events = timed_merge


def _resolved_times(events, groups, indices, types_first, event_times):
    if event_times is None:
        return {}
    if len(event_times) != len(events) or not all(math.isfinite(float(t)) for t in event_times):
        raise ValueError('V32 原生并行时间无效或数量不一致')
    preceding, anchor = [], None
    for event in events:
        preceding.append(anchor)
        if event.type.value == 't':
            anchor = _scalar(event.value)
    resolved = {}
    for index, group in enumerate(groups):
        kind = _kind(group)
        if kind not in ('circle', 'hold_note', 'hold_note_end'):
            continue
        owned = indices[index]
        boundary = next(i for i in owned if events[i].type.value == kind)
        fields = [i for i in owned if types_first or i <= boundary]
        explicit = [i for i in fields if events[i].type.value == 't']
        time = _scalar(group.time)
        if explicit:
            if _scalar(events[explicit[-1]].value) != time:
                raise ValueError('V32 显式时间与原生并行时间冲突')
        elif (preceding[fields[0]] is not None and preceding[fields[0]] == time
              and all(event_times[i] == time for i in fields)):
            resolved[index] = {'group_index': index, 'kind': kind, 'time_ms': time,
                               'native_token_indices': list(fields),
                               'source': TIME_POLICY}
    return resolved


def pair_hold_groups(groups, keycount=4, *, invalid_terminal_groups=None, group_token_indices=None):
    """Return ordered group-index blocks, tap conversions and an audit.

    A missing tail cannot justify inventing a hold duration: retain its existing
    head as a tap, with the decision recorded. Leading tails have no playable
    head in this inference range and are recorded separately.
    """
    pending, blocks, conversions, records = {}, [], {}, []
    counts = Counter(_kind(g) for g in groups)
    invalid_terminal_groups = invalid_terminal_groups or {}
    # Fragments before the last located head are interior; none is ever located.
    last_located = max((i for i, g in enumerate(groups) if i not in invalid_terminal_groups
                        and _kind(g) in ('circle', 'hold_note')), default=-1)

    def lane(group):
        if group.x is None:
            raise ValueError('V32 长条缺少轨道坐标，无法可靠配对')
        return int(float(group.x) * keycount // 512)

    def tap(index, reason):
        conversions[index] = 'circle'
        blocks.append([index])
        g = groups[index]
        records.append({'decision': reason, 'head_ms': _scalar(g.time), 'column': lane(g),
                        'head_group_index': index,
                        'native_token_indices': list(group_token_indices[index]) if group_token_indices is not None else None})

    for index, group in enumerate(groups):
        kind = _kind(group)
        if index in invalid_terminal_groups:
            # No malformed tail may choose one of several pending columns;
            # an unlocated head may also precede a later, ambiguous tail.
            # Preserve all existing located heads as taps before continuing.
            for pending_index in list(pending.values()):
                tap(pending_index, 'terminal_fragment_pending_head_as_tap')
            pending.clear()
            records.append({'decision': 'discarded_terminal_fragment' if index > last_located else 'discarded_interior_fragment',
                            **invalid_terminal_groups[index]})
            continue
        if kind == 'hold_note':
            column = lane(group)
            if column in pending:
                tap(pending.pop(column), 'unclosed_same_column_head_as_tap')
            pending[column] = index
        elif kind == 'hold_note_end':
            column = lane(group)
            head_index = pending.pop(column, None)
            if head_index is None:
                records.append({'decision': 'tail_without_head', 'tail_ms': _scalar(group.time),
                                'column': column, 'tail_group_index': index})
            elif group.time <= groups[head_index].time:
                tap(head_index, 'nonpositive_hold_as_tap')
                records.append({'decision': 'invalid_tail', 'tail_ms': _scalar(group.time),
                                'column': column, 'tail_group_index': index})
            else:
                blocks.append([head_index, index])
                records.append({'decision': 'paired', 'head_ms': _scalar(groups[head_index].time),
                                'tail_ms': _scalar(group.time), 'column': column,
                                'head_group_index': head_index, 'tail_group_index': index})
        else:
            blocks.append([index])
    for index in pending.values():
        tap(index, 'head_without_tail_as_tap')
    blocks.sort(key=lambda block: (groups[block[0]].time, block[0]))
    audit = {'policy': POLICY, 'model_heads': counts['circle'] + counts['hold_note'],
             'model_taps': counts['circle'], 'model_hold_heads': counts['hold_note'],
             'paired_holds': sum(r['decision'] == 'paired' for r in records),
             'hold_heads_as_taps': len(conversions),
             'tails_without_heads': sum(r['decision'] == 'tail_without_head' for r in records),
             'created_heads': 0, 'records': records}
    discarded_heads = sum(_kind(groups[i]) in ('circle', 'hold_note') for i in invalid_terminal_groups)
    audit.update(discarded_model_heads=discarded_heads,
                 kept_model_heads=audit['model_heads']-discarded_heads,
                 discarded_terminal_groups=sum(i > last_located for i in invalid_terminal_groups),
                 discarded_interior_groups=sum(i < last_located for i in invalid_terminal_groups),
                 terminal_fragment_policy=TERMINAL_POLICY if invalid_terminal_groups else None)
    return blocks, conversions, audit


def _lane_centres(keycount):
    return {int((column + .5) * 512 / keycount) for column in range(keycount)}


def _validate_native_groups(events, groups, indices, types_first, terminal_policy, resolved_times=None, keycount=4):
    if terminal_policy not in (None, TERMINAL_POLICY, FRAGMENT_POLICY):
        raise ValueError('V32 终末事件策略版本不受支持')
    malformed, last_located_head = {}, -1
    centres = _lane_centres(keycount)
    for index, group in enumerate(groups):
        kind = _kind(group)
        if kind not in ('circle', 'hold_note', 'hold_note_end'):
            continue
        owned = indices[index]
        # get_groups appends an untyped final suffix to its previous group.
        # Those later tokens cannot provide missing fields to a types-last
        # note, whose actual fields end at its own type token.
        boundary = next(i for i in owned if events[i].type.value == kind)
        fields = [events[i] for i in owned if types_first or i <= boundary]
        kinds = {event.type.value for event in fields}
        missing = []
        if ('t' not in kinds and index not in (resolved_times or {})) or not math.isfinite(float(_scalar(group.time))):
            missing.append('时间')
        # A lane token outside 0..keycount-1 converts to an off-grid pos_x; it is no lane.
        if ('pos_x' not in kinds or group.x is None or
                not math.isfinite(float(_scalar(group.x))) or float(_scalar(group.x)) not in centres):
            missing.append('轨道坐标')
        if missing:
            malformed[index] = {'group_index': index, 'kind': kind,
                                'missing_fields': missing,
                                'native_token_indices': list(owned)}
        elif kind in ('circle', 'hold_note'):
            last_located_head = index
    if not malformed:
        return {}
    if terminal_policy is None:
        raise NativeGroupError('V32 音符缺少' + '、'.join(next(iter(malformed.values()))['missing_fields']) + '，禁止推断',
                               malformed, last_located_head, terminal_policy)
    if last_located_head < 0 or (terminal_policy != FRAGMENT_POLICY and any(index <= last_located_head for index in malformed)):
        raise NativeGroupError('V32 非终末音符字段缺失，保留原生事件并拒绝打包',
                               malformed, last_located_head, terminal_policy)
    head_count = sum(_kind(group) in ('circle', 'hold_note') for group in groups)
    limit = min(8, max(1, int(head_count * .01)))
    if len(malformed) > limit or len(malformed) / max(1, head_count) > .01:
        raise NativeGroupError('V32 异常片段数量超出保守上限，拒绝打包' if terminal_policy == FRAGMENT_POLICY
                               else 'V32 终末异常片段数量超出保守上限，拒绝打包',
                               malformed, last_located_head, terminal_policy)
    return malformed


def pack_mania_events(events, *, group_reader, event_factory, event_types,
                      types_first=False, keycount=4, terminal_policy=None, event_times=None):
    if event_times is None and isinstance(events, NativeTimedEvents):
        event_times = events.checked_times()
    kwargs = {'types_first': types_first}
    if event_times is not None:
        kwargs['event_times'] = event_times
    try:
        groups, indices = group_reader(events, **kwargs)
    except IndexError as error:
        # Pinned get_groups indexes group_indices[-1] for trailing tokens; with no
        # type event at all (e.g. a lone orphan hold_note_sustain) that list is empty.
        # An empty stream is valid and never reaches here; a stream of only
        # fragments contains no note, so it is rejected for this request alone.
        counts = Counter(event.type.value for event in events)
        raise NativeStreamError('V32 输出只有零散事件标记、没有任何音符，无法解析为谱面（已保留原生事件）',
                                {'reason': 'fragments_without_any_note_group', 'event_count': len(events),
                                 'token_types': dict(counts)}) from error
    resolved = _resolved_times(events, groups, indices, types_first, event_times)
    malformed = _validate_native_groups(events, groups, indices, types_first, terminal_policy, resolved, keycount)
    blocks, conversions, audit = pair_hold_groups(groups, keycount, invalid_terminal_groups=malformed,
                                                 group_token_indices=indices)
    audit['terminal_fragment_policy'] = terminal_policy
    audit['parallel_time_policy'] = TIME_POLICY if event_times is not None else None
    audit['materialized_times'] = list(resolved.values())
    packed = []
    for block in blocks:
        for index in block:
            if index in resolved and not types_first:
                packed.append(event_factory(event_types.TIME_SHIFT, groups[index].time))
            for event_index in indices[index]:
                event = events[event_index]
                if index in conversions and event.type == event_types.HOLD_NOTE:
                    event = event_factory(event_types.CIRCLE, event.value)
                packed.append(event)
                if index in resolved and types_first and event.type == groups[index].event_type:
                    packed.append(event_factory(event_types.TIME_SHIFT, groups[index].time))
    return packed, audit


def install(inference_module, *, terminal_policy=None):
    """Install once in the existing worker, without modifying pinned vendor files."""
    _install_parallel_clock(inference_module)
    original = inference_module.Postprocessor
    if getattr(original, '_malody_column_holds', False):
        original._malody_terminal_policy = terminal_policy
        return original
    upstream = inspect.getmodule(original)

    class ColumnHoldPostprocessor(original):
        _malody_column_holds = True
        _malody_terminal_policy = None

        def __init__(self, args, *a, **kw):
            super().__init__(args, *a, **kw)
            self._malody_output = Path(args.output_path)
            self._malody_keycount = args.keycount or 4
            self._malody_native_policy = type(self)._malody_terminal_policy

        def generate(self, events, beatmap_config, timing=None):
            if beatmap_config.mode != 3:
                return super().generate(events, beatmap_config, timing)
            from malody_studio.resident import atomic
            event_times = events.checked_times() if isinstance(events, NativeTimedEvents) else None
            self._malody_output.mkdir(parents=True, exist_ok=True)
            # Save the untouched native stream before conversion. A failing
            # serializer must leave the same evidence as a successful attempt.
            atomic(self._malody_output / 'model-events.json', {
                'policy': POLICY,
                'rhythm_metadata_version': 'v32-rhythm-v1',
                'types_first': self.types_first,
                'keycount': self._malody_keycount,
                'events': [[event.type.value, _scalar(event.value)] for event in events],
                'event_times': list(event_times) if event_times is not None else None,
                'parallel_time_policy': TIME_POLICY if event_times is not None else None,
                'timing': [point.pack() for point in timing] if timing else [],
            })
            try:
                packed, audit = pack_mania_events(
                    events, group_reader=upstream.get_groups, event_factory=upstream.Event,
                    event_types=upstream.EventType, types_first=self.types_first,
                    keycount=self._malody_keycount, terminal_policy=self._malody_native_policy)
                result = super().generate(packed, beatmap_config, timing)
            except Exception as error:
                atomic(self._malody_output / 'serialization-error.json', {
                    'policy': POLICY, 'error_type': type(error).__name__,
                    'error': str(error), 'native_events': 'model-events.json',
                    'types_first': self.types_first, 'keycount': self._malody_keycount,
                    'event_count': len(events),
                    'parallel_time_count': len(event_times) if event_times is not None else None,
                    'details': getattr(error, 'details', None),
                })
                raise
            body = result.partition('[HitObjects]')[2]
            actual = len([line for line in body.splitlines() if line.strip()])
            audit['serialized_heads'] = actual
            if actual != audit['kept_model_heads']:
                raise ValueError(f'V32 音符头转换不完整：保留模型 {audit["kept_model_heads"]}，谱面 {actual}')
            atomic(self._malody_output / 'v32-event-audit.json', audit)
            return result

    ColumnHoldPostprocessor._malody_terminal_policy = terminal_policy
    inference_module.Postprocessor = ColumnHoldPostprocessor
    return ColumnHoldPostprocessor
