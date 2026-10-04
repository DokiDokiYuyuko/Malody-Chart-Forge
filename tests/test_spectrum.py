import json
import math
import heapq
import threading

import numpy as np
import pytest
import soundfile as sf
from scipy.signal import resample_poly

from malody_studio import spectrum as s


class Queue:
    def __init__(self):
        self.tasks = []

    def add(self, service, aid, index, priority):
        self.tasks.append((service, aid, index, priority))


@pytest.fixture
def audio(tmp_path, monkeypatch):
    queue = Queue()
    monkeypatch.setattr(s, "_queue", lambda: queue)
    samples = s.SOURCE_SR * 7 + 1
    rng = np.random.default_rng(55)
    data = rng.normal(0, .03, (samples, 2)).astype(np.float32)
    data[1234800 % samples] += .5
    path = tmp_path / "source.wav"
    sf.write(path, data, s.SOURCE_SR, subtype="FLOAT")
    return tmp_path, path, data, queue


def test_chunk_resampling_is_identical_on_global_grid_including_edges(audio):
    _, path, data, _ = audio
    complete = resample_poly(data, 1, 2, axis=0, window=s.FILTER, padtype="constant")
    for first, last in ((0, 1024), (511, 2093), (s.HOP * s.TILE_FRAMES - 512, s.HOP * s.TILE_FRAMES + 700), (len(complete) - 1031, len(complete))):
        chunk = s.analysis_samples(path, first, last)
        np.testing.assert_allclose(chunk, complete[first:last], atol=1e-7, rtol=1e-6)
    padded = s.analysis_samples(path, -512, len(complete) + 512)
    assert not padded[:512].any() and not padded[-512:].any()
    np.testing.assert_allclose(padded[512:-512], complete, atol=1e-7, rtol=1e-6)


def test_stft_tiles_equal_whole_window_without_local_center_reset(audio):
    _, path, _, _ = audio
    first = s.TILE_FRAMES - 7
    combined = s.power_frames(path, first, 21)
    left, right = s.power_frames(path, first, 7), s.power_frames(path, s.TILE_FRAMES, 14)
    np.testing.assert_allclose(combined, np.concatenate((left, right)), atol=1e-9, rtol=2e-5)


def test_centered_impulse_timestamp_is_not_shifted_half_window(tmp_path):
    signal = np.zeros((s.SOURCE_SR, 2), np.float32)
    frame = 83
    signal[frame * s.HOP * 2] = 1
    path = tmp_path / "impulse.wav"
    sf.write(path, signal, s.SOURCE_SR, subtype="FLOAT")
    power = s.power_frames(path, frame - 8, 17)
    strongest = frame - 8 + int(np.argmax(power.sum(axis=1)))
    assert strongest == frame
    assert strongest * s.HOP * 2 == np.argmax(signal[:, 0])


def test_antiphase_stereo_keeps_energy_and_lod_keeps_short_attacks(tmp_path):
    t = np.arange(s.SOURCE_SR) / s.SOURCE_SR
    wave = (.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    path = tmp_path / "antiphase.wav"
    sf.write(path, np.stack((wave, -wave), axis=1), s.SOURCE_SR, subtype="FLOAT")
    assert s.power_frames(path, 20, 30).max() > .001
    power = np.zeros((33, s.BINS), np.float32)
    power[15, 9] = 1
    mean, peak = s.aggregate(power, 16)
    assert mean[0, 9] == 1 / 16 and peak[0, 9] == 1
    assert len(mean) == 3 and not mean[2].any()
    assert s.quantize(peak)[0, 9] == 255
    assert s.quantize(np.array([1e-9, 1, 10])).tolist() == [0, 255, 255]


def test_cache_identity_priority_tiles_and_png_values(audio):
    directory, path, _, queue = audio
    service = s.SpectrumService(directory)
    m = service.request("original", path, 5500, 6000)
    aid = m["analysis_id"]
    assert queue.tasks[0][2:] == (1, 0)
    assert queue.tasks[-1][2:] == (0, 1)
    assert "audio_path" not in m and m["frame_origin_sample"] == 0
    service._compute(aid, 1)
    assert service.tile(aid, 0, 1).is_file()
    assert service.manifest(aid)["ready_tiles"] == [1]
    result = service.alignment_matrix(aid, 5500, 5600)
    assert result is not None
    assert result["db"].shape[1] == 192
    np.testing.assert_allclose(np.diff(result["times_ms"]), s.HOP / s.ANALYSIS_SR * 1000)
    assert result["times_ms"][0] >= 5500 and result["times_ms"][-1] < 5600
    assert service.request("original", path, 5500, 5600)["analysis_id"] == aid
    assert service.request("vocals", path, 5500, 5600)["analysis_id"] != aid
    assert service.request("original", path, 5500, 5600, origin_sample=1234800)["analysis_id"] != aid
    service._compute(aid, 0)
    assert service.manifest(aid)["status"] == "ready"


def test_changed_audio_invalidates_old_analysis_and_corrupt_tile_recovers(audio):
    directory, path, data, _ = audio
    service = s.SpectrumService(directory)
    aid = service.request("original", path)["analysis_id"]
    service._compute(aid, 0)
    tile = service.tile(aid, 0, 0)
    content = bytearray(tile.read_bytes())
    content[55] ^= 255
    tile.write_bytes(content)
    assert service.tile(aid, 0, 0) is None
    service._compute(aid, 0)
    assert service.tile(aid, 0, 0).read_bytes() != content
    data[0] += .02
    sf.write(path, data, s.SOURCE_SR, subtype="FLOAT")
    with pytest.raises(ValueError, match="已变化"):
        service.manifest(aid)
    assert service.request("original", path)["analysis_id"] != aid


def test_invalid_ranges_paths_parameters_and_partial_tile(audio, tmp_path):
    directory, path, _, _ = audio
    service = s.SpectrumService(directory)
    with pytest.raises(ValueError):
        service.request("original", path, 1, float("nan"))
    with pytest.raises(ValueError):
        service.request("original", path, 1, 0)
    aid = service.request("assembly:test", path)["analysis_id"]
    manifest_path = service._folder(aid) / "manifest.json"
    for args in ((aid, True, 0), (aid, 0, -1), (aid, 5, 0), (aid, 0, 99)):
        with pytest.raises(ValueError):
            service.tile(*args)
    with pytest.raises(ValueError):
        service.manifest("../source.wav")
    service._compute(aid, 1)
    m = service.manifest(aid)
    count = m["total_frames"] - s.TILE_FRAMES
    assert service.tile(aid, 4, 1).stat().st_size == 2 * math.ceil(count / 16) * s.BINS
    saved = json.loads(manifest_path.read_text())
    saved["parameters"]["hop_samples"] = 220
    manifest_path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="过期"):
        service.manifest(aid)


def test_actual_assembly_audio_has_silent_preroll_and_real_seam(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "_queue", lambda: Queue())
    data = np.zeros((s.SOURCE_SR * 3, 2), np.float32)
    t = np.arange(s.SOURCE_SR * 2) / s.SOURCE_SR
    data[s.SOURCE_SR:] = (.15 * np.sin(2 * np.pi * 880 * t))[:, None]
    data[s.SOURCE_SR:s.SOURCE_SR + 221] *= np.linspace(0, 1, 221)[:, None]
    path = tmp_path / "assembled.wav"
    sf.write(path, data, s.SOURCE_SR, subtype="FLOAT")
    service = s.SpectrumService(tmp_path)
    aid = service.request("assembly:test", path)["analysis_id"]
    service._compute(aid, 0)
    values = service.alignment_matrix(aid, 0, 2500)
    assert np.all(values["db"][values["times_ms"] < 900] == s.DB_FLOOR)
    assert values["db"][values["times_ms"] > 1200].max() > -30


def test_seek_promotes_latest_window_ahead_of_old_window_and_prefetch(tmp_path):
    scheduler = object.__new__(s._Scheduler)
    scheduler.condition = threading.Condition()
    scheduler.queue, scheduler.pending, scheduler.serial = [], {}, 0
    service = s.SpectrumService(tmp_path)
    scheduler.add(service, "x", 0, 0)
    scheduler.add(service, "x", 1, 1)
    scheduler.add(service, "x", 30, 0)
    assert heapq.heappop(scheduler.queue)[-1] == 30
    assert heapq.heappop(scheduler.queue)[-1] == 0
    assert heapq.heappop(scheduler.queue)[-1] == 1
