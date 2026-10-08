"""Settings-independent, channel-preserving acoustic evidence (no BPM analysis)."""
import hashlib
import json
import bisect
import uuid
from functools import lru_cache
from pathlib import Path
import numpy as np
from scipy.signal import find_peaks, stft

VERSION = 'event-evidence-v2'


def _channels(audio):
    if isinstance(audio, (str, Path)):
        import soundfile as sf
        audio, rate = sf.read(str(audio), dtype='float32', always_2d=True)
        if rate != 44100:
            raise ValueError('Evidence PCM files must retain their original 44100 Hz clock')
    value = np.asarray(audio, dtype=np.float32)
    if value.ndim == 1:
        value = value[:, None]
    if value.ndim != 2 or not len(value) or not np.isfinite(value).all():
        raise ValueError('Evidence requires finite sample-major PCM channels')
    return value


def _analyze_chunk(original, sr=44100, stems=None):
    """Analyze independent channels at ~2 ms hop and two spectral scales.

    Frame spacing is not timing certainty: uncertainty includes analysis-window
    smearing and agreement across scales. No channel averaging is performed.
    """
    if sr != 44100:
        raise ValueError('Original evidence must use 44100 Hz PCM')
    sources = {'original': _channels(original)}
    sources.update({str(k): _channels(v) for k, v in (stems or {}).items()})
    hop = round(sr * .002)
    output = {'version': VERSION, 'sample_rate': sr, 'hop_ms': hop * 1000 / sr,
              'duration_ms': len(sources['original']) * 1000 / sr, 'sources': {}}
    for role, pcm in sources.items():
        channels = []
        for channel in pcm.T:
            if len(channel) < 1024:
                channel = np.pad(channel, (0, 1024-len(channel)))
            scales = []
            for size in (256, 1024):
                frequencies, times, spectrum = stft(channel, fs=sr, nperseg=size,
                                                     noverlap=size-hop, boundary='zeros')
                magnitude = abs(spectrum)
                fluxes = []
                for low, high in ((40, 250), (250, 2000), (2000, sr / 2 + 1)):
                    band = np.log1p(magnitude[(frequencies >= low) & (frequencies < high)] * 100)
                    flux = np.maximum(np.diff(band, axis=1, prepend=band[:, :1]), 0).mean(axis=0)
                    fluxes.append(flux)
                envelope = np.max(fluxes, axis=0)
                reference = max(float(np.percentile(envelope, 95)), float(envelope.max()) * .08, 1e-7)
                envelope /= reference
                peaks, properties = find_peaks(envelope, height=.6, prominence=.35, distance=2)
                rms = np.sqrt(np.mean(magnitude ** 2, axis=0))
                floor = max(float(rms.max()) * .01, 1e-7)
                records = [{'time_ms': float(times[p] * 1000), 'strength': float(envelope[p]),
                            'uncertainty_ms': size * 500 / sr, 'scale': size}
                           for p in peaks if rms[p] >= floor]
                scales.append(records)
                if size == 1024:
                    unit = magnitude / np.maximum(np.linalg.norm(magnitude, axis=0), 1e-10)
                    continuity = np.sum(unit[:, 1:] * unit[:, :-1], axis=0)
                    channels.append({'onsets': [], 'frame_ms': (times * 1000).tolist(),
                                     'energy': rms.tolist(), 'audible_floor': floor,
                                     'spectral_continuity': np.r_[1., continuity].tolist()})
            for record in scales[0]:
                matches = [p for p in scales[1] if abs(p['time_ms']-record['time_ms']) <= 12]
                # Disagreement and nearby independent peaks remain explicit.
                record['multiscale_agreement'] = bool(matches)
                record['scale_support'] = 2 if matches else 1
                record['uncertainty_ms'] = max(record['uncertainty_ms'], min((abs(p['time_ms']-record['time_ms']) for p in matches), default=12.))
                channels[-1]['onsets'].append(record)
            output['sources'][role] = {'channels': channels}
    return output


def analyze_audio(original, sr=44100, stems=None):
    """Bounded spectral memory: independent channels, aligned padded chunks."""
    if sr != 44100:
        raise ValueError('Evidence requires the 44100 Hz source clock')
    sources = {'original': _channels(original), **{str(k): _channels(v) for k, v in (stems or {}).items()}}
    length = len(sources['original'])
    if any(len(pcm) != length for pcm in sources.values()):
        raise ValueError('Stem evidence must share the complete original PCM clock')
    hop = round(sr*.002); core = (12*sr//hop)*hop; halo = 16*hop
    output = {'version': VERSION, 'sample_rate': sr, 'hop_ms': hop*1000/sr,
              'duration_ms': length*1000/sr, 'sources': {}}
    # Process one channel at a time: no six-channel STFT allocation or averaging.
    for role, pcm in sources.items():
        merged = []
        for channel in pcm.T:
            target = {'onsets': [], 'frame_ms': [], 'energy': [], 'spectral_continuity': [], 'audible_floor': 1e-7}
            for start in range(0, length, core):
                stop = min(length, start+core); left = max(0, start-halo); right = min(length, stop+halo)
                part = _analyze_chunk(channel[left:right], sr)['sources']['original']['channels'][0]
                offset = left*1000/sr; a = start*1000/sr; b = stop*1000/sr
                for onset in part['onsets']:
                    onset['time_ms'] += offset
                    if a <= onset['time_ms'] < b:
                        target['onsets'].append(onset)
                for i, local in enumerate(part['frame_ms']):
                    time = local+offset
                    if a <= time < b:
                        target['frame_ms'].append(time)
                        target['energy'].append(part['energy'][i])
                        target['spectral_continuity'].append(part['spectral_continuity'][i])
                target['audible_floor'] = max(target['audible_floor'], part['audible_floor'])
            merged.append(target)
        output['sources'][role] = {'channels': merged}
    return output


@lru_cache(maxsize=3)
def _read_cache(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


_PATH_CACHE = {}


def cached_evidence(original, sr=44100, stems=None, cache_dir=None):
    """Content-addressed cache includes every channel and stem, never settings."""
    if cache_dir is None:
        cache_dir = Path(__file__).resolve().parents[1] / 'data' / 'event_evidence'
    supplied = {'original': original, **(stems or {})}
    path_key = None
    if all(isinstance(value, (str, Path)) for value in supplied.values()):
        path_key = (VERSION, sr, str(cache_dir), tuple((str(role), str(Path(value).resolve()), Path(value).stat().st_size,
                                                     Path(value).stat().st_mtime_ns) for role, value in sorted(supplied.items())))
        if path_key in _PATH_CACHE:
            return _PATH_CACHE[path_key]
    digest = hashlib.sha256((VERSION + str(sr)).encode())
    arrays = {}
    for role, value in sorted({'original': original, **(stems or {})}.items()):
        pcm = _channels(value)
        arrays[role] = pcm
        digest.update(str(role).encode()); digest.update(str(pcm.shape).encode()); digest.update(pcm.tobytes())
    key = digest.hexdigest()
    folder = Path(cache_dir); folder.mkdir(parents=True, exist_ok=True)
    path = folder / (key + '.json')
    if path.exists():
        result = _read_cache(str(path))
        if path_key is not None: _PATH_CACHE[path_key] = result
        return result
    result = analyze_audio(arrays.pop('original'), sr, arrays)
    result['cache_key'] = key
    result['id'] = VERSION + ':' + key
    temporary = folder / (key + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temporary.replace(path)
    if path_key is not None: _PATH_CACHE[path_key] = result
    return result


def head_evidence(event, evidence):
    """Evaluate a supplied model head/tail; never synthesize a chart event."""
    start = event['start_ms']; end = event.get('end_ms')
    role = event.get('source_role')
    if not role and event.get('origins'):
        role = event['origins'][0].get('stem_role')
    roles = ['original'] + ([role] if role in evidence.get('sources', {}) and role != 'original' else [])
    onset_sets = []; sustained = []
    for source in roles:
        for channel_index, channel in enumerate(evidence.get('sources', {}).get(source, {}).get('channels', [])):
            onset_times = channel.get('_onset_times')
            if onset_times is None:
                onset_times = [p['time_ms'] for p in channel['onsets']]
                channel['_onset_times'] = onset_times
            lo = bisect.bisect_left(onset_times, start-15); hi = bisect.bisect_right(onset_times, start+15)
            near = channel['onsets'][lo:hi]
            onset_sets.extend({**peak, 'source_role': source, 'channel': channel_index} for peak in near)
            if end is not None and end > start:
                first = bisect.bisect_left(channel['frame_ms'], start)
                last = bisect.bisect_right(channel['frame_ms'], end+60)
                times = np.asarray(channel['frame_ms'][first:last])
                mask = (times >= start + 25) & (times <= end - 15)
                if mask.sum() >= 3:
                    energy = np.asarray(channel['energy'][first:last])[mask]
                    continuity = np.asarray(channel['spectral_continuity'][first:last])[mask]
                    support = float(np.mean((energy >= channel['audible_floor']) & (continuity >= .9)))
                    attack_first = bisect.bisect_right(onset_times, start+30)
                    attack_last = bisect.bisect_left(onset_times, end-20)
                    rearticulation = sum(p['strength'] >= 1.5 for p in channel['onsets'][attack_first:attack_last])
                    after = (times > end+10) & (times <= end+60)
                    released = bool(after.any() and np.mean(np.asarray(channel['energy'][first:last])[after]) < np.mean(energy)*.5)
                    sustained.append({'support': support, 'rearticulations': rearticulation,
                                      'release_supported': released, 'source_role': source})
    result = {'onsets': onset_sets, 'sustain': sustained}
    matched = role if role in evidence.get('sources', {}) and role != 'original' else 'original'
    result['matched_sustain_role'] = matched
    pool = [item for item in sustained if item['source_role'] == matched]
    if pool:
        # Mixture continuity may belong to a different instrument. Known stem
        # ownership takes priority over the louder original channel.
        best = max(pool, key=lambda p: p['support'] - .15*p['rearticulations'])
        result.update(sustain_support=best['support'], rearticulations=best['rearticulations'], release_supported=best['release_supported'])
    return result
