"""Window-prioritized, immutable spectrogram tiles on the audio sample grid.

This module never changes charts, playback or judgment offsets.  Tile timestamps
are centers in the supplied WAV's time domain; origin_sample is provenance only.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import firwin, resample_poly
from scipy.signal.windows import hann

SOURCE_SR = 44100
ANALYSIS_SR = 22050
N_FFT = 1024
HOP = 110
TILE_FRAMES = 1024
BINS = 192
STRIDES = (1, 2, 4, 8, 16)
DB_FLOOR = -90.0
VERSION = "alignment-2.1"
PARAMETERS = dict(version=VERSION, source_sr=SOURCE_SR, analysis_sr=ANALYSIS_SR,
                  n_fft=N_FFT, hop_samples=HOP, center=True, window="hann-periodic",
                  pad_mode="constant", resample="polyphase-fir41-kaiser5",
                  channels="mean-channel-power", bins=BINS, tile_frames=TILE_FRAMES,
                  strides=STRIDES, db_floor=DB_FLOOR, db_ceiling=0.0,
                  db_reference="window-normalized-amplitude-1", aggregation="power-mean-and-peak")
FILTER = firwin(41, .5, window=("kaiser", 5.0))
WINDOW = hann(N_FFT, sym=False).astype(np.float32)
EDGES = np.r_[0.0, np.geomspace(ANALYSIS_SR / N_FFT, ANALYSIS_SR / 2, BINS)]


def _frequency_weights():
    centers = np.arange(N_FFT // 2 + 1) * ANALYSIS_SR / N_FFT
    low = np.maximum(0, centers - ANALYSIS_SR / N_FFT / 2)
    high = np.minimum(ANALYSIS_SR / 2, centers + ANALYSIS_SR / N_FFT / 2)
    overlap = np.maximum(0, np.minimum(EDGES[1:, None], high) - np.maximum(EDGES[:-1, None], low))
    return (overlap / np.maximum(overlap.sum(axis=1, keepdims=True), 1e-20)).astype(np.float32)


WEIGHTS = _frequency_weights()


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def analysis_samples(path, start, end):
    """Read globally aligned 22.05k samples, with a finite FIR halo.

    Every source chunk starts at an even 44.1k sample. The explicit FIR's half
    support is 20 source samples; 40 samples of context safely includes it.
    Only physical file boundaries use zero padding. Negative analysis indexes
    are valid STFT halo and produce zeros, never a new local audio origin.
    """
    with sf.SoundFile(path) as stream:
        if stream.samplerate != SOURCE_SR:
            raise ValueError("频谱音频必须为 44100 Hz")
        count = math.ceil(len(stream) / 2)
        result = np.zeros((max(0, end - start), stream.channels), dtype=np.float32)
        left, right = max(0, start), min(count, end)
        if right <= left:
            return result
        source_left = max(0, 2 * left - 40)
        source_right = min(len(stream), 2 * right + 40)
        stream.seek(source_left)
        audio = stream.read(source_right - source_left, dtype="float32", always_2d=True)
    if not np.isfinite(audio).all():
        raise ValueError("频谱音频包含无效采样")
    resampled = resample_poly(audio, 1, 2, axis=0, window=FILTER, padtype="constant")
    offset = left - source_left // 2
    result[left - start:right - start] = resampled[offset:offset + right - left]
    return result


def power_frames(path, first_frame, frame_count):
    """Return [frames, display_frequency_bins] normalized channel power."""
    if frame_count <= 0:
        return np.empty((0, BINS), dtype=np.float32)
    start = first_frame * HOP - N_FFT // 2
    end = (first_frame + frame_count - 1) * HOP + N_FFT // 2
    audio = analysis_samples(path, start, end)
    frames = np.lib.stride_tricks.sliding_window_view(audio, N_FFT, axis=0)[::HOP]
    frames = frames[:frame_count] * WINDOW
    spectrum = np.fft.rfft(frames, axis=-1)
    scale = np.full(N_FFT // 2 + 1, 2 / WINDOW.sum(), dtype=np.float32)
    scale[[0, -1]] /= 2
    power = np.mean(np.abs(spectrum * scale) ** 2, axis=1).astype(np.float32)
    return power @ WEIGHTS.T


def aggregate(power, stride):
    """Power pooling preserves a short attack instead of skipping frames."""
    groups = math.ceil(len(power) / stride)
    means = np.empty((groups, BINS), dtype=np.float32)
    peaks = np.empty_like(means)
    for group in range(groups):
        block = power[group * stride:(group + 1) * stride]
        means[group], peaks[group] = block.mean(axis=0), block.max(axis=0)
    return means, peaks


def quantize(power):
    db = 10 * np.log10(np.maximum(power, 1e-20))
    return np.rint(np.clip((db - DB_FLOOR) / -DB_FLOOR, 0, 1) * 255).astype(np.uint8)


class _Scheduler:
    """One CPU worker across projects; each current window outranks prefetch."""
    def __init__(self):
        self.condition = threading.Condition()
        self.queue = []
        self.pending = {}
        self.serial = 0
        threading.Thread(target=self._run, name="spectrum-tiles", daemon=True).start()

    def add(self, service, aid, index, priority):
        key = (str(service.directory), aid, index)
        with self.condition:
            previous = self.pending.get(key)
            if previous and previous[0] <= priority:
                return
            self.serial += 1
            # A seek's latest current window outranks earlier queued windows.
            serial = -self.serial if priority == 0 else self.serial
            token = (priority, serial)
            self.pending[key] = token
            heapq.heappush(self.queue, (priority, serial, key, service, aid, index))
            self.condition.notify()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: bool(self.queue))
                priority, serial, key, service, aid, index = heapq.heappop(self.queue)
                if self.pending.get(key) != (priority, serial):
                    continue
                self.pending.pop(key, None)
            service._compute(aid, index)


_scheduler = None
_scheduler_lock = threading.Lock()
_manifest_lock = threading.RLock()


def _queue():
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = _Scheduler()
        return _scheduler


class SpectrumService:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.cache = self.directory / "analysis" / "spectrum"
        self.cache.mkdir(parents=True, exist_ok=True)
        self._fingerprints = {}

    def _folder(self, aid):
        if not isinstance(aid, str) or not re.fullmatch(r"[0-9a-f]{64}", aid):
            raise ValueError("无效的频谱分析 ID")
        return self.cache / aid

    def _source(self, path):
        path = Path(path).resolve()
        if not path.is_relative_to(self.directory) or not path.is_file():
            raise ValueError("频谱音频必须来自当前项目")
        return path

    def _fingerprint(self, path):
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        saved = self._fingerprints.get(str(path))
        if saved and saved[0] == signature:
            return saved[1], signature
        value = _hash(path)
        self._fingerprints[str(path)] = (signature, value)
        return value, signature

    def request(self, source_id, audio_path, start_ms=0, end_ms=5000, origin_sample=0):
        if not isinstance(source_id, str) or not source_id or len(source_id) > 200:
            raise ValueError("无效的频谱来源")
        if isinstance(origin_sample, bool) or not isinstance(origin_sample, int):
            raise ValueError("频谱来源原点必须为采样点整数")
        path = self._source(audio_path)
        info = sf.info(path)
        if info.samplerate != SOURCE_SR or info.frames < 1 or info.channels > 8:
            raise ValueError("频谱音频必须为有效 44100 Hz 音频")
        digest, signature = self._fingerprint(path)
        identity = dict(parameters=PARAMETERS, audio_sha256=digest, source_id=source_id, origin_sample=origin_sample)
        aid = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        folder = self._folder(aid)
        folder.mkdir(exist_ok=True)
        manifest_path = folder / "manifest.json"
        with _manifest_lock:
            if not manifest_path.exists():
                total = math.ceil(info.frames / 2) // HOP + 1
                value = dict(schema_version=2, analysis_version=VERSION, analysis_id=aid,
                             status="queued", source_id=source_id, audio_sha256=digest,
                             sample_rate=SOURCE_SR, samples=info.frames,
                             duration_ms=info.frames / SOURCE_SR * 1000,
                             origin_sample=origin_sample, frame_origin_sample=0,
                             total_frames=total, dt_ms=HOP / ANALYSIS_SR * 1000,
                             frequency_edges_hz=EDGES.tolist(), parameters=PARAMETERS,
                             bins=BINS, planes=["mean", "peak"], dtype="uint8",
                             order="plane-frame-frequency", tile_frames=TILE_FRAMES,
                             levels=[dict(level=i, stride=s) for i, s in enumerate(STRIDES)],
                             ready_tiles=[], tile_hashes={}, errors={}, created=time.time(),
                             audio_path=str(path.relative_to(self.directory)), signature=list(signature))
                _atomic_json(manifest_path, value)
        return self.manifest(aid, start_ms, end_ms)

    def _load(self, aid):
        value = _read_json(self._folder(aid) / "manifest.json")
        if value.get("parameters") != json.loads(json.dumps(PARAMETERS)):
            raise ValueError("频谱缓存参数已过期，请重新准备")
        path = self._source(self.directory / value["audio_path"])
        digest, signature = self._fingerprint(path)
        if digest != value["audio_sha256"]:
            raise ValueError("频谱音频已变化，请重新准备")
        return value, path

    def manifest(self, aid, start_ms=None, end_ms=None):
        with _manifest_lock:
            value, _ = self._load(aid)
        if start_ms is not None or end_ms is not None:
            self._schedule(aid, value, start_ms or 0, end_ms if end_ms is not None else value["duration_ms"])
        return {key: data for key, data in value.items() if key not in ("audio_path", "signature")}

    def _schedule(self, aid, value, start, end):
        if isinstance(start, bool) or isinstance(end, bool) or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (start, end)) or end <= start:
            raise ValueError("频谱时间范围无效")
        span = TILE_FRAMES * value["dt_ms"]
        total = math.ceil(value["total_frames"] / TILE_FRAMES)
        first = max(0, min(total - 1, math.floor(max(0, start) / span)))
        last = max(first, min(total - 1, math.floor(max(0, end - .000001) / span)))
        current = list(range(first, last + 1))
        prefetch = [i for i in (first - 1, last + 1) if 0 <= i < total]
        for priority, indices in ((0, current), (1, prefetch)):
            for index in indices:
                if index not in value["ready_tiles"] or not self._valid_files(aid, value, index):
                    _queue().add(self, aid, index, priority)

    def _valid_files(self, aid, value, index):
        count = min(TILE_FRAMES, value["total_frames"] - index * TILE_FRAMES)
        for level, stride in enumerate(STRIDES):
            path = self._folder(aid) / f"{level}-{index}.bin"
            if not path.is_file() or path.stat().st_size != 2 * math.ceil(count / stride) * BINS:
                return False
            if value.get("tile_hashes", {}).get(f"{level}-{index}") != _hash(path):
                return False
        return True

    def _compute(self, aid, index):
        try:
            with _manifest_lock:
                value, path = self._load(aid)
                if self._valid_files(aid, value, index):
                    return
            count = min(TILE_FRAMES, value["total_frames"] - index * TILE_FRAMES)
            power = power_frames(path, index * TILE_FRAMES, count)
            folder = self._folder(aid)
            # Validate the source again before publishing a completed tile set.
            with _manifest_lock:
                current, _ = self._load(aid)
            checksums = {}
            for level, stride in enumerate(STRIDES):
                mean, peak = aggregate(power, stride)
                target = folder / f"{level}-{index}.bin"
                temporary = target.with_suffix(".bin.tmp")
                temporary.write_bytes(np.stack((quantize(mean), quantize(peak))).tobytes())
                temporary.replace(target)
                checksums[f"{level}-{index}"] = _hash(target)
            with _manifest_lock:
                current, _ = self._load(aid)
                current["ready_tiles"] = sorted(set(current["ready_tiles"] + [index]))
                current.setdefault("tile_hashes", {}).update(checksums)
                current["errors"].pop(str(index), None)
                total = math.ceil(current["total_frames"] / TILE_FRAMES)
                current["status"] = "ready" if len(current["ready_tiles"]) == total else "partial"
                _atomic_json(folder / "manifest.json", current)
        except Exception as exc:
            with _manifest_lock:
                manifest = self._folder(aid) / "manifest.json"
                if manifest.is_file():
                    value = _read_json(manifest)
                    value["errors"][str(index)] = str(exc)
                    value["status"] = "failed" if not value["ready_tiles"] else "partial"
                    _atomic_json(manifest, value)

    def tile(self, aid, level, index):
        if isinstance(level, bool) or isinstance(index, bool) or not isinstance(level, int) or not isinstance(index, int) or not 0 <= level < len(STRIDES) or index < 0:
            raise ValueError("无效的频谱块")
        with _manifest_lock:
            value, _ = self._load(aid)
        if index >= math.ceil(value["total_frames"] / TILE_FRAMES):
            raise ValueError("频谱块越过音频边界")
        if index in value["ready_tiles"] and self._valid_files(aid, value, index):
            return self._folder(aid) / f"{level}-{index}.bin"
        _queue().add(self, aid, index, 0)
        return None

    def alignment_matrix(self, aid, start_ms, end_ms):
        """Cached level-zero dB values for static PNG, or None while queued.

        Return centers explicitly: PNG consumers must not stretch columns to a
        requested range with a different first/last frame center.
        """
        value = self.manifest(aid, start_ms, end_ms)
        first = max(0, math.ceil(start_ms / value["dt_ms"]))
        last = min(value["total_frames"], math.ceil(end_ms / value["dt_ms"]))
        columns = []
        for index in range(first // TILE_FRAMES, math.ceil(last / TILE_FRAMES)):
            path = self.tile(aid, 0, index)
            if path is None:
                return None
            count = min(TILE_FRAMES, value["total_frames"] - index * TILE_FRAMES)
            data = np.frombuffer(path.read_bytes(), dtype=np.uint8).reshape(2, count, BINS)[0]
            a, b = max(0, first - index * TILE_FRAMES), min(count, last - index * TILE_FRAMES)
            columns.append(data[a:b])
        pixels = np.concatenate(columns) if columns else np.empty((0, BINS), dtype=np.uint8)
        return dict(times_ms=np.arange(first, last) * value["dt_ms"],
                    db=DB_FLOOR + pixels.astype(np.float32) / 255 * -DB_FLOOR,
                    analysis_id=aid, frequency_edges_hz=EDGES)
