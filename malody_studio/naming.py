"""Stable, filesystem-safe names for independently selectable chart variants."""
import re
import unicodedata
from .difficulty import PATTERN_LABELS, PRESETS

MODEL_SLUGS = {'v32': 'Mapperatorinator-V32', 'mug': 'MuG-Diffusion'}


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
    model = MODEL_SLUGS.get(engine, safe_component(engine, 28))
    pattern_label = PATTERN_LABELS.get(pattern, safe_component(pattern, 24))
    difficulty_label = PRESETS.get(difficulty, {}).get('label', safe_component(difficulty, 24))
    suffix = f'_{model}_{pattern_label}_{difficulty_label}'
    return f'{safe_component(title, max(12, 120 - len(suffix)))}{suffix}'


def model_display(engine):
    return 'Mapperatorinator V32' if engine == 'v32' else 'MuG Diffusion'


def archive_stem(title, engine, patterns, difficulties):
    patterns = list(dict.fromkeys(patterns))
    difficulties = list(dict.fromkeys(difficulties))
    if len(patterns) == len(difficulties) == 1:
        return chart_stem(title, engine, patterns[0], difficulties[0])
    pattern_part = '+'.join(PATTERN_LABELS.get(key, key) for key in patterns)
    difficulty_part = '+'.join(PRESETS[key]['label'] for key in difficulties)
    stem = f'{safe_component(title, 72)}_{MODEL_SLUGS.get(engine, engine)}_{pattern_part}_{difficulty_part}'
    if len(stem) > 180:
        stem = f'{safe_component(title, 90)}_{MODEL_SLUGS.get(engine, engine)}_{len(patterns)}Patterns_{len(difficulties)}Difficulties'
    return safe_component(stem, 180)
