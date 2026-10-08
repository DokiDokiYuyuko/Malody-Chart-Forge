from dataclasses import dataclass
from enum import Enum
from types import SimpleNamespace

import pytest

from malody_studio.v32_event_serialization import pack_mania_events


class Kind(Enum):
    CIRCLE = 'circle'
    HOLD_NOTE = 'hold_note'
    HOLD_NOTE_END = 'hold_note_end'
    TIME_SHIFT = 't'
    POS_X = 'pos_x'


@dataclass
class Event:
    type: Kind
    value: int = 0


def reader(events, *, types_first=False, event_times=None):
    groups, indices = [], []
    current, owned = SimpleNamespace(event_type=None, time=0, x=None), []
    for index, event in enumerate(events):
        if types_first and event.type in (Kind.CIRCLE, Kind.HOLD_NOTE, Kind.HOLD_NOTE_END):
            if current.event_type is not None:
                groups.append(current); indices.append(owned)
                current, owned = SimpleNamespace(event_type=None, time=0, x=None), []
            current.event_type = event.type
            if event_times is not None:
                current.time = event_times[index]
        owned.append(index)
        if event.type == Kind.TIME_SHIFT:
            current.time = event.value
        elif event.type == Kind.POS_X:
            current.x = event.value
        if not types_first and event.type in (Kind.CIRCLE, Kind.HOLD_NOTE, Kind.HOLD_NOTE_END):
            current.event_type = event.type
            if event_times is not None:
                current.time = event_times[index]
            groups.append(current); indices.append(owned)
            current, owned = SimpleNamespace(event_type=None, time=0, x=None), []
    if current.event_type is not None:
        groups.append(current); indices.append(owned)
    elif owned and indices:
        indices[-1].extend(owned)
    return groups, indices


def pack(events, **kw):
    return pack_mania_events(events, group_reader=reader, event_factory=Event, event_types=Kind, **kw)


def taps(count=300, *, types_first=False):
    result = []
    for i in range(count):
        group = [Event(Kind.TIME_SHIFT, i * 20), Event(Kind.POS_X, 64), Event(Kind.CIRCLE)]
        result.extend([group[-1], *group[:-1]] if types_first else group)
    return result


def test_missing_tap_lane_never_reaches_upstream_last_position_fallback():
    with pytest.raises(ValueError, match='轨道坐标'):
        pack([Event(Kind.TIME_SHIFT, 100), Event(Kind.CIRCLE)])


def test_missing_time_is_not_the_group_readers_default_zero_or_trailing_time():
    with pytest.raises(ValueError, match='时间'):
        pack([Event(Kind.POS_X, 64), Event(Kind.HOLD_NOTE), Event(Kind.TIME_SHIFT, 200)])


@pytest.mark.parametrize('types_first', [False, True])
def test_terminal_fragment_quarantines_pending_holds_without_guessing_a_tail(types_first):
    events = taps(types_first=types_first)
    suffix = [Event(Kind.TIME_SHIFT, 7000), Event(Kind.POS_X, 192), Event(Kind.HOLD_NOTE),
              Event(Kind.HOLD_NOTE_END),  # no time or lane; cannot choose a pending head
              Event(Kind.TIME_SHIFT, 9000), Event(Kind.POS_X, 192), Event(Kind.HOLD_NOTE_END)]
    if types_first:
        suffix = [suffix[2], suffix[0], suffix[1], suffix[3], suffix[6], suffix[4], suffix[5]]
    events += suffix
    original = [(event.type, event.value) for event in events]
    packed, audit = pack(events, types_first=types_first, terminal_policy='mania-terminal-fragments-v1')
    assert audit['model_heads'] == audit['kept_model_heads'] == 301
    assert audit['discarded_model_heads'] == 0
    assert audit['discarded_terminal_groups'] == 1
    assert audit['paired_holds'] == 0 and audit['hold_heads_as_taps'] == 1
    conversion = next(record for record in audit['records'] if record['decision'] == 'terminal_fragment_pending_head_as_tap')
    assert conversion['head_group_index'] == 300
    assert conversion['native_token_indices'] == [900, 901, 902]
    assert sum(event.type == Kind.CIRCLE for event in packed) == 301
    assert not any(event.type == Kind.HOLD_NOTE_END for event in packed)
    assert original == [(event.type, event.value) for event in events]


def test_terminal_bad_head_is_reported_but_an_interior_bad_head_or_large_loss_fails():
    events = taps() + [Event(Kind.HOLD_NOTE)]
    _, audit = pack(events, terminal_policy='mania-terminal-fragments-v1')
    assert audit['model_heads'] == 301 and audit['kept_model_heads'] == 300
    assert audit['discarded_model_heads'] == 1
    assert audit['created_heads'] == 0
    with pytest.raises(ValueError, match='终末'):
        pack(events + [Event(Kind.TIME_SHIFT, 8000), Event(Kind.POS_X, 64), Event(Kind.CIRCLE)],
             terminal_policy='mania-terminal-fragments-v1')
    with pytest.raises(ValueError, match='数量'):
        pack(taps(20) + [Event(Kind.HOLD_NOTE)], terminal_policy='mania-terminal-fragments-v1')


def test_resident_request_policy_resets_before_the_next_simple_request(tmp_path, monkeypatch):
    from malody_studio import v32_event_serialization as adapter

    class Postprocessor:
        def __init__(self, args):
            self.types_first = False

        def generate(self, events, *_args):
            return '[HitObjects]\n' + '64,192,100,1,0,0:0:0:0:\n' * sum(e.type == Kind.CIRCLE for e in events)

    upstream = SimpleNamespace(get_groups=reader, Event=Event, EventType=Kind)
    monkeypatch.setattr(adapter.inspect, 'getmodule', lambda _cls: upstream)
    module = SimpleNamespace(Postprocessor=Postprocessor)
    events = taps() + [Event(Kind.HOLD_NOTE)]
    adapter.install(module, terminal_policy='mania-terminal-fragments-v1')
    advanced = module.Postprocessor(SimpleNamespace(output_path=tmp_path / 'advanced', keycount=4))
    assert advanced.generate(events, SimpleNamespace(mode=3)).count('64,192,100,1,0') == 300
    adapter.install(module)
    simple = module.Postprocessor(SimpleNamespace(output_path=tmp_path / 'simple', keycount=4))
    with pytest.raises(ValueError, match='时间'):
        simple.generate(events, SimpleNamespace(mode=3))
    assert advanced._malody_native_policy == 'mania-terminal-fragments-v1'
    assert simple._malody_native_policy is None


@pytest.mark.parametrize('types_first', [False, True])
def test_parallel_clock_preserves_same_time_tail_without_changing_native_events(types_first):
    from malody_studio.v32_event_serialization import NativeTimedEvents
    # The actual failure was a tail with explicit column but no repeated t,
    # followed by later heads. Both the Processor and preceding t say 269957.
    groups = [[Event(Kind.TIME_SHIFT, 269000), Event(Kind.POS_X, 64), Event(Kind.HOLD_NOTE)],
              [Event(Kind.TIME_SHIFT, 269957), Event(Kind.POS_X, 192), Event(Kind.CIRCLE)],
              [Event(Kind.POS_X, 64), Event(Kind.HOLD_NOTE_END)],
              [Event(Kind.TIME_SHIFT, 270037), Event(Kind.POS_X, 320), Event(Kind.CIRCLE)]]
    if types_first:
        groups = [[g[-1], *g[:-1]] for g in groups]
    events = NativeTimedEvents([e for g in groups for e in g],
                              [t for g,t in zip(groups,[269000,269957,269957,270037]) for _ in g])
    original = [(e.type, e.value) for e in events]
    packed, audit = pack(events, types_first=types_first)
    actual, _ = reader(packed, types_first=types_first)
    assert [(g.event_type.value,g.time,g.x) for g in actual] == [
        ('hold_note',269000,64),('hold_note_end',269957,64),
        ('circle',269957,192),('circle',270037,320)]
    assert audit['paired_holds'] == 1 and audit['created_heads'] == 0
    assert audit['kept_model_heads'] == 3
    assert audit['materialized_times'][0]['native_token_indices'] == [6,7]
    assert audit['materialized_times'][0]['time_ms'] == 269957
    assert original == [(e.type, e.value) for e in events]
    with pytest.raises(ValueError, match='时间'):
        pack(list(events), types_first=types_first)


def test_parallel_time_requires_an_existing_anchor_and_explicit_lane():
    from malody_studio.v32_event_serialization import NativeTimedEvents
    with pytest.raises(ValueError, match='时间'):
        pack(NativeTimedEvents([Event(Kind.POS_X,64),Event(Kind.CIRCLE)], [100,100]))
    good = [Event(Kind.TIME_SHIFT,100),Event(Kind.POS_X,64),Event(Kind.CIRCLE)]
    with pytest.raises(ValueError, match='时间'):
        pack(NativeTimedEvents(good+[Event(Kind.POS_X,192),Event(Kind.CIRCLE)], [100]*3+[200]*2))
    with pytest.raises(ValueError, match='轨道坐标'):
        pack(NativeTimedEvents(good+[Event(Kind.HOLD_NOTE_END)], [100]*4))
    with pytest.raises(ValueError, match='冲突'):
        pack(NativeTimedEvents(good, [200]*3))
    stale = NativeTimedEvents(good,[100]*3)
    stale[0] = Event(Kind.TIME_SHIFT,200)
    with pytest.raises(ValueError, match='过期'):
        pack(stale)
    with pytest.raises(ValueError, match='数量'):
        NativeTimedEvents(good,[100])


def test_install_retains_parallel_clock_for_one_or_merged_contexts_without_request_leaks(monkeypatch):
    from functools import reduce
    from malody_studio import v32_event_serialization as adapter
    class Processor:
        def generate(self, pairs):
            return pairs
    class Postprocessor:
        pass
    def merge(first,second):
        ordered = sorted(zip(first[1]+second[1],first[0]+second[0]), key=lambda pair: pair[0])
        return [e for _,e in ordered], [t for t,_ in ordered]
    monkeypatch.setattr(adapter.inspect,'getmodule',lambda _cls: SimpleNamespace())
    module = SimpleNamespace(Processor=Processor,Postprocessor=Postprocessor,merge_events=merge)
    adapter.install(module)
    wrapper = module.Processor
    adapter.install(module)
    assert module.Processor is wrapper
    events = [Event(Kind.TIME_SHIFT,100),Event(Kind.POS_X,64),Event(Kind.CIRCLE)]
    pairs = module.Processor().generate([(events,[100]*3)])
    native,_ = reduce(module.merge_events,pairs)
    assert native.checked_times() == (100,100,100)
    assert native[0] is events[0]
    extra = [Event(Kind.TIME_SHIFT,200),Event(Kind.POS_X,192),Event(Kind.CIRCLE)]
    merged,_ = reduce(module.merge_events,module.Processor().generate([(events,[100]*3),(extra,[200]*3)]))
    assert merged.checked_times() == (100,100,100,200,200,200)
    next_request,_ = module.Processor().generate([(extra,[200]*3)])[0]
    assert next_request.checked_times() == (200,200,200)


FRAGMENT = 'mania-unlocated-fragments-v2'


def interior_stream(*, bare=1, types_first=False):
    """Located head, bare hold_note fragment(s), a later tail on that lane, more taps."""
    def note(kind, t=None, x=None):
        group = ([Event(Kind.TIME_SHIFT, t)] if t is not None else []) + ([Event(Kind.POS_X, x)] if x is not None else []) + [Event(kind)]
        return [group[-1], *group[:-1]] if types_first else group
    events = taps(300, types_first=types_first)
    events += note(Kind.HOLD_NOTE, 7000, 64)
    for _ in range(bare):
        events += note(Kind.HOLD_NOTE)
    events += note(Kind.HOLD_NOTE_END, 9000, 64)
    for i in range(100):
        events += note(Kind.CIRCLE, 9100 + i * 20, 192)
    return events


@pytest.mark.parametrize('types_first', [False, True])
def test_interior_bare_hold_is_discarded_under_fragment_policy_without_inference(types_first):
    events = interior_stream(types_first=types_first)
    original = [(e.type, e.value) for e in events]
    packed, audit = pack(events, types_first=types_first, terminal_policy=FRAGMENT)
    assert audit['model_heads'] == 402 and audit['kept_model_heads'] == 401
    assert audit['discarded_model_heads'] == 1 and audit['created_heads'] == 0
    assert audit['terminal_fragment_policy'] == FRAGMENT
    assert audit['discarded_interior_groups'] == 1 and audit['discarded_terminal_groups'] == 0
    decisions = [r['decision'] for r in audit['records']]
    assert decisions.count('discarded_interior_fragment') == 1
    assert 'discarded_terminal_fragment' not in decisions
    discarded = next(r for r in audit['records'] if r['decision'] == 'discarded_interior_fragment')
    assert discarded['group_index'] == 301 and discarded['missing_fields'] == ['时间', '轨道坐标']
    pending = next(r for r in audit['records'] if r['decision'] == 'terminal_fragment_pending_head_as_tap')
    assert pending['head_group_index'] == 300 and pending['column'] == 0
    assert audit['paired_holds'] == 0 and audit['hold_heads_as_taps'] == 1
    assert audit['tails_without_heads'] == 1 and decisions.count('tail_without_head') == 1
    assert sum(e.type == Kind.CIRCLE for e in packed) == 401
    assert not any(e.type in (Kind.HOLD_NOTE, Kind.HOLD_NOTE_END) for e in packed) or \
        not any(e.type == Kind.HOLD_NOTE for e in packed)
    assert original == [(e.type, e.value) for e in events]


@pytest.mark.parametrize('policy', [None, 'mania-terminal-fragments-v1'])
def test_interior_bare_hold_still_rejected_by_strict_and_terminal_policies(policy):
    with pytest.raises(ValueError):
        pack(interior_stream(), terminal_policy=policy)


def test_interior_fragments_over_the_conservative_cap_still_fail():
    with pytest.raises(ValueError, match='数量'):
        pack(interior_stream(bare=5), terminal_policy=FRAGMENT)  # 5 > int(406*.01)=4
    pack(interior_stream(bare=4), terminal_policy=FRAGMENT)
    with pytest.raises(ValueError, match='数量'):
        pack(taps(20) + [Event(Kind.HOLD_NOTE)] + taps(20), terminal_policy=FRAGMENT)


def test_fragment_policy_needs_one_located_head():
    with pytest.raises(ValueError):
        pack([Event(Kind.HOLD_NOTE)], terminal_policy=FRAGMENT)


def test_terminal_fragment_under_fragment_policy_behaves_as_before():
    events = taps() + [Event(Kind.HOLD_NOTE)]
    legacy, legacy_audit = pack(events, terminal_policy='mania-terminal-fragments-v1')
    packed, audit = pack(events, terminal_policy=FRAGMENT)
    assert [(e.type, e.value) for e in packed] == [(e.type, e.value) for e in legacy]
    assert audit['kept_model_heads'] == 300 and audit['discarded_terminal_groups'] == 1
    assert audit['discarded_interior_groups'] == 0
    assert any(r['decision'] == 'discarded_terminal_fragment' for r in audit['records'])
    assert audit['terminal_fragment_policy'] == FRAGMENT
    assert legacy_audit['terminal_fragment_policy'] == 'mania-terminal-fragments-v1'


def test_unsupported_policy_is_rejected():
    with pytest.raises(ValueError, match='不受支持'):
        pack(taps(), terminal_policy='mania-unknown')


@pytest.mark.parametrize('x', [-48, 832, 960, 100, 512, 65])
def test_pos_x_outside_the_legal_lane_centres_is_a_malformed_group_not_a_lane(x):
    bad = [Event(Kind.TIME_SHIFT, 7000), Event(Kind.POS_X, x), Event(Kind.CIRCLE)]
    with pytest.raises(ValueError, match='轨道坐标'):
        pack(taps() + bad)
    packed, audit = pack(taps() + bad, terminal_policy='mania-unlocated-fragments-v2')
    assert audit['model_heads'] == 301 and audit['kept_model_heads'] == 300 and audit['discarded_model_heads'] == 1
    assert audit['created_heads'] == 0 and not any(event.value == x for event in packed if event.type == Kind.POS_X)
    # an interior bad lane follows the same fragment policy: discarded under the cap
    packed, audit = pack(taps(150) + bad + [Event(Kind.TIME_SHIFT, 8000), Event(Kind.POS_X, 64), Event(Kind.CIRCLE)],
                         terminal_policy='mania-unlocated-fragments-v2')
    assert audit['discarded_interior_groups'] == 1 and audit['kept_model_heads'] == 151


@pytest.mark.parametrize('x', [64, 192, 320, 448])
def test_every_legal_4k_lane_centre_is_still_accepted(x):
    _, audit = pack([Event(Kind.TIME_SHIFT, 100), Event(Kind.POS_X, x), Event(Kind.CIRCLE)], keycount=4)
    assert audit['kept_model_heads'] == 1 and audit['discarded_model_heads'] == 0


def test_bad_lane_above_the_fragment_cap_is_rejected():
    events = taps(20) + [Event(Kind.TIME_SHIFT, 7000), Event(Kind.POS_X, 832), Event(Kind.CIRCLE)] + taps(20)
    with pytest.raises(ValueError, match='数量'):
        pack(events, terminal_policy='mania-unlocated-fragments-v2')


def upstream_reader(events, *, types_first=False, event_times=None):
    """Same control flow as the pinned data_utils.get_groups, including its unguarded
    ``group_indices[-1]`` when no type event exists at all (kept to reproduce the IndexError)."""
    groups, group_indices, indices, group = [], [], [], SimpleNamespace(event_type=None, time=0, x=None)
    for i, event in enumerate(events):
        indices.append(i)
        if event.type == Kind.TIME_SHIFT:
            group.time = event.value
        elif event.type == Kind.POS_X:
            group.x = event.value
        elif event.type in (Kind.CIRCLE, Kind.HOLD_NOTE, Kind.HOLD_NOTE_END):
            group.event_type = event.type
            groups.append(group); group_indices.append(indices)
            group, indices = SimpleNamespace(event_type=None, time=0, x=None), []
    if group.event_type is not None:
        groups.append(group); group_indices.append(indices)
    elif len(indices) > 0:
        group_indices[-1].extend(indices)
    return groups, group_indices


class Sustain(Enum):
    SUSTAIN = 'hold_note_sustain'
    POS_Y = 'pos_y'


@pytest.mark.parametrize('types_first', [False, True])
def test_orphan_sustain_marker_stream_is_a_clean_per_request_rejection(types_first):
    # The real C1_s2 / C3_s1 outputs: t, pos_x, pos_y, hold_note_sustain and nothing else.
    events = [Event(Kind.TIME_SHIFT, 182786), Event(Kind.POS_X, 64), Event(Sustain.POS_Y, 192), Event(Sustain.SUSTAIN, 0)]
    with pytest.raises(IndexError):
        upstream_reader(events, types_first=types_first)
    with pytest.raises(ValueError, match='没有任何音符') as caught:
        pack_mania_events(events, group_reader=upstream_reader, event_factory=Event, event_types=Kind,
                          types_first=types_first, terminal_policy='mania-unlocated-fragments-v2')
    assert not isinstance(caught.value, IndexError)
    assert caught.value.details['reason'] == 'fragments_without_any_note_group'
    assert caught.value.details['event_count'] == 4
    assert caught.value.details['token_types'] == {'t': 1, 'pos_x': 1, 'pos_y': 1, 'hold_note_sustain': 1}


def test_an_empty_stream_stays_a_valid_empty_chart():
    packed, audit = pack_mania_events([], group_reader=upstream_reader, event_factory=Event, event_types=Kind)
    assert packed == [] and audit['model_heads'] == audit['kept_model_heads'] == 0


def test_postprocessor_saves_native_events_and_error_for_the_sustain_only_stream(tmp_path, monkeypatch):
    from malody_studio import v32_event_serialization as adapter

    class Postprocessor:
        def __init__(self, args):
            self.types_first = False

        def generate(self, events, *_args):
            return '[HitObjects]\n'

    upstream = SimpleNamespace(get_groups=upstream_reader, Event=Event, EventType=Kind)
    monkeypatch.setattr(adapter.inspect, 'getmodule', lambda _cls: upstream)
    module = SimpleNamespace(Postprocessor=Postprocessor)
    adapter.install(module, terminal_policy='mania-unlocated-fragments-v2')
    processor = module.Postprocessor(SimpleNamespace(output_path=tmp_path / 'req', keycount=4))
    events = [Event(Kind.TIME_SHIFT, 5), Event(Kind.POS_X, 64), Event(Sustain.SUSTAIN, 0)]
    with pytest.raises(ValueError, match='没有任何音符'):
        processor.generate(events, SimpleNamespace(mode=3))
    import json
    saved = json.loads((tmp_path / 'req' / 'serialization-error.json').read_text(encoding='utf-8'))
    assert saved['error_type'] == 'NativeStreamError' and saved['details']['reason'] == 'fragments_without_any_note_group'
    assert (tmp_path / 'req' / 'model-events.json').is_file()
