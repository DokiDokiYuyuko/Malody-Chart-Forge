"""Bound local single-finger overload without changing note timing or audio."""
from __future__ import annotations

import bisect
import copy
from collections import defaultdict
from fractions import Fraction

FAST_GAP_MS = 125.0
MAX_FAST_RUN = 4
WINDOW_MS = 1000.0
MAX_LANE_HEADS_PER_SECOND = 8
POLICY = "single-lane-burden-v1"


def _ms(beat: list[int]) -> float:
    return float((Fraction(beat[0]) + Fraction(beat[1], beat[2])) * 500)


def _maximums(events: list[tuple[float, int]]) -> tuple[int, int]:
    lanes = defaultdict(list)
    for time, lane in events:
        lanes[lane].append(time)
    max_run = max_window = 0
    for times in lanes.values():
        times.sort()
        left = 0
        run = 0
        previous = None
        for right, time in enumerate(times):
            run = run + 1 if previous is not None and time - previous <= FAST_GAP_MS + 1e-6 else 1
            max_run = max(max_run, run)
            previous = time
            while time - times[left] > WINDOW_MS + 1e-6:
                left += 1
            max_window = max(max_window, right - left + 1)
    return max_run, max_window


def _repair_one_pass(chart: dict) -> tuple[dict, dict]:
    timing = chart.get("time", [])
    if (len(timing) != 1 or timing[0].get("beat") != [0, 0, 1]
            or float(timing[0].get("bpm", 0)) != 120.0):
        raise ValueError("Lane burden correction requires the fixed 120 BPM chart clock")
    result = copy.deepcopy(chart)
    notes = result["note"]
    rows = sorted(((i, _ms(n["beat"]), n) for i, n in enumerate(notes) if "column" in n),
                  key=lambda x: (x[1], x[0]))
    before = _maximums([(t, n["column"]) for _, t, n in rows])
    lane_times = {lane: sorted((t, i) for i, t, n in rows if n["column"] == lane) for lane in range(4)}
    holds = {lane: sorted((_ms(n["beat"]), _ms(n["endbeat"]), i)
                          for i, n in enumerate(notes) if n.get("column") == lane and "endbeat" in n)
             for lane in range(4)}
    removed = set()
    relaned = []
    unresolved = []

    def burden(lane, time, index):
        entries = lane_times[lane]
        pos = bisect.bisect_left(entries, (time, index))
        previous = entries[:pos]
        last_second = [item for item in previous if time - item[0] <= WINDOW_MS + 1e-6]
        run = 0
        cursor = time
        for old_time, _ in reversed(previous):
            if cursor - old_time > FAST_GAP_MS + 1e-6:
                break
            run += 1
            cursor = old_time
        return run + 1, len(last_second) + 1

    def change_tap(index, time, note):
        source = note["column"]
        exact_lanes = {other["column"] for other_index, other_time, other in rows
                       if other_index not in removed and other_index != index and abs(other_time - time) < 1e-6}
        options = []
        for target in range(4):
            if target == source or target in exact_lanes:
                continue
            entries = lane_times[target]
            pos = bisect.bisect_left(entries, (time, -1))
            previous = entries[pos - 1][0] if pos else None
            following = entries[pos][0] if pos < len(entries) else None
            if previous is not None and time - previous < FAST_GAP_MS - 1e-6:
                continue
            if following is not None and following - time < FAST_GAP_MS - 1e-6:
                continue
            if any(start < time < end for start, end, _ in holds[target]):
                continue
            target_run, target_window = burden(target, time, -1)
            if target_run > MAX_FAST_RUN or target_window > MAX_LANE_HEADS_PER_SECOND:
                continue
            options.append((target_window, target_run, abs(target - source), target))
        if options:
            target = min(options)[3]
            lane_times[source].remove((time, index))
            bisect.insort(lane_times[target], (time, index))
            note["column"] = target
            relaned.append({"index": index, "time_ms": time, "from": source, "to": target})
            return "relaned"
        else:
            lane_times[source].remove((time, index))
            removed.add(index)
            return "removed"

    for index, time, note in rows:
        if index in removed:
            continue
        source = note["column"]
        run, window = burden(source, time, index)
        if run <= MAX_FAST_RUN and window <= MAX_LANE_HEADS_PER_SECOND:
            continue
        if "endbeat" not in note:
            change_tap(index, time, note)
            continue
        # Keep a hold head on its original lane. Move or thin a preceding tap
        # in the offending run/window, choosing a central hit for fast runs.
        for _ in range(8):
            run, window = burden(source, time, index)
            if run <= MAX_FAST_RUN and window <= MAX_LANE_HEADS_PER_SECOND:
                break
            prior = [(old_time, old_index) for old_time, old_index in lane_times[source]
                     if old_time < time and time - old_time <= WINDOW_MS + 1e-6
                     and old_index not in removed and "endbeat" not in notes[old_index]]
            if run > MAX_FAST_RUN:
                chain = []
                cursor = time
                for old_time, old_index in reversed(lane_times[source]):
                    if old_time >= time:
                        continue
                    if cursor - old_time > FAST_GAP_MS + 1e-6:
                        break
                    chain.append((old_time, old_index))
                    cursor = old_time
                chain_ids = {old_index for _, old_index in chain}
                prior = [item for item in prior if item[1] in chain_ids]
                center = (chain[-1][0] + time) / 2 if chain else time
                prior.sort(key=lambda item: abs(item[0] - center))
            else:
                prior.sort()
            if not prior:
                break
            old_time, old_index = prior[0]
            change_tap(old_index, old_time, notes[old_index])
        run, window = burden(source, time, index)
        if run > MAX_FAST_RUN or window > MAX_LANE_HEADS_PER_SECOND:
            unresolved.append({"index": index, "time_ms": time, "lane": source,
                               "run": run, "lane_1s": window, "reason": "fixed_hold_head"})
    result["note"] = [n for i, n in enumerate(notes) if i not in removed]
    after = _maximums([(t, n["column"]) for i, t, n in rows if i not in removed])
    report = {"policy": POLICY, "before_max_fast_run": before[0], "before_max_lane_1s": before[1],
              "after_max_fast_run": after[0], "after_max_lane_1s": after[1],
              "relaned": len(relaned), "removed_taps": len(removed),
              "original_heads": len(rows), "remaining_heads": len(rows) - len(removed),
              "unresolved": unresolved[:20], "changes": relaned[:20]}
    return result, report


def repair_chart(chart: dict) -> tuple[dict, dict]:
    """Repair until local caps hold, or report the remaining impossible case."""
    current = chart
    reports = []
    for _ in range(4):
        current, report = _repair_one_pass(current)
        reports.append(report)
        if (report["after_max_fast_run"] <= MAX_FAST_RUN
                and report["after_max_lane_1s"] <= MAX_LANE_HEADS_PER_SECOND):
            break
        if not report["relaned"] and not report["removed_taps"]:
            break
    first, last = reports[0], reports[-1]
    summary = {"policy": POLICY, "before_max_fast_run": first["before_max_fast_run"],
               "before_max_lane_1s": first["before_max_lane_1s"],
               "after_max_fast_run": last["after_max_fast_run"],
               "after_max_lane_1s": last["after_max_lane_1s"],
               "relaned": sum(row["relaned"] for row in reports),
               "removed_taps": sum(row["removed_taps"] for row in reports),
               "original_heads": first["original_heads"], "remaining_heads": last["remaining_heads"],
               "passes": len(reports), "unresolved": last["unresolved"],
               "changes": [change for row in reports for change in row["changes"]][:20]}
    return current, summary
