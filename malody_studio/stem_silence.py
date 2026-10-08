"""Frame-level activity of a stem relative to its own file peak (pure numpy).

The model sees peak-normalised audio, so a stem that is mostly silent still has its
loudest moment at 0 dB. Level is therefore measured against the whole-file peak.
Used to (a) recognise a timing-less model request on a silent stem and (b) declare
long silent runs in which a stem's own generated notes are not trusted.
"""
import math

import numpy as np

POLICY = 'stem-silence-v1'
FRAME_MS = 100
THRESHOLD_DB = -50.
REQUEST_SILENT_FRACTION = .5
RUN_MIN_MS = 5000
RUN_GUARD_MS = 500
FLOOR_DB = -120.


def policy_record(**overrides):
    return {'policy': POLICY, 'frame_ms': FRAME_MS, 'threshold_db_rel_file_peak': THRESHOLD_DB,
            'request_silent_fraction': REQUEST_SILENT_FRACTION, 'run_min_ms': RUN_MIN_MS,
            'run_guard_ms': RUN_GUARD_MS, **overrides}


def _mono(samples):
    data = np.asarray(samples, dtype=np.float64)
    return data.mean(axis=1) if data.ndim > 1 else data


def file_peak(samples):
    data = _mono(samples)
    return float(np.max(np.abs(data))) if len(data) else 0.


def frame_levels_db(samples, sample_rate, frame_ms=FRAME_MS):
    """RMS of every frame in dB relative to the file peak. The final partial frame counts."""
    data = _mono(samples)
    size = max(1, int(round(sample_rate * frame_ms / 1000)))
    count = -(-len(data) // size)
    if not count:
        return np.zeros(0)
    peak = float(np.max(np.abs(data)))
    padded = np.zeros(count * size)
    padded[:len(data)] = data
    lengths = np.full(count, size, dtype=np.float64)
    lengths[-1] = len(data) - (count - 1) * size
    rms = np.sqrt((padded.reshape(count, size) ** 2).sum(axis=1) / lengths)
    if peak <= 0:
        return np.full(count, FLOOR_DB)
    with np.errstate(divide='ignore'):
        levels = 20 * np.log10(rms / peak)
    return np.maximum(levels, FLOOR_DB)


def _runs(mask):
    """[start, stop) index pairs of consecutive True values."""
    if not len(mask):
        return []
    edges = np.flatnonzero(np.diff(np.concatenate(([0], mask.astype(np.int8), [0]))))
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2])]


def silent_runs(levels, *, min_run_ms=RUN_MIN_MS, guard_ms=RUN_GUARD_MS, threshold_db=THRESHOLD_DB,
                frame_ms=FRAME_MS, duration_ms=None):
    """Runs of silent frames at least ``min_run_ms`` long.

    ``declared_*`` shrinks the run by ``guard_ms`` where it touches active audio; a side
    that touches the start or end of the file has no active audio to protect and gets no guard.
    """
    count = len(levels)
    total = count * frame_ms if duration_ms is None else min(float(duration_ms), count * frame_ms)
    result = []
    for first, stop in _runs(np.asarray(levels) < threshold_db):
        start_ms, end_ms = first * frame_ms, min(stop * frame_ms, total)
        if end_ms - start_ms < min_run_ms:
            continue
        declared_start = start_ms + (guard_ms if first > 0 else 0)
        declared_end = end_ms - (guard_ms if stop < count else 0)
        if declared_end <= declared_start:
            continue
        result.append({'start_ms': start_ms, 'end_ms': end_ms, 'declared_start_ms': declared_start,
                       'declared_end_ms': declared_end, 'level_db': round(float(np.max(levels[first:stop])), 2),
                       'threshold_db': threshold_db, 'min_run_ms': min_run_ms, 'guard_ms': guard_ms})
    return result


def range_activity(levels, start_ms, end_ms, *, threshold_db=THRESHOLD_DB, frame_ms=FRAME_MS):
    """Silence measurements over the frames that lie completely inside [start_ms, end_ms)."""
    first = max(0, int(math.ceil(start_ms / frame_ms)))
    stop = min(len(levels), int(math.floor(end_ms / frame_ms)))
    window = np.asarray(levels[first:stop]) if stop > first else np.zeros(0)
    silent = window < threshold_db
    longest = max((b - a for a, b in _runs(silent)), default=0)
    energy = float(np.mean(10 ** (window / 10))) if len(window) else 0.
    return {'frames': int(len(window)), 'silent_frames': int(silent.sum()),
            'silent_fraction': float(silent.mean()) if len(window) else 0.,
            'longest_silent_run_ms': int(longest * frame_ms),
            'peak_relative_rms_db': round(10 * math.log10(energy), 2) if energy > 0 else FLOOR_DB,
            'threshold_db': threshold_db, 'frame_ms': frame_ms,
            'range_ms': [first * frame_ms, stop * frame_ms]}
