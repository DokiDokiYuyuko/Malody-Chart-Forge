"""Isolated V32 process and strict osu!mania-to-classic-Malody conversion."""
import bisect
import json
import hashlib
import math
import os
import re
from pathlib import Path
import subprocess
import time
from .paths import ROOT, PRESETS
from .charts import parse_osu_objects, serialize, to_beat

MODEL_ROOT = ROOT / 'models' / 'mapperatorinator'
PYTHON = ROOT / 'runtime' / 'mapperatorinator-venv' / 'Scripts' / 'python.exe'
ENGINE_NAME = 'Mapperatorinator V32 mania'

def ready():
    return PYTHON.is_file() and (MODEL_ROOT / 'deployment-ready.json').is_file() and all(
        (MODEL_ROOT / folder / 'model.safetensors').is_file() and
        (MODEL_ROOT / folder / 'model.safetensors').stat().st_size == 865900700
        for folder in ('v32-mania', 'v32-timing'))

def read_osu(path, discard_invalid_lanes=False, diagnostics=None):
    sections, current = {}, None
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('//'):
            continue
        if line.startswith('[') and line.endswith(']'):
            current = line[1:-1]
            sections[current] = []
        elif current:
            sections[current].append(line)
    def fields(section):
        return dict(line.split(':', 1) for line in sections.get(section, []) if ':' in line)
    if fields('General').get('Mode', '').strip() != '3' or float(fields('Difficulty').get('CircleSize', 0)) != 4:
        raise ValueError('V32 输出必须是 osu!mania 四键谱面')
    timing, inherited = {}, 0
    for line in sections.get('TimingPoints', []):
        values = line.split(',')
        timestamp, beat_ms = float(values[0]), float(values[1])
        if not math.isfinite(timestamp) or not math.isfinite(beat_ms):
            raise ValueError('V32 输出节拍含无效数值')
        red = len(values) < 7 or values[6].strip() == '1'
        if red:
            if beat_ms <= 0:
                raise ValueError('V32 输出 BPM 必须为正数')
            timing[timestamp] = 60000 / beat_ms
        else:
            inherited += 1
    if not timing:
        raise ValueError('V32 没有生成有效节拍')
    objects = sections.get('HitObjects', [])
    invalid = [line for line in objects if not 0 <= float(line.split(',')[0]) < 512]
    if invalid:
        if not discard_invalid_lanes or len(invalid) > min(8, max(1, int(len(objects) * .01))) or len(invalid) / len(objects) > .01:
            raise ValueError('音符轨道坐标超出范围')
        # Keep the original .osu. Discard rare invalid objects; never map them
        # into lane 4 by clipping and pretend they were valid model notes.
        objects = [line for line in objects if line not in invalid]
    if diagnostics is not None:
        diagnostics['discarded_invalid_lane_notes'] = len(invalid)
    for line in objects:
        values = line.split(',')
        kind = int(values[3])
        if not (kind & 1 or kind & 128) or kind & (2 | 8):
            raise ValueError('四键谱面包含不支持的音符类型')
        if not 0 <= float(values[0]) < 512:
            raise ValueError('音符轨道坐标超出范围')
    return parse_osu_objects(objects), sorted(timing.items()), inherited

def read_worker_charts(result):
    """Isolate malformed model attempts; valid retries and sibling outputs survive."""
    charts, rejected, diagnostics = {}, {}, {}
    for key, path in result['charts'].items():
        detail = {}
        try:
            charts[key] = read_osu(path, discard_invalid_lanes=True, diagnostics=detail)
        except (ValueError, IndexError, OverflowError) as exc:
            rejected[key] = str(exc)
        diagnostics[key] = detail
    result['chart_diagnostics'] = diagnostics
    result['rejected_charts'] = rejected
    if not charts:
        raise ValueError('V32 所有输出均未通过结构检查：' + json.dumps(rejected, ensure_ascii=False))
    return charts, result


def serialize_with_timing(notes, title, artist, version, timing):
    # Convert absolute milliseconds through every red timing point, including holds.
    active = next((bpm for timestamp, bpm in reversed(timing) if timestamp <= 0), timing[0][1])
    points = [(0.0, active)] + [(t, bpm) for t, bpm in timing if t > 0]
    times = [point[0] for point in points]
    beats = [0.0]
    for index in range(1, len(points)):
        beats.append(beats[-1] + (times[index] - times[index - 1]) * points[index - 1][1] / 60000)
    def coordinate(milliseconds):
        index = max(0, bisect.bisect_right(times, milliseconds) - 1)
        value = beats[index] + (milliseconds - times[index]) * points[index][1] / 60000
        return to_beat(value * 500, 120)
    chart = serialize(notes, title, artist, version, points[0][1])
    chart['meta']['creator'] = 'Malody Studio / Mapperatorinator V32 (AI)'
    chart['time'] = [{'beat': coordinate(t), 'bpm': bpm} for t, bpm in points]
    for event, note in zip(chart['note'], notes):
        event['beat'] = coordinate(note.start)
        if note.end is not None:
            event['endbeat'] = coordinate(note.end)
    return chart

def build_worker_request(source, destination, options):
    difficulty = options.get('v32_difficulty', 8)
    request = {'audio': str(source), 'output': str(destination), 'title': options['title'],
            'artist': options['artist'], 'seed': options['seed'], 'ln_ratio': options['ln_ratio'],
            'difficulty': difficulty, 'temperature': options.get('v32_temperature', .9),
            'top_p': options.get('v32_top_p', .9),
            'mania_column_temperature': options.get('v32_column_temperature', .8),
            'cfg_scale': options.get('v32_cfg_scale', 1), 'year': options.get('v32_year', 2024),
            'descriptors': options.get('v32_descriptors', []),
            'negative_descriptors': options.get('v32_negative_descriptors', []),
            'presets': [dict(label='共享母谱', sr=difficulty, key='master')]}
    for field in ('start_time', 'end_time', 'timing_reference'):
        if field in options: request[field] = options[field]
    if options.get('_advanced_presets'): request['presets'] = options['_advanced_presets']
    return request

def generate(source, directory, options, progress):
    if not ready():
        raise RuntimeError('V32 模型或独立生成环境尚未完成部署')
    destination = Path(directory) / 'v32-original'
    destination.mkdir()
    keys = [key for key in PRESETS if key in options['difficulties']]
    request = build_worker_request(source, destination, options)
    request_path = destination / 'request.json'
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
    from .resident import call
    if os.environ.get('STARTRAIL_RESIDENT_DISABLED')=='1':
        env=os.environ.copy();env.update(PYTHONUTF8='1',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
        with (destination/'standalone.log').open('w',encoding='utf-8') as log:
            subprocess.run([str(PYTHON),'-u',str(ROOT/'tools/mapperatorinator_worker.py'),str(request_path)],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),timeout=3600,check=True)
        result=json.loads((destination/'worker-result.json').read_text(encoding='utf-8'))
    else:
        result=call('v32',{'request_path':str(request_path.resolve())},progress)
    if options.get('_advanced_presets'):
        return read_worker_charts(result)
    master = read_osu(result['charts']['master'], discard_invalid_lanes=True, diagnostics=result)
    result['difficulty_policy'] = '共享母谱，音频起音辅助分层；非独立模型难度输出'
    return {key: master for key in keys}, result
