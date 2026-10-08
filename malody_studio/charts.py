"""Classic Malody serializer and independent playability checks.

All internal timestamps refer to the packaged audio, in milliseconds.
The BGM event is at beat zero and stores negative beat-zero audio time.
"""
from collections import Counter, deque
import copy
from dataclasses import dataclass
from fractions import Fraction
import json
import math
from pathlib import Path
import time
import zipfile

@dataclass(frozen=True)
class Note:
    start: float
    lane: int
    end: float | None = None

class ChartStructureError(ValueError):
    def __init__(self, message, start_ms=None, end_ms=None, lane=None):
        super().__init__(message)
        self.start_ms=start_ms
        self.end_ms=end_ms
        self.lane=lane

def beat_value(value):
    if len(value) != 3 or not all(isinstance(x, int) and not isinstance(x, bool) for x in value):
        raise ValueError('拍坐标必须包含三个整数')
    a, b, c = value
    if a < 0 or b < 0 or c <= 0 or b >= c:
        raise ValueError('拍坐标必须使用非负整数拍和有效分数')
    return Fraction(a) + Fraction(b, c)

def to_beat(milliseconds, bpm, origin=0):
    value = Fraction(str((milliseconds - origin) * bpm / 60000)).limit_denominator(1920)
    if value < 0:
        raise ValueError('音符早于拍零时间')
    integer = value.numerator // value.denominator
    remainder = value - integer
    return [integer, remainder.numerator, remainder.denominator]

def parse_osu_objects(lines):
    notes = []
    for line in lines:
        fields = line.split(',')
        lane = min(3, int(float(fields[0])) // 128)
        start = float(fields[2])
        end = float(fields[5].split(':')[0]) if int(fields[3]) & 128 else None
        notes.append(Note(start, lane, end))
    return sorted(notes, key=lambda n: (n.start, n.lane))

def clean_notes(notes, duration_ms, preset):
    """Remove malformed notes, occupied-lane taps and unreasonable bursts.

    These are explicit phone playability limits, not official Malody levels.
    Absolute model timestamps are preserved.
    """
    output = []
    occupied = [-1e9] * 4
    last = [-1e9] * 4
    starts = deque()
    chords = Counter()
    for n in sorted(notes, key=lambda x: (x.start, x.lane)):
        if n.lane not in range(4) or not math.isfinite(n.start) or not 0 <= n.start < duration_ms:
            continue
        if n.start - last[n.lane] < preset['gap'] or n.start < occupied[n.lane] + 35:
            continue
        timestamp = round(n.start)
        if chords[timestamp] >= preset['chord']:
            continue
        while starts and starts[0] <= n.start - 1000:
            starts.popleft()
        if len(starts) >= preset['peak']:
            continue
        end = n.end
        if end is not None:
            if not math.isfinite(end) or end <= n.start:
                continue
            end = min(end, duration_ms)
            if end - n.start < 100:
                end = None
        output.append(Note(n.start, n.lane, end))
        last[n.lane] = n.start
        occupied[n.lane] = end if end is not None else n.start
        starts.append(n.start)
        chords[timestamp] += 1
    if len(output) < preset.get('min_notes', 8):
        raise ValueError('有效音符太少，请检查音频或调整难度后重试')
    return output

def chart_stats(notes, duration):
    timestamps = [n.start for n in notes]
    starts = deque()
    peak = 0
    bins = Counter(int(t / 1000) for t in timestamps)
    for t in timestamps:
        while starts and starts[0] <= t - 1000:
            starts.popleft()
        starts.append(t)
        peak = max(peak, len(starts))
    lane_count = [sum(n.lane == i for n in notes) for i in range(4)]
    holds = sum(n.end is not None for n in notes)
    active = max(1, (max(n.end or n.start for n in notes) - notes[0].start) / 1000)
    return {'notes': len(notes), 'holds': holds, 'ln_ratio': round(holds / len(notes), 3),
            'average_nps': round(len(notes) / active, 2), 'peak_nps': peak,
            'lanes': lane_count, 'density': [bins[i] for i in range(math.ceil(duration))]}

def serialize(notes, title, artist, version, bpm, origin=0):
    # Represent early model notes without negative beat triples, preserving audio alignment.
    origin = min(origin, min(n.start for n in notes))
    events = []
    for n in notes:
        event = {'beat': to_beat(n.start, bpm, origin), 'column': n.lane}
        if n.end is not None:
            event['endbeat'] = to_beat(n.end, bpm, origin)
        events.append(event)
    events.append({'beat': [0, 0, 1], 'sound': 'audio.ogg', 'vol': 100,
                   'offset': -round(origin), 'type': 1})
    return {'meta': {'$ver': 0, 'creator': 'Malody Studio / MuG Diffusion v1.0.0',
                     'background': '', 'version': version, 'id': 0, 'mode': 0,
                     'time': int(time.time()), 'song': {'title': title, 'artist': artist, 'id': 0},
                     'mode_ext': {'column': 4, 'bar_begin': 0}},
            'time': [{'beat': [0, 0, 1], 'bpm': float(bpm)}], 'effect': [], 'note': events,
            'extra': {version: {'divide': 4, 'speed': 100, 'save': 0, 'lock': 0, 'edit_mode': 0}}}

def validate_chart(chart, audio_duration_ms):
    if chart['meta']['mode'] != 0 or chart['meta']['mode_ext']['column'] != 4:
        raise ValueError('必须是 Key 模式 4K 谱面')
    timings = chart['time']
    if not timings or beat_value(timings[0]['beat']) != 0:
        raise ValueError('必须从拍零定义 BPM')
    previous_beat = -1
    for point in timings:
        b = float(beat_value(point['beat']))
        if b <= previous_beat or not math.isfinite(point['bpm']) or point['bpm'] <= 0:
            raise ValueError('BPM 事件必须有序且为正数')
        previous_beat = b
    music = [n for n in chart['note'] if n.get('type') == 1 and 'sound' in n]
    if len(music) != 1 or music[0]['sound'] != 'audio.ogg' or beat_value(music[0]['beat']) != 0:
        raise ValueError('背景音乐引用错误')
    origin = -music[0]['offset']
    def milliseconds(beat):
        target = float(beat_value(beat))
        total = origin
        for i, point in enumerate(timings):
            begin = float(beat_value(point['beat']))
            finish = float(beat_value(timings[i + 1]['beat'])) if i + 1 < len(timings) else target
            if target <= begin:
                break
            total += (min(target, finish) - begin) * 60000 / point['bpm']
            if target <= finish:
                break
        return total
    last_end = [-1e9] * 4
    last_start = [-1e9] * 4
    total = 0
    previous_time = -1e9
    for event in chart['note']:
        if 'column' not in event:
            continue
        lane = event['column']
        if not isinstance(lane, int) or isinstance(lane, bool) or lane not in range(4):
            raise ValueError('轨道必须是 0–3')
        start = milliseconds(event['beat'])
        end = milliseconds(event['endbeat']) if 'endbeat' in event else start
        if start < previous_time - 0.6 or start < -0.6 or end > audio_duration_ms + 1:
            raise ValueError('音符排序或音频时间范围错误')
        if 'endbeat' in event and end <= start:
            raise ValueError('长条时长必须为正')
        if start <= last_start[lane] + 0.1 or start < last_end[lane] - 0.1:
            raise ChartStructureError(f'第 {lane + 1} 轨在 {start:.0f} ms 附近存在重复音符或长条冲突',
                                     start_ms=round(start),end_ms=round(end),lane=lane)
        previous_time = start
        last_start[lane] = start
        last_end[lane] = end
        total += 1
    if not total:
        raise ValueError('谱面没有可击打音符')
    return {'valid': True, 'notes': total, 'audio': 'OGG Vorbis', 'mode': '4K'}

def _compact_timing_points(chart):
    """Drop repeated timing markers that do not change BPM or other timing data."""
    packed = copy.deepcopy(chart)
    compact = []
    for point in packed.get('time', []):
        if compact:
            previous = compact[-1]
            current_fields = {key: value for key, value in point.items() if key != 'beat'}
            previous_fields = {key: value for key, value in previous.items() if key != 'beat'}
            if current_fields == previous_fields:
                continue
        compact.append(point)
    packed['time'] = compact
    return packed


def package(directory, charts, audio_path, report, background=None, filenames=None):
    directory = Path(directory)
    song = directory / '0'
    song.mkdir(exist_ok=True)
    import shutil
    if Path(audio_path).resolve() != (song / 'audio.ogg').resolve():
        shutil.copyfile(audio_path, song / 'audio.ogg')
    if background:
        if Path(background).resolve() != (song / 'background.jpg').resolve():
            shutil.copyfile(background, song / 'background.jpg')
    for chart in charts.values():
        if chart['meta'].get('background') and (not background or chart['meta']['background'] != 'background.jpg'):
            raise ValueError('背景图片引用缺失或无效')
    filenames = filenames or {}
    names = {}
    for key, chart in charts.items():
        packed_chart = _compact_timing_points(chart)
        filename = filenames.get(key, f'{key}.mc')
        if Path(filename).name != filename or not filename.lower().endswith('.mc'):
            raise ValueError('谱面文件名无效')
        names[key] = filename
        (song / filename).write_text(json.dumps(packed_chart, ensure_ascii=False, indent=2), encoding='utf-8')
    (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    keep = {'audio.ogg', 'background.jpg', *names.values()}
    for path in song.iterdir():
        if path.is_file() and path.suffix.lower() == '.mc' and path.name not in keep:
            path.unlink()
    archive = directory / 'malody-4k.mcz'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        # No '0/' directory entry: mobile Malody rejects archives that carry one.
        for path in sorted(song.iterdir()):
            if path.name == 'generation.txt':
                continue
            z.write(path, f'0/{path.name}')
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:
            raise ValueError('曲包压缩校验失败')
        for key in charts:
            decoded = json.loads(z.read(f'0/{names[key]}'))
            validate_chart(decoded, report['duration'] * 1000)
    return archive
