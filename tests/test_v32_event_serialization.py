from types import SimpleNamespace
from enum import Enum
from dataclasses import dataclass
import pytest

from malody_studio.v32_event_serialization import pair_hold_groups, pack_mania_events


class Kind(Enum):
    HOLD_NOTE = 'hold_note'
    HOLD_NOTE_END = 'hold_note_end'
    CIRCLE = 'circle'
    TIME_SHIFT = 't'
    POS_X = 'pos_x'


@dataclass
class Event:
    type: Kind
    value: int = 0


def group(kind, time, x):
    return SimpleNamespace(event_type=Kind(kind), time=time, x=x)


def test_parallel_holds_and_mixed_chord_keep_independent_releases():
    groups = [group('hold_note', 100, 64), group('hold_note', 100, 192),
              group('circle', 100, 320), group('hold_note_end', 300, 192),
              group('hold_note_end', 700, 64)]
    blocks, conversions, audit = pair_hold_groups(groups)
    # Exercise the pinned serializer's single-pending behavior on the adapter's
    # output: both original model holds must survive with their own release.
    actual, pending = [], None
    for block in blocks:
        for index in block:
            g = groups[index]
            if g.event_type == Kind.HOLD_NOTE:
                assert pending is None
                pending = g
            elif g.event_type == Kind.HOLD_NOTE_END:
                actual.append((pending.time, pending.x, g.time))
                pending = None
            elif g.event_type == Kind.CIRCLE:
                actual.append((g.time, g.x, None))
    assert actual == [(100, 64, 700), (100, 192, 300), (100, 320, None)]
    assert conversions == {} and audit['model_heads'] == 3
    assert audit['paired_holds'] == 2 and audit['created_heads'] == 0


def test_incomplete_hold_preserves_head_without_invented_release():
    groups = [group('hold_note_end', 20, 64), group('hold_note', 100, 64),
              group('hold_note', 200, 192)]
    blocks, conversions, audit = pair_hold_groups(groups)
    assert blocks == [[1], [2]] and conversions == {1: 'circle', 2: 'circle'}
    assert audit['model_heads'] == 2 and audit['hold_heads_as_taps'] == 2
    assert audit['tails_without_heads'] == 1


def test_same_column_overwrite_retains_both_model_heads_and_reports_conflict():
    groups = [group('hold_note', 100, 64), group('hold_note', 250, 64),
              group('hold_note_end', 600, 64)]
    blocks, conversions, audit = pair_hold_groups(groups)
    assert blocks == [[0], [1, 2]] and conversions == {0: 'circle'}
    assert audit['model_heads'] == 2 and audit['paired_holds'] == 1
    assert audit['records'][0]['decision'] == 'unclosed_same_column_head_as_tap'


def test_negative_hold_duration_is_reported_and_head_is_retained():
    groups = [group('hold_note', 100, 64), group('hold_note_end', 90, 64)]
    blocks, conversions, audit = pair_hold_groups(groups)
    assert blocks == [[0]] and conversions == {0: 'circle'}
    assert audit['paired_holds'] == 0 and audit['hold_heads_as_taps'] == 1


def test_missing_hold_column_fails_instead_of_guessing():
    with pytest.raises(ValueError, match='轨道坐标'):
        pair_hold_groups([group('hold_note', 100, None)])


def test_event_packing_keeps_metadata_time_and_source_objects_immutable():
    groups = [group('hold_note', 100, 64), group('circle', 105, 320),
              group('hold_note', 110, 192), group('hold_note_end', 300, 192),
              group('hold_note_end', 700, 64)]
    events = [event for g in groups for event in
              (Event(Kind.TIME_SHIFT, g.time), Event(Kind.POS_X, g.x), Event(g.event_type))]
    original = [(e.type, e.value) for e in events]
    def reader(values, **_):
        assert values is events
        return groups, [list(range(i*3, i*3+3)) for i in range(len(groups))]
    packed, audit = pack_mania_events(events, group_reader=reader,
                                     event_factory=Event, event_types=Kind)
    assert [e.value for e in packed if e.type == Kind.TIME_SHIFT] == [100, 700, 105, 110, 300]
    assert [(e.type, e.value) for e in events] == original
    assert len(packed) == len(events) and audit['model_heads'] == 3


def test_failed_conversion_preserves_native_events_before_pairing(tmp_path, monkeypatch):
    import json
    from malody_studio import v32_event_serialization as adapter
    class Processor:
        def __init__(self, args):
            self.types_first = False
        def generate(self, *args):
            pytest.fail('Invalid native hold must not reach serialization')
    upstream = SimpleNamespace(
        get_groups=lambda events, **kw: ([group('hold_note', 100, None)], [[0, 1]]),
        Event=Event, EventType=Kind)
    monkeypatch.setattr(adapter.inspect, 'getmodule', lambda _: upstream)
    module = SimpleNamespace(Postprocessor=Processor)
    adapter.install(module)
    processor = module.Postprocessor(SimpleNamespace(output_path=tmp_path, keycount=4))
    events = [Event(Kind.TIME_SHIFT, 100), Event(Kind.HOLD_NOTE)]
    original = [(e.type, e.value) for e in events]
    with pytest.raises(ValueError, match='轨道坐标'):
        processor.generate(events, SimpleNamespace(mode=3))
    native = json.loads((tmp_path/'model-events.json').read_text(encoding='utf-8'))
    assert native['events'] == [['t',100], ['hold_note',0]]
    assert [(e.type, e.value) for e in events] == original
    failure = json.loads((tmp_path/'serialization-error.json').read_text(encoding='utf-8'))
    assert failure['native_events'] == 'model-events.json'
    assert failure['error_type'] == 'NativeGroupError'
    assert failure['details']['malformed_groups'] == [dict(group_index=0,kind='hold_note',missing_fields=['轨道坐标'],native_token_indices=[0,1])]
