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
    charts, rejected, diagnostics = {}, dict(result.get('errors',{})), {}
    for key, path in result['charts'].items():
        detail = {}
        try:
            charts[key] = read_osu(path, discard_invalid_lanes=True, diagnostics=detail)
            from .v32_rhythm import read_rhythm, attach_notes
            rhythm, missing = read_rhythm(Path(path).parent / 'model-events.json')
            attach_notes(charts[key][0], rhythm, missing)
        except (ValueError, IndexError, OverflowError) as exc:
            rejected[key] = str(exc)
        diagnostics[key] = detail
    result['chart_diagnostics'] = diagnostics
    result['rejected_charts'] = rejected
    if not charts:
        raise ValueError('V32 所有输出均未通过结构检查：' + json.dumps(rejected, ensure_ascii=False))
    return charts, result


def serialize_with_timing(notes, title, artist, version, timing, meter=None, phase_ms=None):
    # Convert absolute milliseconds through every red timing point, including holds.
    active = next((bpm for timestamp, bpm in reversed(timing) if timestamp <= 0), timing[0][1])
    origin=0.
    if phase_ms is not None:
        bar_ms=60000/active*(meter or 4)
        origin=phase_ms-math.ceil(phase_ms/bar_ms)*bar_ms
    points = [(origin, active)] + [(t, bpm) for t, bpm in timing if t > origin]
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
    chart['time'] = [{'beat': coordinate(t), 'bpm': bpm, **({'meter':meter} if meter else {})} for t, bpm in points]
    music=next(event for event in chart['note'] if event.get('type')==1)
    music['offset']=-origin
    if meter:chart['meta']['mode_ext']['bar_begin']=0
    for event, note in zip(chart['note'], notes):
        event['beat'] = coordinate(note.start)
        if note.end is not None:
            event['endbeat'] = coordinate(note.end)
    return chart


def serialize_fixed_scroll(notes,title,artist,version):
    """Advanced delivery clock, independent of musical and model BPM maps."""
    chart=serialize(notes,title,artist,version,120.)
    chart['meta']['creator']='Malody Studio / Mapperatorinator V32 (AI)'
    chart['effect']=[]
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
    from .v32_batch_streams import clamp_streams
    request['parallel_streams'] = clamp_streams(options.get('parallel_streams', 0))
    # A caller-supplied boolean is insufficient: validate the frozen direct policy.
    if options.get('direct_v32_policy') is not None and options.get('_advanced_presets'):
        from .nps_star_calibration import load_mapping
        load_mapping(options['direct_v32_policy'])
        request['direct_inference'] = True
        request['direct_v32_policy'] = options['direct_v32_policy']
    for field in ('start_time', 'end_time', 'timing_reference', 'timing_fallback_reference',
                  'timing_fallback_identity',
                  'experimental_parameter_snapshot'):
        if field in options: request[field] = options[field]
    if options.get('_advanced_presets'):
        from .v32_event_serialization import FRAGMENT_POLICY
        from .v32_grammar_mask import GRAMMAR_POLICY
        request['presets'] = options['_advanced_presets']
        request['capture_native_trace'] = True
        request['native_event_policy'] = FRAGMENT_POLICY
        request['grammar_policy'] = GRAMMAR_POLICY
    if request.get('experimental_parameter_snapshot') is True:
        # Reference files may otherwise import AR/hitsounds/style metadata.
        # Freeze the no-reference defaults for the single-factor timing study.
        request['experimental_conditioning']={
            'gamemode':3,'keycount':4,'difficulty':request['presets'][0]['sr'],
            'beatmap_id':None,'mapper_id':None,'descriptors':request['descriptors'] or None,
            'hitsounded':True,'hp_drain_rate':5,'circle_size':4,'overall_difficulty':8,
            'approach_rate':9,'slider_multiplier':1.4,'slider_tick_rate':1,
            'hold_note_ratio':request['ln_ratio'],'scroll_speed_ratio':0.,
            'title':request['title'],'title_unicode':request['title'],
            'artist':request['artist'],'artist_unicode':request['artist'],
            'creator':'Malody Studio / Mapperatorinator V32 (AI)','source':'',
            'background':None,'preview_time':-1}
    return request

def generate(source, directory, options, progress):
    from .workflow_log import stage, current_context, file_identity, event
    with stage('v32.prepare_request', source=file_identity(source), destination=directory,
               resolved_difficulties=options.get('difficulties'), presets=options.get('_advanced_presets'),
               engine='Mapperatorinator V32 mania'):
        if not ready():
            raise RuntimeError('V32 模型或独立生成环境尚未完成部署')
        destination = Path(directory) / 'v32-original'
        destination.mkdir()
        keys = [key for key in PRESETS if key in options['difficulties']]
        request = build_worker_request(source, destination, options)
        trace_context=current_context()
        if trace_context:request['workflow_trace']=trace_context
        request_path = destination / 'request.json'
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
        event('artifact_written','v32.request',identity=file_identity(request_path,hash_file=True),
              request=request)
    from .resident import call
    with stage('v32.cuda_inference_rpc', request_path=request_path, model_path=str(MODEL_ROOT),
               request_count=len(request.get('presets',[])), conditions=[{'key':row.get('key'),'sr':row.get('sr'),
                   'seed':row.get('seed'),'core':row.get('core'),'context':[row.get('start_time'),row.get('end_time')]}
                   for row in request.get('presets',[])], resident_disabled=os.environ.get('STARTRAIL_RESIDENT_DISABLED')=='1'):
        if os.environ.get('STARTRAIL_RESIDENT_DISABLED')=='1':
            env=os.environ.copy();env.update(PYTHONUTF8='1',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
            with (destination/'standalone.log').open('w',encoding='utf-8') as log:
                subprocess.run([str(PYTHON),'-u',str(ROOT/'tools/mapperatorinator_worker.py'),str(request_path)],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),timeout=3600,check=True)
            result=json.loads((destination/'worker-result.json').read_text(encoding='utf-8'))
        else:
            result=call('v32',{'request_path':str(request_path.resolve())},progress)
        event('model_result','v32.cuda_inference_rpc',result={key:result.get(key) for key in
              ('device','resident','cost','generation_seconds','peak_allocated_vram_mb','errors','local_retries',
               'actual_conditions','parallel_inference','requested_batch_size','effective_batch_size','batch_fallbacks',
               'sequential_fallback','reference_conditioning_policy','decode_batching')})
    if options.get('_advanced_presets'):
        with stage('v32.read_and_validate_osu', charts=list(result.get('charts',{})),
                   errors=result.get('errors'), output_dir=destination):
            charts=read_worker_charts(result)
        event('chart_conversion_result','v32.read_and_validate_osu',chart_diagnostics=result.get('chart_diagnostics'),
              rejected_charts=result.get('rejected_charts'))
        return charts
    if 'master' not in result.get('charts',{}):
        raise ValueError(result.get('errors',{}).get('master','模型未返回有效母谱，请检查节拍参考'))
    with stage('v32.read_and_validate_osu', chart=result['charts']['master']):
        master = read_osu(result['charts']['master'], discard_invalid_lanes=True, diagnostics=result)
    result['difficulty_policy'] = '共享母谱，音频起音辅助分层；非独立模型难度输出'
    return {key: master for key in keys}, result
