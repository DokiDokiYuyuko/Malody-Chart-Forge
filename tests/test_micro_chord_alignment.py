from copy import deepcopy

from malody_studio.micro_chord_alignment import normalize_chart


def chart(notes):
    return {"meta": {"song": {"title": "test", "artist": "test"}},
            "time": [{"beat": [0, 0, 1], "bpm": 120.0}], "effect": [], "note": notes}


def test_one_stray_head_joins_exact_chord_without_changing_other_events():
    original = chart([{"beat": [10, 0, 1], "column": 0},
                      {"beat": [10, 0, 1], "column": 2},
                      {"beat": [10, 1, 50], "column": 3},
                      {"beat": [10, 1, 20], "column": 1}])
    saved = deepcopy(original)
    corrected, summary = normalize_chart(original)
    assert original == saved
    assert summary["moved_heads"] == 1
    assert corrected["note"][2]["beat"] == [10, 0, 1]
    assert corrected["note"][3]["beat"] == [10, 1, 20]  # 25 ms stagger stays
    assert len(corrected["note"]) == len(original["note"])


def test_same_lane_and_crossing_hold_are_not_merged():
    original = chart([{"beat": [10, 0, 1], "column": 0},
                      {"beat": [10, 0, 1], "column": 2},
                      {"beat": [10, 1, 50], "column": 0},
                      {"beat": [9, 1, 2], "column": 3, "endbeat": [10, 1, 2]},
                      {"beat": [10, 1, 50], "column": 3}])
    corrected, summary = normalize_chart(original)
    assert corrected == original
    assert summary["moved_heads"] == 0


def test_hold_head_moves_but_release_keeps_its_musical_position():
    original = chart([{"beat": [10, 0, 1], "column": 0},
                      {"beat": [10, 0, 1], "column": 2},
                      {"beat": [10, 1, 50], "column": 3, "endbeat": [11, 0, 1]}])
    corrected, summary = normalize_chart(original)
    assert summary["moved_heads"] == 1
    assert corrected["note"][2]["beat"] == [10, 0, 1]
    assert corrected["note"][2]["endbeat"] == [11, 0, 1]


def test_nonadjacent_pair_and_split_four_key_chord_align():
    original = chart([{"beat": [10, 0, 1], "column": 0},
                      {"beat": [10, 1, 50], "column": 3},
                      {"beat": [11, 0, 1], "column": 0},
                      {"beat": [11, 0, 1], "column": 2},
                      {"beat": [11, 1, 50], "column": 1},
                      {"beat": [11, 1, 50], "column": 3}])
    corrected, summary = normalize_chart(original)
    assert summary["moved_heads"] == 3
    assert corrected["note"][1]["beat"] == [10, 0, 1]
    assert corrected["note"][4]["beat"] == [11, 0, 1]
    assert corrected["note"][5]["beat"] == [11, 0, 1]


def test_ten_millisecond_chain_joins_one_four_key_chord():
    original = chart([{"beat": [10, 0, 1], "column": 2},
                      {"beat": [10, 1, 50], "column": 0},
                      {"beat": [10, 1, 50], "column": 1},
                      {"beat": [10, 1, 25], "column": 3}])
    corrected, summary = normalize_chart(original)
    assert summary["moved_heads"] == 2
    assert {tuple(note["beat"]) for note in corrected["note"]} == {(10, 1, 50)}


def test_twenty_millisecond_split_double_and_thirty_millisecond_anchor_align():
    split = chart([{"beat": [10, 0, 1], "column": 0},
                   {"beat": [10, 0, 1], "column": 2},
                   {"beat": [10, 1, 25], "column": 1},
                   {"beat": [10, 1, 25], "column": 3}])
    corrected, summary = normalize_chart(split)
    assert summary["moved_heads"] == 2
    assert all(note["beat"] == [10, 0, 1] for note in corrected["note"])

    anchored = chart([{"beat": [10, 0, 1], "column": 0},
                      {"beat": [10, 3, 50], "column": 2},
                      {"beat": [10, 3, 50], "column": 3}])
    corrected, summary = normalize_chart(anchored)
    assert summary["moved_heads"] == 1
    assert all(note["beat"] == [10, 3, 50] for note in corrected["note"])


def test_wider_stagger_and_nearby_same_lane_stay_separate():
    stagger = chart([{"beat": [10, 0, 1], "column": 0},
                     {"beat": [10, 2, 25], "column": 1},
                     {"beat": [10, 2, 25], "column": 2}])
    assert normalize_chart(stagger)[0] == stagger
    repeat = chart([{"beat": [9, 47, 50], "column": 2},
                    {"beat": [9, 4, 5], "column": 3, "endbeat": [11, 0, 1]},
                    {"beat": [10, 0, 1], "column": 0},
                    {"beat": [10, 0, 1], "column": 1},
                    {"beat": [10, 1, 25], "column": 2}])
    assert normalize_chart(repeat)[0] == repeat


def test_nearby_same_lane_that_moves_farther_away_does_not_block_alignment():
    original = chart([{"beat": [9, 22, 25], "column": 2},
                      {"beat": [10, 0, 1], "column": 2},
                      {"beat": [10, 1, 50], "column": 0},
                      {"beat": [10, 1, 50], "column": 1}])
    corrected, summary = normalize_chart(original)
    assert summary["moved_heads"] == 1
    assert corrected["note"][1]["beat"] == [10, 1, 50]


def test_equal_gap_chain_uses_unique_exact_chord_anchor():
    original = chart([{"beat": [10, 0, 1], "column": 0},
                      {"beat": [10, 3, 50], "column": 2},
                      {"beat": [10, 3, 25], "column": 0},
                      {"beat": [10, 3, 25], "column": 3}])
    corrected, summary = normalize_chart(original)
    assert summary["moved_heads"] == 1
    assert corrected["note"][1]["beat"] == [10, 3, 25]
    assert corrected["note"][0]["beat"] == [10, 0, 1]

    ambiguous = chart([{"beat": [10, 0, 1], "column": 0},
                       {"beat": [10, 0, 1], "column": 1},
                       {"beat": [10, 3, 50], "column": 2},
                       {"beat": [10, 3, 25], "column": 0},
                       {"beat": [10, 3, 25], "column": 3}])
    assert normalize_chart(ambiguous)[0] == ambiguous


def test_symmetric_scatter_around_exact_double_becomes_one_four_key_chord():
    original = chart([{"beat": [10, 0, 1], "column": 0},
                      {"beat": [10, 3, 50], "column": 1},
                      {"beat": [10, 3, 50], "column": 2},
                      {"beat": [10, 3, 25], "column": 3}])
    corrected, summary = normalize_chart(original)
    assert summary["moved_heads"] == 2
    assert all(note["beat"] == [10, 3, 50] for note in corrected["note"])

    repeated_lane = chart([{"beat": [10, 0, 1], "column": 0},
                           {"beat": [10, 3, 50], "column": 1},
                           {"beat": [10, 3, 50], "column": 2},
                           {"beat": [10, 3, 25], "column": 0}])
    assert normalize_chart(repeated_lane)[0] == repeated_lane


def test_stray_head_can_use_a_free_lane_instead_of_creating_a_fast_jack():
    original = chart([{"beat": [9, 23, 25], "column": 0},
                      {"beat": [10, 0, 1], "column": 1},
                      {"beat": [10, 0, 1], "column": 3},
                      {"beat": [10, 1, 50], "column": 0}])
    corrected, summary = normalize_chart(original)
    assert summary["moved_heads"] == 1
    assert summary["relaned_heads"] == 1
    assert corrected["note"][3]["beat"] == [10, 0, 1]
    assert corrected["note"][3]["column"] == 2
