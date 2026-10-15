"""Conservative, model-free cleanup of micro-offset simultaneous key heads.

The generated MC charts use a fixed 120 BPM clock. Up to 20 ms of distinct-
lane model-token scatter can form one chord, including 10+10 ms chains. A
20–32 ms split is aligned only when an existing exact chord anchors one side;
an exact center chord can absorb symmetric heads up to 32 ms on either side.
Wider rhythmic staggers and nearby same-lane repeats remain untouched.
"""
from __future__ import annotations

import copy
from collections import defaultdict
from fractions import Fraction

MAX_OFFSET_BEATS = Fraction(8, 125)  # 32 ms at 120 BPM
MAX_CHAIN_BEATS = Fraction(1, 25)   # 20 ms at 120 BPM
LANE_GUARD_BEATS = Fraction(3, 25)  # 60 ms at 120 BPM
POLICY = "micro-chord-32ms-evidence-v5"


def _moment(value: list[int]) -> Fraction:
    return Fraction(value[0]) + Fraction(value[1], value[2])


def normalize_chart(chart: dict) -> tuple[dict, dict]:
    """Return a chart copy plus a verifiable head-only edit summary."""
    timing = chart.get("time", [])
    if (len(timing) != 1 or timing[0].get("beat") != [0, 0, 1]
            or float(timing[0].get("bpm", 0)) != 120.0):
        raise ValueError("Micro-chord correction requires the fixed 120 BPM chart clock")
    result = copy.deepcopy(chart)
    rows = [(i, _moment(n["beat"]), n) for i, n in enumerate(result["note"]) if "column" in n]
    grouped = defaultdict(list)
    lane_times = defaultdict(list)
    lane_holds = defaultdict(list)
    for i, moment, note in rows:
        grouped[moment].append((i, note))
        lane_times[note["column"]].append((moment, i))
        if "endbeat" in note:
            lane_holds[note["column"]].append((moment, _moment(note["endbeat"]), i))
    times = sorted(grouped)
    components = []
    current = []
    for moment in times:
        if current and moment - current[-1] > MAX_OFFSET_BEATS:
            components.append(current)
            current = []
        current.append(moment)
    if current:
        components.append(current)
    proposals = []
    skipped = defaultdict(int)

    def safe_subcomponents(component):
        if len(component) < 2:
            return []
        span = component[-1] - component[0]
        if span <= MAX_CHAIN_BEATS or (span <= MAX_OFFSET_BEATS and len(component) == 2
                                       and max(len(grouped[moment]) for moment in component) >= 2):
            return [component]
        gaps = [right - left for left, right in zip(component, component[1:])]
        largest = max(gaps)
        if len(component) >= 3 and gaps.count(largest) == 1:
            split = gaps.index(largest) + 1
            return safe_subcomponents(component[:split]) + safe_subcomponents(component[split:])
        if len(component) == 3 and gaps[0] == gaps[1]:
            if (gaps[0] <= MAX_OFFSET_BEATS and len(grouped[component[1]]) >= 2
                    and sum(len(grouped[moment]) for moment in component) <= 4):
                return [component]
            left_has_anchor = len(grouped[component[0]]) >= 2
            right_has_anchor = len(grouped[component[2]]) >= 2
            if left_has_anchor != right_has_anchor and gaps[0] <= MAX_OFFSET_BEATS:
                pair = component[:2] if left_has_anchor else component[1:]
                return [pair]
        skipped["ambiguous_chain" if span > MAX_OFFSET_BEATS else
                "wide_split_without_chord_anchor"] += 1
        return []

    for broad_component in components:
        for component in safe_subcomponents(broad_component):
            notes = [(index, moment, note) for moment in component for index, note in grouped[moment]]
            lanes = [note["column"] for _, _, note in notes]
            if len(notes) > 4 or len(set(lanes)) != len(lanes):
                skipped["same_lane_or_full_chord"] += 1
                continue
            anchor = min(component, key=lambda moment: (-len(grouped[moment]), moment))
            moving = [(index, moment, note) for index, moment, note in notes if moment != anchor]
            invalid = None
            moving_ids = {index for index, _, _ in moving}
            relanes = {}
            for index, original, note in moving:
                lane = note["column"]
                nearby = any(other_index not in moving_ids and other_index != index
                             and (other_time == anchor or
                                  (abs(other_time - anchor) < LANE_GUARD_BEATS and
                                   abs(other_time - anchor) < abs(other_time - original)))
                             for other_time, other_index in lane_times[lane])
                if nearby:
                    alternatives = []
                    if len(moving) == 1 and len(grouped[anchor]) >= 2 and "endbeat" not in note:
                        for alternative in range(4):
                            if alternative in lanes:
                                continue
                            other_times = [other_time for other_time, other_index in lane_times[alternative]
                                           if other_index != index]
                            if any(abs(other_time - anchor) < LANE_GUARD_BEATS for other_time in other_times):
                                continue
                            if any(start < anchor < end for start, end, _ in lane_holds[alternative]):
                                continue
                            clearance = min((abs(other_time - anchor) for other_time in other_times),
                                            default=Fraction(10**9))
                            alternatives.append((-clearance, abs(alternative - lane), alternative))
                    if alternatives:
                        relanes[index] = min(alternatives)[2]
                    else:
                        invalid = "nearby_same_lane"
                        break
                if any(other_index != index and start < anchor < end
                       for start, end, other_index in lane_holds[lane]):
                    invalid = "crossing_hold"
                    break
                if "endbeat" in note and anchor >= _moment(note["endbeat"]):
                    invalid = "nonpositive_hold"
                    break
            if invalid:
                skipped[invalid] += 1
                continue
            proposals.extend((index, moment, anchor, relanes.get(index))
                             for index, moment, _ in moving)
    for index, _, anchor, new_lane in proposals:
        result["note"][index]["beat"] = grouped[anchor][0][1]["beat"].copy()
        if new_lane is not None:
            result["note"][index]["column"] = new_lane
    deltas_ms = [float(abs(anchor - original) * 500) for _, original, anchor, _ in proposals]
    summary = {"policy": POLICY, "moved_heads": len(proposals),
               "relaned_heads": sum(new_lane is not None for _, _, _, new_lane in proposals),
               "max_shift_ms": max(deltas_ms, default=0), "offset_ms": {
                   str(int(v) if v.is_integer() else v): deltas_ms.count(v) for v in sorted(set(deltas_ms))},
               "skipped": dict(skipped), "note_count": len(chart["note"])}
    return result, summary
