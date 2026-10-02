import numpy as np
import pytest
from malody_studio.charts import Note, chart_stats, serialize, validate_chart
from malody_studio.difficulty import attacks, calibrate, PRESETS

def test_calibration_has_distinct_density_and_no_hold_lane_conflicts():
    candidates = [(float(t), 1 + (i % 5) / 5,
                   [Note(float(t), i % 4, float(t + 700))])
                  for i, t in enumerate(range(100, 20000, 100))]
    previous = 0
    for key in PRESETS:
        notes, meta = calibrate(candidates, 20000, key, .3, 42)
        stats = chart_stats(notes, 20)
        assert stats['average_nps'] > previous * 1.2
        previous = stats['average_nps']
        assert stats['peak_nps'] <= PRESETS[key]['peak']
        assert validate_chart(serialize(notes, 't', 'a', key, 120), 20000)['valid']
        assert notes == calibrate(candidates, 20000, key, .3, 42)[0]
        assert set(n.start for n in notes) <= set(c[0] for c in candidates)
    assert stats['average_nps'] >= 20

def test_sparse_candidates_report_capacity_instead_of_inventing_notes():
    candidates = [(float(t), 1, []) for t in range(500, 20000, 1000)]
    notes, meta = calibrate(candidates, 20000, 'expert', 0, 42)
    assert len(notes) <= len(candidates) * 4
    assert chart_stats(notes, 20)['average_nps'] < meta['target_active_nps'] * .75
    assert all(n.end is None for n in notes)

def test_lunatic_defaults_limit_chords_and_short_bursts():
    candidates = [(float(t), 1.0, []) for t in range(100, 20000, 50)]
    notes, meta = calibrate(candidates, 20000, 'lunatic', .15, 42)
    chord_sizes = {}
    for note in notes:
        chord_sizes.setdefault(round(note.start), 0)
        chord_sizes[round(note.start)] += 1
    stats = chart_stats(notes, 20)
    assert meta['target_chord_size'] == 2.1
    assert stats['average_nps'] >= 20
    assert stats['peak_nps'] <= 38
    assert max(chord_sizes.values()) <= 3
    assert sum(size == 3 for size in chord_sizes.values()) <= len(chord_sizes) * .2

def test_silent_audio_does_not_get_added_notes_even_from_master():
    assert attacks(np.zeros(22050 * 5, dtype=np.float32), 22050,
                   [Note(1000, 0), Note(2000, 1)]) == []

def test_audio_attacks_are_detected_without_a_model_master():
    sr = 22050
    audio = np.zeros(sr * 5, dtype=np.float32)
    for second in (1, 2, 3, 4):
        audio[second*sr:second*sr+400] = np.hanning(400).astype(np.float32)
    candidates = attacks(audio, sr, [])
    assert all(any(abs(t - second*1000) < 60 for t, _, _ in candidates) for second in (1, 2, 3, 4))
    assert not any(1200 < t < 1800 for t, _, _ in candidates)
