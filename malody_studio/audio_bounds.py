"""Exact-sample silence policy shared by generation and assembly."""
import copy
import json
import hashlib
import uuid
from pathlib import Path
import numpy as np
import soundfile as sf
from functools import lru_cache

SR = 44100


def detect_tail(source):
    with sf.SoundFile(source) as stream:
        if stream.samplerate != SR: raise ValueError('裁尾检测需要 44100 Hz 原曲 PCM')
        frames = len(stream)
        stop = frames
        cutoff = 0
        while stop:
            start = max(0, stop - SR * 8)
            stream.seek(start)
            data = stream.read(stop-start, dtype='float32', always_2d=True)
            if not np.isfinite(data).all():
                raise ValueError('原曲 PCM 含无效采样')
            active = np.flatnonzero(np.any(data != 0, axis=1))
            if len(active):
                cutoff = start + int(active[-1]) + 1
                break
            stop = start
    return {'cutoff_sample': cutoff if frames-cutoff >= SR else frames,
            'samples': frames, 'rule': 'exact-zero-tail-v1', 'enabled': True}


def policy(directory, project):
    path = Path(directory) / 'tail-analysis.json'
    source = Path(directory) / 'source.wav'
    if not source.is_file():
        # Historical metadata remains readable; generation validates its source separately.
        return {'enabled': False, 'samples': project.get('samples', 0),
                'cutoff_sample': project.get('samples', 0), 'unavailable': True}
    stamp = [source.stat().st_size, source.stat().st_mtime_ns]
    try:
        saved = json.loads(path.read_text(encoding='utf-8'))
        if saved.get('source_stamp') != stamp or saved.get('source_sha256') != project.get('source_sha256'):
            raise ValueError('source changed')
    except (OSError, ValueError, KeyError):
        expected = project.get('source_sha256')
        if expected:
            with source.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                    raise ValueError('原曲 PCM 校验失败，请恢复原音源后重试')
        detected = detect_tail(source)
        if project.get('samples') is not None and detected['samples'] != project['samples']:
            raise ValueError('原曲 PCM 采样数与项目不匹配')
        saved = {**detected, 'source_stamp': stamp, 'source_sha256': expected}
        temporary = path.with_name('tail-analysis.'+uuid.uuid4().hex+'.tmp')
        temporary.write_text(json.dumps(saved), encoding='utf-8')
        temporary.replace(path)
    return {**saved, 'enabled': project.get('tail_trim', {}).get('enabled', True)}


def content_end(project):
    trim = project.get('tail_trim', {})
    samples = project.get('samples', trim.get('samples'))
    if samples is None: raise ValueError('音源快照缺少完整采样数')
    return min(samples, trim.get('cutoff_sample', samples)) if trim.get('enabled', True) else samples


def effective_segment(project, segment):
    row = copy.deepcopy(segment)
    row['original_range'] = [segment['start_sample'], segment['end_sample']]
    row['end_sample'] = min(segment['end_sample'], content_end(project))
    return row if row['end_sample'] > row['start_sample'] else None


def effective_segments(project, included_only=True):
    return [row for segment in project['segments'] if (not included_only or segment['included'])
            and (row := effective_segment(project, segment))]


def exact_silence(source, start, end):
    if end <= start: return False
    with sf.SoundFile(source) as stream:
        stream.seek(start)
        for offset in range(start, end, SR*8):
            data = stream.read(min(SR*8, end-offset), dtype='float32', always_2d=True)
            if not len(data) or not np.isfinite(data).all() or np.any(data != 0): return False
    return True


@lru_cache(maxsize=2048)
def _cached_silence(path,stamp,start,end):return exact_silence(path,start,end)


def silent_segment_ids(directory,project):
    path=Path(directory)/'source.wav';stat=path.stat()
    return [s['id'] for s in effective_segments(project,False) if _cached_silence(str(path.resolve()),(stat.st_size,stat.st_mtime_ns),s['start_sample'],s['end_sample'])]
