"""Stable, filesystem-safe names for independently selectable chart variants."""
import re
import unicodedata
from .difficulty import PATTERN_LABELS, PRESETS

MODEL_SLUGS = {'v32': 'Mapperatorinator-V32', 'mug': 'MuG-Diffusion'}
DEFAULT_CHART_CREATOR = 'Startrail'


def validate_creator(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 120:
        raise ValueError('谱师名字须填写，最多 120 字')
    if any(ord(char) < 32 for char in value):
        raise ValueError('谱师名字不能包含控制字符')
    return value.strip()


def chart_id(pattern, difficulty):
    return f'{pattern}--{difficulty}'


def safe_component(value, limit=120):
    value = unicodedata.normalize('NFC', str(value or '')).strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value)
    value = re.sub(r'\s+', ' ', value).rstrip(' .') or 'untitled'
    if value.upper().split('.')[0] in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        value = '_' + value
    return value[:limit].rstrip(' .') or 'untitled'


def chart_stem(title, engine, pattern, difficulty):
    pattern_label = PATTERN_LABELS.get(pattern, safe_component(pattern, 24))
    difficulty_label = PRESETS.get(difficulty, {}).get('label', safe_component(difficulty, 24))
    suffix = f'_{pattern_label}_{difficulty_label}'
    return f'{safe_component(title, max(12, 120 - len(suffix)))}{suffix}'


def model_display(engine):
    return 'Mapperatorinator V32' if engine == 'v32' else 'MuG Diffusion'


def archive_stem(title, engine, patterns, difficulties):
    patterns = [key for key in PATTERN_LABELS if key in patterns]
    difficulties = [key for key in PRESETS if key in difficulties]
    if len(patterns) == len(difficulties) == 1:
        return chart_stem(title, engine, patterns[0], difficulties[0])
    pattern_part = '+'.join(PATTERN_LABELS.get(key, key) for key in patterns)
    difficulty_part = '+'.join(PRESETS[key]['label'] for key in difficulties)
    suffix=f'_{pattern_part}_{difficulty_part}'
    stem = f'{safe_component(title, max(12,180-len(suffix)))}{suffix}'
    return safe_component(stem, 180)


def chart_label(pattern, difficulty):
    return f"{PATTERN_LABELS[pattern]} {PRESETS[difficulty]['label']}"
