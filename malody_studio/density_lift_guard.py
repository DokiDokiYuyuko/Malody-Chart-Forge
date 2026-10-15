"""Bound model-note reuse against the original chart in every local window."""
from __future__ import annotations

from bisect import bisect_left
from math import ceil, floor

# Width (ms), maximum proportional increase. The additive floor permits one
# extra head in a sparse musical figure, never a new run in a silent window.
WINDOW_LIMITS = ((500, 0.25), (1000, 0.25), (5000, 0.20), (10000, 0.15))


def _count(sorted_times: list[float], start: float, width: int) -> int:
    return bisect_left(sorted_times, start + width) - bisect_left(sorted_times, start)


def _starts(times: list[float], point: float, width: int) -> set[float]:
    lo = point - width
    starts = {float(step * 50) for step in range(floor(lo / 50), ceil(point / 50) + 1)}
    for time in times:
        if lo <= time <= point + width:
            starts.add(time)
            starts.add(time - width + 0.001)
    return {start for start in starts if lo < start <= point}


def local_cap_allows(base_sorted: list[float], added_sorted: list[float], point: float) -> bool:
    trial = sorted([*added_sorted, point])
    for width, fraction in WINDOW_LIMITS:
        for start in _starts([*base_sorted, *trial], point, width):
            baseline = _count(base_sorted, start, width)
            added = _count(trial, start, width)
            if baseline == 0 and added:
                return False
            allowance = max(1, floor(baseline * fraction)) if baseline else 0
            if added > allowance:
                return False
    return True


def full_cap_violations(base: list[float], candidate: list[float]) -> list[dict]:
    """Independent final sweep, including exact event boundaries."""
    original = sorted(base)
    result = sorted(candidate)
    violations = []
    for width, fraction in WINDOW_LIMITS:
        starts = {float(step * 50) for step in range(floor(min(result) / 50),
                                                       ceil(max(result) / 50) + 1)}
        starts.update(result)
        starts.update(time - width + 0.001 for time in result)
        for start in sorted(starts):
            baseline = _count(original, start, width)
            total = _count(result, start, width)
            allowance = max(1, floor(baseline * fraction)) if baseline else 0
            if total - baseline > allowance:
                violations.append({"width_ms": width, "start_ms": start,
                                   "baseline": baseline, "candidate": total,
                                   "allowed_additions": allowance})
                if len(violations) >= 20:
                    return violations
    return violations
