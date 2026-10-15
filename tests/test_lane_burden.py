from fractions import Fraction

from malody_studio.lane_burden import repair_chart


def beat_ms(value):
    beat = Fraction(value, 500)
    return [beat.numerator, 0, 1] if beat.denominator == 1 else [0, beat.numerator, beat.denominator]


def chart(notes):
    return {"meta": {"song": {"title": "test", "artist": "test"}},
            "time": [{"beat": [0, 0, 1], "bpm": 120.0}], "effect": [], "note": notes}


def test_long_single_lane_run_is_bounded_without_changing_time():
    original = chart([{"beat": beat_ms(i * 100), "column": 1} for i in range(7)])
    repaired, report = repair_chart(original)
    assert report["before_max_fast_run"] == 7
    assert report["after_max_fast_run"] <= 4
    assert report["after_max_lane_1s"] <= 8
    assert report["remaining_heads"] == 7
    assert [note["beat"] for note in repaired["note"]] == [note["beat"] for note in original["note"]]
    assert all(note["column"] == 1 for note in original["note"])


def test_full_chord_stack_thins_taps_only_when_no_lane_is_free():
    original = chart([{"beat": beat_ms(i * 100), "column": lane}
                      for i in range(7) for lane in range(4)])
    repaired, report = repair_chart(original)
    assert report["removed_taps"] > 0
    assert report["after_max_fast_run"] <= 4
    assert report["after_max_lane_1s"] <= 8
    assert all(note in original["note"] for note in repaired["note"])


def test_hold_endpoints_are_preserved():
    notes = [{"beat": beat_ms(0), "column": 0, "endbeat": beat_ms(300)}]
    notes += [{"beat": beat_ms(i * 100), "column": 1} for i in range(7)]
    repaired, report = repair_chart(chart(notes))
    holds = [note for note in repaired["note"] if "endbeat" in note]
    assert holds == notes[:1]
    assert report["after_max_fast_run"] <= 4
