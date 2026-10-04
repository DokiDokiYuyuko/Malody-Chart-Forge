"""Peak-safe audition copies; never alter model PCM, timing, or stem identity."""
import hashlib
import json
import threading
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

_lock = threading.Lock()
VERSION = 'peak-safe-v1'


def audition_metadata(source, project_directory):
    """RMS is descriptive and used only for optional playback attenuation."""
    source, directory = Path(source).resolve(), Path(project_directory).resolve()
    if not source.is_relative_to(directory):
        raise ValueError('试听音源路径越过项目目录')
    stat=source.stat()
    key=hashlib.sha256(f'metadata-v1:{source}:{stat.st_size}:{stat.st_mtime_ns}'.encode()).hexdigest()
    cache=directory/'preview-audio';target=cache/(key+'.json')
    with _lock:
        if target.is_file():return json.loads(target.read_text(encoding='utf-8'))
        peak, square_sum, count=0.,0.,0
        with sf.SoundFile(source) as audio:
            frames, rate, channels=audio.frames,audio.samplerate,audio.channels
            for block in audio.blocks(blocksize=262144,dtype='float32',always_2d=True):
                if not np.isfinite(block).all():raise ValueError('试听音频含无效采样')
                peak=max(peak,float(np.max(np.abs(block),initial=0.)))
                square_sum+=float(np.sum(block.astype(np.float64)**2));count+=block.size
        gain=gain_for_peak(peak);rms=float(np.sqrt(square_sum/max(1,count)))
        record={'frames':frames,'sample_rate':rate,'channels':channels,'peak':peak,'rms':rms,
                'audition_gain':gain,'audition_rms':rms*gain}
        cache.mkdir(exist_ok=True)
        from .advanced import atomic
        atomic(target,record)
        return record


def gain_for_peak(peak):
    if not np.isfinite(peak) or peak < 0:
        raise ValueError('音频峰值无效')
    return .98 / peak if peak > 1. else 1.


def audition_file(source, project_directory):
    """Uniform attenuation only above full scale, retaining float PCM and frames."""
    source = Path(source).resolve()
    directory = Path(project_directory).resolve()
    if not source.is_relative_to(directory):
        raise ValueError('试听音源路径越过项目目录')
    info = source.stat()
    key = hashlib.sha256(f'{VERSION}:{source}:{info.st_size}:{info.st_mtime_ns}'.encode()).hexdigest()
    cache = directory / 'preview-audio'
    with _lock:
        metadata = cache / (key + '.json')
        target = cache / (key + '.wav')
        if metadata.exists():
            record = json.loads(metadata.read_text(encoding='utf-8'))
            if record['gain'] == 1.:
                return source
            if target.is_file():
                return target
        peak = 0.
        with sf.SoundFile(source) as audio:
            for block in audio.blocks(blocksize=262144, dtype='float32', always_2d=True):
                if not np.isfinite(block).all():
                    raise ValueError('试听音频含无效采样')
                peak = max(peak, float(np.max(np.abs(block), initial=0.)))
        gain = gain_for_peak(peak)
        cache.mkdir(exist_ok=True)
        if gain < 1.:
            temporary = cache / (key + '.' + uuid.uuid4().hex + '.wav')
            try:
                with sf.SoundFile(source) as audio, sf.SoundFile(temporary, 'w', samplerate=audio.samplerate,
                                                                 channels=audio.channels, subtype='FLOAT') as output:
                    for block in audio.blocks(blocksize=262144, dtype='float32', always_2d=True):
                        output.write(block * gain)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        metadata.write_text(json.dumps({'version': VERSION, 'gain': gain, 'source_peak': peak}), encoding='utf-8')
        return target if gain < 1. else source
