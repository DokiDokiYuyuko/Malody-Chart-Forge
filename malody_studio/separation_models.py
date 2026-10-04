"""Shared, JSON-compatible separation model and parameter definitions."""
import copy
import math

DEMUC_VERSION = 'demucs-4.0.1-adapter-v1'
ROFORMER_VERSION = 'melband-roformer-kim-adapter-v1'
ROFORMER_MODEL = 'melband_roformer_kim'
DEMUC_DEFAULTS = {'model': 'htdemucs', 'segment': 7.8, 'overlap': .25, 'shifts': 1, 'seed': 20261003}
DEMUC_LIMITS = {
    'segment': (1, 7.8, '处理窗口须为 1–7.8 秒。Demucs 标准和 FT 的单次窗口上限为 7.8 秒，完整歌曲仍会全部处理。'),
    'overlap': (.1, .75, '重叠比例须为 0.1–0.75（10%–75%）。'),
    'shifts': (0, 4, '随机平移次数须为 0–4 的整数。'),
    'seed': (0, 2147483640, '分离种子须为 0–2147483640 的整数。'),
}
ROFORMER_DEFAULTS = {'model': ROFORMER_MODEL, 'segment': 8, 'overlap_count': 4,
                     'seed': 20261003, 'batch_size': 1, 'amp': True, 'tta': False}


def _parameter(key, label, default, minimum, maximum, step, kind, help_text, **extra):
    return dict(key=key, label=label, default=default, min=minimum, max=maximum,
                step=step, type=kind, help=help_text, **extra)


def catalog():
    demucs_parameters = [
        _parameter(key, title, DEMUC_DEFAULTS[key], *DEMUC_LIMITS[key][:2], step, kind, DEMUC_LIMITS[key][2])
        for key, title, step, kind in (
            ('segment', '处理窗口（秒）', .1, 'number'), ('overlap', '重叠比例', .05, 'number'),
            ('shifts', '随机平移次数', 1, 'integer'), ('seed', '分离种子', 1, 'integer'))
    ]
    result = {}
    for model, label, quality, description in (
        ('htdemucs', 'Demucs 标准', 'standard', '已验证的快速分离。'),
        ('htdemucs_ft', 'Demucs FT', 'quality', '已验证的精调分离；通常耗时更长。')):
        result[model] = dict(id=model, label=label, quality=quality, description=description,
                             defaults={**DEMUC_DEFAULTS, 'model': model}, parameters=copy.deepcopy(demucs_parameters))
    result[ROFORMER_MODEL] = dict(
        id=ROFORMER_MODEL, label='Kim MelBand RoFormer', quality='experimental',
        description='高质量人声分离；伴奏由原曲减去预测人声得到。可先试听，再决定是否用于生成。',
        defaults=dict(ROFORMER_DEFAULTS), parameters=[
            _parameter('segment', '处理窗口（秒）', 8, 8, 8, 1, 'integer', '匹配训练配置的固定 8 秒窗口。', read_only=True),
            _parameter('overlap_count', '窗口覆盖次数', 4, 2, 8, 2, 'integer', '2、4 或 8 次覆盖；4 次的步长为 2 秒。', choices=[2, 4, 8]),
            _parameter('seed', '分离种子', 20261003, 0, 2147483640, 1, 'integer', DEMUC_LIMITS['seed'][2]),
            _parameter('batch_size', '批量大小', 1, 1, 1, 1, 'integer', '当前验证路径固定为单窗口。', read_only=True),
            _parameter('amp', '混合精度', True, None, None, None, 'boolean', '使用 CUDA 混合精度。', read_only=True),
            _parameter('tta', '增强推理', False, None, None, None, 'boolean', '当前验证路径关闭增强推理。', read_only=True),
        ])
    return result


def adapter_version(model):
    if model in ('htdemucs', 'htdemucs_ft'):
        return DEMUC_VERSION
    if model == ROFORMER_MODEL:
        return ROFORMER_VERSION
    raise ValueError('未知分离模型：' + str(model))


def validated_settings(value=None):
    value = value or {}
    if not isinstance(value, dict):
        raise ValueError('分离配置须为对象')
    model = value.get('model', DEMUC_DEFAULTS['model'])
    adapter_version(model)
    if model != ROFORMER_MODEL:
        result = {**DEMUC_DEFAULTS, **value}
        for key, (lo, hi, message) in DEMUC_LIMITS.items():
            number = result[key]
            if isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number) or not lo <= number <= hi:
                raise ValueError(message)
            if key in ('shifts', 'seed') and int(number) != number:
                raise ValueError(message)
        # Retain the legacy five keys and their numeric representation exactly.
        return {key: result[key] for key in DEMUC_DEFAULTS}
    unsupported = set(value) - set(ROFORMER_DEFAULTS)
    if unsupported:
        raise ValueError('RoFormer 不支持这些参数：' + '、'.join(sorted(unsupported)))
    result = {**ROFORMER_DEFAULTS, **value}
    for key, choices in (('segment', (8,)), ('overlap_count', (2, 4, 8)), ('batch_size', (1,))):
        number = result[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number not in choices:
            raise ValueError('RoFormer ' + key + ' 须为 ' + '、'.join(map(str, choices)))
        result[key] = int(number)
    seed = result['seed']
    if isinstance(seed, bool) or not isinstance(seed, (int, float)) or not math.isfinite(seed) or not 0 <= seed <= 2147483640 or int(seed) != seed:
        raise ValueError(DEMUC_LIMITS['seed'][2])
    result['seed'] = int(seed)
    if result['amp'] is not True or result['tta'] is not False:
        raise ValueError('RoFormer 当前路径须开启 AMP 并关闭 TTA')
    return result
