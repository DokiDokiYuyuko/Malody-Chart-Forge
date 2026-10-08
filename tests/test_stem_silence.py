import numpy as np
import pytest

from malody_studio import stem_silence as ss

SR = 1000  # 100 ms frames are 100 samples


def tone(seconds, level=1.0):
    t = np.arange(int(seconds * SR))
    return (level * np.sin(2 * np.pi * 50 * t / SR)).astype(np.float32)


def test_frames_levels_are_relative_to_the_file_peak_not_absolute():
    quiet = np.concatenate([tone(1, .5), tone(1, .005)])  # -40 dB below the loud half
    levels = ss.frame_levels_db(quiet, SR)
    assert levels.shape == (20,)
    assert levels[:10].max() > -4 and levels[:10].min() < -2.9  # rms of a sine is peak - 3 dB
    assert np.allclose(levels[10:], levels[:10] - 40, atol=.2)
    # the same audio scaled to any absolute level gives the same relative levels
    assert np.allclose(ss.frame_levels_db(quiet * .01, SR), levels, atol=.01)


def test_peak_above_one_and_stereo_input_are_handled():
    loud = tone(1, 8.0)
    assert np.allclose(ss.frame_levels_db(loud, SR), ss.frame_levels_db(loud / 8, SR), atol=.01)
    stereo = np.stack([loud, loud], axis=1)
    assert ss.frame_levels_db(stereo, SR).shape == (10,)


def test_trailing_partial_frame_is_kept_and_all_silent_file_has_no_activity():
    levels = ss.frame_levels_db(tone(1.05), SR)
    assert levels.shape == (11,)
    zero = ss.frame_levels_db(np.zeros(2000, np.float32), SR)
    assert zero.shape == (20,) and np.all(zero <= ss.FLOOR_DB)
    assert ss.frame_levels_db(np.zeros(0, np.float32), SR).shape == (0,)


def audio(*parts):
    return np.concatenate([tone(s, 1.0) if on else np.zeros(int(s * SR), np.float32) for on, s in parts])


def test_silent_runs_need_the_minimum_duration_and_a_guard_is_kept_active():
    y = audio((1, 2), (0, 4.9), (1, 2), (0, 5.0), (1, 2))
    levels = ss.frame_levels_db(y, SR)
    runs = ss.silent_runs(levels, min_run_ms=5000, guard_ms=500)
    assert len(runs) == 1
    run = runs[0]
    assert (run['start_ms'], run['end_ms']) == (8900, 13900)
    assert (run['declared_start_ms'], run['declared_end_ms']) == (9400, 13400)
    assert run['level_db'] <= ss.THRESHOLD_DB
    # a shorter minimum finds both runs
    assert len(ss.silent_runs(levels, min_run_ms=4000, guard_ms=500)) == 2


def test_runs_touching_the_file_edges_have_no_guard_on_that_side_and_all_silent_is_one_run():
    y = audio((0, 6), (1, 2), (0, 6))
    runs = ss.silent_runs(ss.frame_levels_db(y, SR), min_run_ms=5000, guard_ms=500)
    assert [(r['start_ms'], r['end_ms']) for r in runs] == [(0, 6000), (8000, 14000)]
    assert [(r['declared_start_ms'], r['declared_end_ms']) for r in runs] == [(0, 5500), (8500, 14000)]
    whole = ss.silent_runs(ss.frame_levels_db(np.zeros(8000, np.float32), SR), min_run_ms=5000, guard_ms=500)
    assert [(r['declared_start_ms'], r['declared_end_ms']) for r in whole] == [(0, 8000)]


def test_a_run_shorter_than_twice_the_guard_declares_nothing():
    y = audio((1, 2), (0, 1.0), (1, 2))
    assert ss.silent_runs(ss.frame_levels_db(y, SR), min_run_ms=500, guard_ms=500) == []


def test_range_activity_counts_frames_fully_inside_the_range():
    y = audio((1, 3), (0, 3), (1, 4))
    levels = ss.frame_levels_db(y, SR)
    full = ss.range_activity(levels, 0, 10000)
    assert full['frames'] == 100 and full['silent_frames'] == 30 and full['silent_fraction'] == pytest.approx(.3)
    assert full['longest_silent_run_ms'] == 3000
    half = ss.range_activity(levels, 2000, 6000)  # 1 s tone then 3 s silence
    assert half['frames'] == 40 and half['silent_fraction'] == pytest.approx(.75) and half['longest_silent_run_ms'] == 3000
    assert half['threshold_db'] == ss.THRESHOLD_DB and half['peak_relative_rms_db'] > ss.THRESHOLD_DB
    # a range with no whole frame has no evidence and is never "silent"
    empty = ss.range_activity(levels, 50, 120)
    assert empty['frames'] == 0 and empty['silent_fraction'] == 0.
    assert ss.range_activity(levels, 9000, 15000)['frames'] == 10  # clipped to the file


def test_all_silent_range_reports_fraction_one_with_finite_numbers():
    levels = ss.frame_levels_db(np.zeros(5000, np.float32), SR)
    got = ss.range_activity(levels, 0, 5000)
    assert got['silent_fraction'] == 1.0 and np.isfinite(got['peak_relative_rms_db'])
