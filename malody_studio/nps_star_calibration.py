"""Frozen production calibration and BPM-bucket targets for independent V32."""
import copy
import hashlib
import json
import math
import re
from pathlib import Path
from .paths import ROOT, PRESETS
from util.nps_star_mapping.mapping import forward, inverse

VERSION = 'nps-star-direct-v1'
ASSETS = ROOT / 'data/calibrations/nps-star-mapping'


def load_mapping(policy):
    if not isinstance(policy, dict) or policy.get('version') != VERSION:
        raise ValueError('直接生成策略版本无效')
    identity = policy.get('mapping_id', '')
    if not isinstance(identity, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', identity):
        raise ValueError('NPS 星级曲线身份无效')
    data = (ASSETS / identity / 'mapping.json').read_bytes()
    if hashlib.sha256(data).hexdigest() != policy.get('mapping_sha256'):
        raise ValueError('冻结 NPS 星级曲线哈希不匹配')
    return json.loads(data)


def freeze_policy():
    active = json.loads((ASSETS / 'active.json').read_text(encoding='utf-8'))
    from .music_timing import selected_policy
    tokenizer = json.loads((ROOT/'models/mapperatorinator/v32-mania/tokenizer.json').read_text(encoding='utf-8'))
    result = dict(version=VERSION, mapping_id=active['mapping_id'],
                  mapping_sha256=active['mapping_sha256'], beat_analysis_policy=selected_policy(),
                  generation='independent_difficulty_and_condition_spans',
                  selection='no_density_thinning', measurement='chart_span_nps',
                  model_timing_reference='none_beat_this_is_for_buckets_only')
    # Frozen at submit: a policy saved without this key belongs to the v2 estimator.
    from .bpm_buckets import VERSION as bucket_version
    result['bpm_bucket_version'] = bucket_version
    result['tokenizer_conditioning'] = {k:tokenizer[k] for k in ('num_diff_classes','max_difficulty')}
    load_mapping(result)
    return result


def normalize_ranges(ranges=None, rules=None):
    if ranges is None:
        ranges = {}
    if not isinstance(ranges, dict) or set(ranges) - set(PRESETS):
        raise ValueError('NPS 范围包含未知难度')
    result = {}
    for key, preset in PRESETS.items():
        rate = float((rules or {}).get(key, {}).get('rate', preset['rate']))
        row = ranges.get(key, {'min':round(rate*.8, 5), 'max':round(rate*1.2, 5)})
        if not isinstance(row, dict) or set(row) != {'min', 'max'}:
            raise ValueError('每档 NPS 范围须包含 min 和 max')
        for value in row.values():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not .5 <= value <= 50:
                raise ValueError('NPS 上下限须为 0.5–50 的有限数值')
        if row['min'] > row['max']:
            raise ValueError('NPS 下限不能大于上限')
        result[key] = {name:float(value) for name,value in row.items()}
    return result


def resolve_condition(policy, nps_range, offset=0., dynamic=True):
    """Choose an actual tokenizer class, rather than flooring a continuous SR."""
    model = load_mapping(policy)
    lower, upper = nps_range['min'], nps_range['max']
    position = min(1., max(0., (float(offset)+1)/2)) if dynamic else .5
    target = lower + (upper-lower)*position
    tokenizer = policy.get('tokenizer_conditioning',{'num_diff_classes':24,'max_difficulty':12})
    step = tokenizer['max_difficulty']/tokenizer['num_diff_classes']
    candidates = [dict(sr=i*step, fitted_nps=forward(model, i*step))
                  for i in range(tokenizer['num_diff_classes']) if 1 <= i*step <= 10]
    admissible = [c for c in candidates if lower <= c['fitted_nps'] <= upper]
    choice = min(admissible or candidates, key=lambda c:(abs(c['fitted_nps']-target), c['sr']))
    mapped = inverse(model, target)
    return dict(nps_range=copy.deepcopy(nps_range), target_nps=target,
                continuous_sr=mapped['sr'], sr=choice['sr'],
                difficulty_class=int(choice['sr']/step), fitted_nps=choice['fitted_nps'],
                quantization_nps_error=choice['fitted_nps']-target,
                no_class_within_range=not bool(admissible),
                calibration_clamped=mapped['clamped'], extrapolated=choice['sr']>model.get('extrapolation',{}).get('anchor_sr',10),
                bucket_offset=float(offset), bucket_position=position,
                mapping_id=policy['mapping_id'], mapping_sha256=policy['mapping_sha256'])


def chart_span_density(notes, bounds):
    count = len(notes)
    if count:
        first = min(n.start for n in notes)
        last = max(n.end if n.end is not None else n.start for n in notes)
        duration = max(0., (last-first)/1000)
    else:
        first = last = None
        duration = 0.
    nps = count/duration if duration > 0 else 0.
    lower, upper = bounds['min'], bounds['max']
    return dict(status='in_range' if count >= 2 and duration > 0 and lower <= nps <= upper else 'out_of_range',
                denominator='first_head_to_last_head_or_tail', measured_nps=nps,
                note_count=count, first_head_ms=first, last_event_ms=last,
                span_seconds=duration, target_range=copy.deepcopy(bounds),
                retry_allowed=False, density_thinning=False)
