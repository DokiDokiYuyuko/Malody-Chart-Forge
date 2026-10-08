import json
from dataclasses import dataclass
from enum import Enum
from types import SimpleNamespace

import pytest

from malody_studio.v32_generation_diagnostics import capture_processor_diagnostics


class Kind(Enum):
    TIME_SHIFT = "t"
    POS_X = "pos_x"
    POS_Y = "pos_y"
    MANIA_COLUMN = "column"
    HOLD_NOTE = "hold_note"


class Context(Enum):
    MAP = "map"


@dataclass
class Event:
    type: Kind
    value: int


@dataclass
class GenerationConfig:
    gamemode: int
    keycount: int


class FakeTensor:
    def __init__(self, values):
        self.values = list(values)

    def detach(self):
        return self

    def cpu(self):
        return self

    def tolist(self):
        return self.values.copy()

    def __bool__(self):
        return bool(self.values)

    def __len__(self):
        return len(self.values)

    def __getitem__(self, item):
        value = self.values[item]
        return FakeTensor(value) if isinstance(item, slice) else value

    def __iter__(self):
        return iter(self.values)


class FakeProcessor:
    """CPU-only stand-in for the upstream methods wrapped by the trace."""

    last_return = None

    def _decode(self, tokens, frame_time):
        mapping = {
            10: Event(Kind.TIME_SHIFT, frame_time),
            11: Event(Kind.POS_X, 64),
            12: Event(Kind.MANIA_COLUMN, 1),
            13: Event(Kind.HOLD_NOTE, 0),
        }
        return [mapping[token] for token in tokens]

    def add_predicted_tokens_to_context(self, context, predicted_tokens, frame_time,
                                        trim_lookback=False, trim_lookahead=False):
        trimmed = predicted_tokens
        while trimmed and trimmed[-1] == 2:
            trimmed = trimmed[:-1]
        decoded = self._decode(trimmed, frame_time)
        context["events"].extend(decoded)
        context["event_times"].extend(frame_time + i * 10 for i in range(len(decoded)))
        if trim_lookahead:
            self._trim_events_after_time(context["events"], context["event_times"], frame_time + 15)

    def _trim_events_before_time(self, events, event_times, time):
        for i in range(len(event_times) - 1, -1, -1):
            if event_times[i] < time:
                del events[i]
                del event_times[i]

    def _trim_events_after_time(self, events, event_times, time):
        for i in range(len(event_times) - 1, -1, -1):
            if event_times[i] > time:
                del events[i]
                del event_times[i]
            else:
                break

    def _rescale_positions(self, events, event_times):
        new_events = [
            Event(event.type, event.value * 2) if event.type == Kind.POS_X else event
            for event in events
        ]
        return new_events, list(event_times)

    def _convert_column_to_position(self, events, event_times, key_count):
        new_events, new_times = [], []
        for event, event_time in zip(events, event_times):
            if event.type == Kind.MANIA_COLUMN:
                new_events.extend((Event(Kind.POS_X, int((event.value + 0.5) * 512 / key_count)),
                                   Event(Kind.POS_Y, 192)))
                new_times.extend((event_time, event_time))
            else:
                new_events.append(event)
                new_times.append(event_time)
        return new_events, new_times

    def generate(self, *, predictions, out_context, generation_config=None,
                 in_context=None, beatmap_path=None, **_kwargs):
        context = {"context_type": Context.MAP, "events": [], "event_times": []}
        for token_ids, frame_time, trim_lookahead in predictions:
            self.add_predicted_tokens_to_context(
                context, token_ids, frame_time, trim_lookahead=trim_lookahead)
        self._trim_events_before_time(context["events"], context["event_times"], 0)
        events, times = self._rescale_positions(context["events"], context["event_times"])
        events, times = self._convert_column_to_position(events, times, key_count=4)
        self.last_return = [(events, times)]
        FakeProcessor.last_return = self.last_return
        return self.last_return


def test_enabled_trace_preserves_inputs_return_and_records_pipeline(tmp_path):
    module = SimpleNamespace(Processor=FakeProcessor)
    original_class = module.Processor
    first_tokens = FakeTensor([10, 11, 12, 2])
    second_tokens = [10, 11, 13, 2]
    original_tokens = [first_tokens.values.copy(), second_tokens.copy()]

    with capture_processor_diagnostics(module, tmp_path, enabled=True) as trace:
        processor = module.Processor()
        returned = processor.generate(
            predictions=[(first_tokens, 100, False), (second_tokens, 200, True)],
            in_context=[], out_context=[Context.MAP],
            generation_config=GenerationConfig(gamemode=3, keycount=4),
        )
        assert returned is processor.last_return

    assert module.Processor is original_class
    assert first_tokens.values == original_tokens[0]
    assert second_tokens == original_tokens[1]
    assert trace is not None

    data = json.loads((tmp_path / "v32-generation-diagnostics.json").read_text(encoding="utf-8"))
    assert data["status"] == "complete"
    call = data["processor_calls"][0]
    assert call["requested_out_context"] == ["map"]
    assert call["generation_config"] == {"gamemode": 3, "keycount": 4}
    assert len(call["windows"]) == 2

    first, second = call["windows"]
    assert first["predicted_token_ids_before_eos_or_overlap_trim"] == original_tokens[0]
    assert first["decode_calls"][0]["token_ids_after_eos_or_overlap_trim"] == [10, 11, 12]
    assert first["decode_calls"][0]["decoded_events"][-1] == {"type": "column", "value": 1}
    assert first["context_before"]["sha256"] != first["context_after"]["sha256"]
    assert first["appended_suffix"]["separable_from_prior_context"] is True

    assert second["predicted_token_ids_before_eos_or_overlap_trim"] == original_tokens[1]
    assert second["decode_calls"][0]["decoded_events"][-1] == {"type": "hold_note", "value": 0}
    removed = [item for step in second["transforms"] for item in step.get("removed", [])]
    assert removed == [{"index": 5, "event": {"type": "hold_note", "value": 0}, "event_time": 220}]

    rescale = next(step for step in call["transforms"] if step["stage"] == "rescale_positions")
    convert = next(step for step in call["transforms"] if step["stage"] == "convert_column_to_position")
    assert rescale["before"]["events"][1] == {"type": "pos_x", "value": 64}
    assert rescale["after"]["events"][1] == {"type": "pos_x", "value": 128}
    assert convert["before"]["events"][2] == {"type": "column", "value": 1}
    assert convert["after"]["events"][2:4] == [
        {"type": "pos_x", "value": 192}, {"type": "pos_y", "value": 192},
    ]
    assert call["returned_contexts"][0]["context"] == "map"
    assert call["returned_contexts"][0]["events"] == [
        {"type": event.type.value, "value": event.value} for event in returned[0][0]
    ]
    assert call["returned_contexts"][0]["event_times"] == returned[0][1]


def test_disabled_trace_is_noop_and_creates_no_file(tmp_path):
    module = SimpleNamespace(Processor=FakeProcessor)
    original_class = module.Processor
    tokens = [10, 11, 2]

    with capture_processor_diagnostics(module, tmp_path, enabled=False) as trace:
        assert trace is None
        processor = module.Processor()
        returned = processor.generate(predictions=[(tokens, 50, False)], out_context=[Context.MAP])

    assert module.Processor is original_class
    assert returned is processor.last_return
    assert list(tmp_path.iterdir()) == []


def test_upstream_exception_is_rethrown_and_partial_trace_is_written(tmp_path):
    failure = RuntimeError("native decode failed")

    class FailingProcessor(FakeProcessor):
        def generate(self, **kwargs):
            self.add_predicted_tokens_to_context(
                {"context_type": Context.MAP, "events": [], "event_times": []},
                kwargs["tokens"], 100,
            )
            raise failure

    module = SimpleNamespace(Processor=FailingProcessor)
    original_class = module.Processor
    tokens = [10, 11, 2]
    with pytest.raises(RuntimeError) as raised:
        with capture_processor_diagnostics(module, tmp_path, enabled=True):
            module.Processor().generate(tokens=tokens, out_context=[Context.MAP])

    assert raised.value is failure
    assert module.Processor is original_class
    assert tokens == [10, 11, 2]
    data = json.loads((tmp_path / "v32-generation-diagnostics.json").read_text(encoding="utf-8"))
    assert data["status"] == "error"
    assert data["error"] == {"type": "RuntimeError", "message": "native decode failed"}
    assert data["processor_calls"][0]["error"] == data["error"]
    assert data["processor_calls"][0]["windows"][0]["predicted_token_ids_before_eos_or_overlap_trim"] == tokens


def test_diagnostic_write_failure_does_not_replace_generation_result(tmp_path, monkeypatch):
    from malody_studio import v32_generation_diagnostics as diagnostics

    def fail_write(_self):
        raise OSError("diagnostic disk failure")

    monkeypatch.setattr(diagnostics._Trace, "write", fail_write)
    module = SimpleNamespace(Processor=FakeProcessor)
    with capture_processor_diagnostics(module, tmp_path, enabled=True) as trace:
        processor = module.Processor()
        result = processor.generate(predictions=[([10, 11, 2], 100, False)], out_context=[Context.MAP])

    assert result is processor.last_return
    assert trace.diagnostic_errors == [{
        "stage": "write", "type": "OSError", "message": "diagnostic disk failure",
    }]
    assert trace.document["status"] == "complete_with_diagnostic_error"
    assert trace.document["diagnostic_error"] == trace.diagnostic_errors


def test_context_hash_rows_are_identical_with_and_without_the_memo():
    import hashlib
    from malody_studio import v32_generation_diagnostics as diagnostics

    def reference(events, event_times):
        digest = hashlib.sha256()
        for index, event in enumerate(events):
            row = {"event": {"type": diagnostics._plain(getattr(event, "type", None)),
                             "value": diagnostics._plain(getattr(event, "value", None))},
                   "event_time": diagnostics._plain(event_times[index]) if index < len(event_times) else None}
            digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()

    # 1, 1.0 and True hash equal as dictionary keys but serialize differently.
    events = [Event(Kind.TIME_SHIFT, 1), Event(Kind.TIME_SHIFT, 1.0), Event(Kind.TIME_SHIFT, True),
              Event(Kind.POS_X, 1), Event("t", 1), Event(Kind.HOLD_NOTE, FakeTensor([1])),
              SimpleNamespace(value=3), Event(Kind.TIME_SHIFT, 1)]
    for event_times in ([10, 10.0, 10, True, 10, 10, None, 10], [10, 10.0], []):
        diagnostics._ROW_CACHE.clear()
        cold = diagnostics._context_sha256(events, event_times)
        assert cold == diagnostics._context_sha256(events, event_times) == reference(events, event_times)
    assert diagnostics._ROW_CACHE
