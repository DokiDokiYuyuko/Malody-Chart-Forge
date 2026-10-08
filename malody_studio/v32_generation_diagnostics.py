"""Opt-in, read-only traces of the pinned V32 Processor event pipeline.

This module deliberately imports no ML runtime. A caller may temporarily wrap
an inference module's ``Processor`` while one Advanced request runs, then pass
the request's per-role/per-difficulty output directory. The wrapped methods
copy data for JSON only and always return the original upstream values.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import uuid
from typing import Any, Iterator


SCHEMA = "v32-generation-diagnostics-v1"
DEFAULT_FILENAME = "v32-generation-diagnostics.json"


def _plain(value: Any) -> Any:
    """Convert small diagnostic values to JSON values without changing inputs."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _plain(value.value)
    if is_dataclass(value):
        return _plain(asdict(value))
    if hasattr(value, "detach") and hasattr(value, "cpu"):
        try:
            return _plain(value.detach().cpu().tolist())
        except Exception:
            pass
    if hasattr(value, "item"):
        try:
            return _plain(value.item())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(_plain(key)): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "value"):
        return _plain(value.value)
    return repr(value)


def _token_ids(tokens: Any) -> list[Any]:
    """Copy a token sequence into plain values; never edit the tensor/list."""
    copied = _plain(tokens)
    if isinstance(copied, list):
        return copied
    if isinstance(copied, tuple):
        return list(copied)
    return [copied]


def _event_record(event: Any) -> dict[str, Any]:
    return {
        "type": _plain(getattr(event, "type", None)),
        "value": _plain(getattr(event, "value", None)),
    }


def _events(events: Any) -> list[dict[str, Any]]:
    return [_event_record(event) for event in events]


def _pair_snapshot(events: Any, event_times: Any) -> dict[str, Any]:
    return {
        "events": _events(events),
        "event_times": _plain(event_times),
    }


def _tail_snapshot(events: Any, event_times: Any, limit: int = 8) -> dict[str, Any]:
    return {
        "event_count": len(events),
        "event_time_count": len(event_times),
        "sha256": _context_sha256(events, event_times),
        "events": _events(events[-limit:]),
        "event_times": _plain(event_times[-limit:]),
    }


_PRIMITIVES = (int, float, str, bool, type(None))
_ROW_CACHE: dict[Any, bytes] = {}
_ROW_CACHE_LIMIT = 200_000


def _context_row(event: Any, event_time: Any) -> bytes:
    """Serialized hash row of one event; identical bytes with or without the memo.

    Every window re-hashes the whole growing context twice, so the same rows are
    serialized hundreds of times per chart. Only immutable primitives are
    memoized, keyed with their exact classes because ``1`` and ``1.0`` hash
    equal but serialize differently.
    """
    kind, value = getattr(event, "type", None), getattr(event, "value", None)
    key = None
    if (type(value) in _PRIMITIVES and type(event_time) in _PRIMITIVES
            and (isinstance(kind, Enum) or type(kind) in _PRIMITIVES)):
        key = (kind.__class__, kind, value.__class__, value, event_time.__class__, event_time)
        cached = _ROW_CACHE.get(key)
        if cached is not None:
            return cached
    row = {
        "event": {"type": _plain(kind), "value": _plain(value)},
        "event_time": _plain(event_time),
    }
    data = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if key is not None:
        if len(_ROW_CACHE) >= _ROW_CACHE_LIMIT:
            _ROW_CACHE.clear()
        _ROW_CACHE[key] = data
    return data


def _context_sha256(events: Any, event_times: Any) -> str:
    """Hash a context incrementally so windows do not retain growing copies."""
    digest = hashlib.sha256()
    time_count = len(event_times)
    for index, event in enumerate(events):
        digest.update(_context_row(event, event_times[index] if index < time_count else None))
    return digest.hexdigest()


def _event_type_name(event: Any) -> Any:
    return _plain(getattr(event, "type", None))


def _exception_record(exc: BaseException) -> dict[str, str]:
    return {"type": type(exc).__name__, "message": str(exc)}


class _Trace:
    def __init__(self, output_dir: Path, filename: str):
        self.path = output_dir / filename
        self.document: dict[str, Any] = {
            "schema": SCHEMA,
            "status": "capturing",
            "processor_calls": [],
        }
        self.scope_error: dict[str, str] | None = None
        self.diagnostic_errors: list[dict[str, str]] = []

    def begin_processor_call(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        call = {
            "requested_in_context": _plain(kwargs.get("in_context", [])),
            "requested_out_context": _plain(kwargs.get("out_context", [])),
            "generation_config": _plain(kwargs.get("generation_config")),
            "beatmap_path": _plain(kwargs.get("beatmap_path")),
            "windows": [],
            "transforms": [],
        }
        self.document["processor_calls"].append(call)
        return call

    def record_transform(self, call: dict[str, Any] | None,
                         window: dict[str, Any] | None,
                         record: dict[str, Any]) -> None:
        if window is not None:
            window.setdefault("transforms", []).append(record)
        elif call is not None:
            call["transforms"].append(record)

    def record_error(self, stage: str, exc: BaseException) -> None:
        self.diagnostic_errors.append({
            "stage": stage,
            "type": type(exc).__name__,
            "message": str(exc),
        })

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.scope_error is not None:
            self.document["status"] = "error"
            self.document["error"] = self.scope_error
        elif self.diagnostic_errors:
            self.document["status"] = "complete_with_diagnostic_error"
        else:
            self.document["status"] = "complete"
        if self.diagnostic_errors:
            self.document["diagnostic_error"] = self.diagnostic_errors

        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(
                json.dumps(self.document, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


def _traced_processor(base: type, trace: _Trace) -> type:
    class TracedProcessor(base):
        _v32_native_diagnostics_wrapper = True

        def __init__(self, *args: Any, **kwargs: Any):
            super().__init__(*args, **kwargs)
            self._v32_trace = trace
            self._v32_trace_call: dict[str, Any] | None = None
            self._v32_trace_window: dict[str, Any] | None = None

        def generate(self, *args: Any, **kwargs: Any):
            try:
                call = trace.begin_processor_call(kwargs)
            except Exception as exc:
                trace.record_error("processor_call_start", exc)
                call = None
            previous_call = self._v32_trace_call
            self._v32_trace_call = call
            try:
                result = super().generate(*args, **kwargs)
            except BaseException as exc:
                if call is not None:
                    try:
                        call["error"] = _exception_record(exc)
                    except Exception as capture_error:
                        trace.record_error("processor_exception_record", capture_error)
                raise
            else:
                if call is not None:
                    try:
                        contexts = kwargs.get("out_context", [])
                        returned = []
                        for index, pair in enumerate(result or []):
                            try:
                                events, event_times = pair
                            except (TypeError, ValueError):
                                returned.append({"index": index, "value": _plain(pair)})
                                continue
                            context = contexts[index] if index < len(contexts) else index
                            returned.append({
                                "context": _plain(context),
                                **_pair_snapshot(events, event_times),
                            })
                        call["returned_contexts"] = returned
                    except Exception as exc:
                        trace.record_error("processor_return_snapshot", exc)
                return result
            finally:
                self._v32_trace_call = previous_call

        def add_predicted_tokens_to_context(
                self, context: dict[str, Any], predicted_tokens: Any, frame_time: Any,
                trim_lookback: bool = False, trim_lookahead: bool = False):
            call = self._v32_trace_call
            window = None
            if call is not None:
                try:
                    before = _tail_snapshot(context["events"], context["event_times"])
                    window = {
                        "window_index": len(call["windows"]),
                        "context": _plain(context.get("context_type")),
                        "frame_time": _plain(frame_time),
                        "trim_lookback": bool(trim_lookback),
                        "trim_lookahead": bool(trim_lookahead),
                        "predicted_token_ids_before_eos_or_overlap_trim": _token_ids(predicted_tokens),
                        "context_before": before,
                        "transforms": [],
                    }
                    call["windows"].append(window)
                except Exception as exc:
                    trace.record_error("window_start_snapshot", exc)

            previous_window = self._v32_trace_window
            self._v32_trace_window = window
            try:
                return super().add_predicted_tokens_to_context(
                    context, predicted_tokens, frame_time,
                    trim_lookback=trim_lookback,
                    trim_lookahead=trim_lookahead,
                )
            finally:
                if window is not None:
                    try:
                        after = _tail_snapshot(context["events"], context["event_times"])
                        before_counts = window["context_before"]
                        before_len = before_counts.get("event_count", 0)
                        before_time_len = before_counts.get("event_time_count", 0)
                        window["context_after"] = after
                        window["appended_suffix"] = {
                            "events": _events(context["events"][before_len:]) if len(context["events"]) >= before_len else [],
                            "event_times": _plain(context["event_times"][before_time_len:]) if len(context["event_times"]) >= before_time_len else [],
                            "separable_from_prior_context": len(context["events"]) >= before_len and len(context["event_times"]) >= before_time_len,
                        }
                    except Exception as exc:
                        trace.record_error("window_context_after", exc)
                self._v32_trace_window = previous_window

        def _decode(self, tokens: Any, *args: Any, **kwargs: Any):
            decoded = super()._decode(tokens, *args, **kwargs)
            if self._v32_trace_window is not None:
                try:
                    self._v32_trace_window.setdefault("decode_calls", []).append({
                        "token_ids_after_eos_or_overlap_trim": _token_ids(tokens),
                        "decoded_events": _events(decoded),
                    })
                except Exception as exc:
                    trace.record_error("decode_snapshot", exc)
            return decoded

        def _trim_events_before_time(self, events: Any, event_times: Any, time: Any):
            call, window = self._v32_trace_call, self._v32_trace_window
            try:
                before_count = len(events)
                removed_indices = [i for i, t in enumerate(event_times) if t < time]
                removed_records = [
                    {"index": i, "event": _event_record(events[i]), "event_time": _plain(event_times[i])}
                    for i in removed_indices if i < len(events)
                ]
                full_before = _pair_snapshot(events, event_times) if window is None and call is not None else None
            except Exception as exc:
                trace.record_error("trim_before_input_snapshot", exc)
                before_count, removed_records, full_before = len(events), [], None
            result = super()._trim_events_before_time(events, event_times, time)
            try:
                record = {
                    "stage": "trim_events_before_time",
                    "cutoff": _plain(time),
                    "event_count_before": before_count,
                    "event_count_after": len(events),
                    "removed": removed_records,
                }
                if full_before is not None:
                    record["input"] = full_before
                    record["output"] = _pair_snapshot(events, event_times)
                trace.record_transform(call, window, record)
            except Exception as exc:
                trace.record_error("trim_before_record", exc)
            return result

        def _trim_events_after_time(self, events: Any, event_times: Any, time: Any):
            call, window = self._v32_trace_call, self._v32_trace_window
            try:
                before_count = len(events)
                removed_indices = []
                for i in range(len(event_times) - 1, -1, -1):
                    if event_times[i] > time:
                        removed_indices.append(i)
                    else:
                        break
                removed_records = [
                    {"index": i, "event": _event_record(events[i]), "event_time": _plain(event_times[i])}
                    for i in removed_indices if i < len(events)
                ]
                full_before = _pair_snapshot(events, event_times) if window is None and call is not None else None
            except Exception as exc:
                trace.record_error("trim_after_input_snapshot", exc)
                before_count, removed_records, full_before = len(events), [], None
            result = super()._trim_events_after_time(events, event_times, time)
            try:
                record = {
                    "stage": "trim_events_after_time",
                    "cutoff": _plain(time),
                    "event_count_before": before_count,
                    "event_count_after": len(events),
                    "removed": removed_records,
                }
                if full_before is not None:
                    record["input"] = full_before
                    record["output"] = _pair_snapshot(events, event_times)
                trace.record_transform(call, window, record)
            except Exception as exc:
                trace.record_error("trim_after_record", exc)
            return result

        def _rescale_positions(self, events: Any, event_times: Any):
            try:
                before = _pair_snapshot(events, event_times)
            except Exception as exc:
                trace.record_error("rescale_input_snapshot", exc)
                before = None
            result = super()._rescale_positions(events, event_times)
            try:
                after = _pair_snapshot(*result)
                trace.record_transform(self._v32_trace_call, self._v32_trace_window, {
                    "stage": "rescale_positions", "before": before, "after": after,
                })
            except Exception as exc:
                trace.record_error("rescale_output_snapshot", exc)
            return result

        def _convert_column_to_position(self, events: Any, event_times: Any, key_count: Any):
            try:
                before = _pair_snapshot(events, event_times)
            except Exception as exc:
                trace.record_error("column_convert_input_snapshot", exc)
                before = None
            result = super()._convert_column_to_position(events, event_times, key_count)
            try:
                after = _pair_snapshot(*result)
                trace.record_transform(self._v32_trace_call, self._v32_trace_window, {
                    "stage": "convert_column_to_position",
                    "key_count": _plain(key_count),
                    "before": before,
                    "after": after,
                })
            except Exception as exc:
                trace.record_error("column_convert_output_snapshot", exc)
            return result

    TracedProcessor.__name__ = f"Traced{base.__name__}"
    TracedProcessor.__qualname__ = TracedProcessor.__name__
    TracedProcessor.__module__ = base.__module__
    return TracedProcessor


@contextmanager
def capture_processor_diagnostics(inference_module, output_dir, *, enabled=False, filename=DEFAULT_FILENAME):
    if not enabled:
        yield None
        return
    original = inference_module.Processor
    router = original if getattr(original, "_malody_stream_router", False) else None
    base = router.resolve() if router is not None else original
    if getattr(base, "_v32_native_diagnostics_wrapper", False):
        raise RuntimeError("V32 Processor diagnostics are already installed")
    trace = _Trace(Path(output_dir), filename)
    traced = _traced_processor(base, trace)
    previous = None
    if router is not None:
        previous = router.push(traced)
    else:
        inference_module.Processor = traced
    try:
        yield trace
    except BaseException as exc:
        trace.scope_error = _exception_record(exc)
        raise
    finally:
        if router is not None:
            router.pop(previous)
        else:
            inference_module.Processor = original
        try:
            trace.write()
        except Exception as write_error:
            trace.record_error("write", write_error)
            trace.document["diagnostic_error"] = list(trace.diagnostic_errors)
            trace.document["status"] = ("error" if trace.scope_error is not None else "complete_with_diagnostic_error")
            if trace.scope_error is not None:
                trace.document["error"] = trace.scope_error
