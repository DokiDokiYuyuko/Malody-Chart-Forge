import hashlib
import json

import pytest

from malody_studio import direct_v32, nps_star_calibration
from malody_studio.charts import Note


def frozen_policy():
    active = json.loads((nps_star_calibration.ASSETS / 'active.json').read_text(encoding='utf-8'))
    return {
        'version': nps_star_calibration.VERSION,
        'mapping_id': active['mapping_id'],
        'mapping_sha256': active['mapping_sha256'],
    }


def test_frozen_mapping_is_verified_and_hash_is_carried_into_condition():
    policy = frozen_policy()
    mapping_path = nps_star_calibration.ASSETS / policy['mapping_id'] / 'mapping.json'
    payload = mapping_path.read_bytes()

    assert hashlib.sha256(payload).hexdigest() == policy['mapping_sha256']
    mapping = nps_star_calibration.load_mapping(policy)
    assert mapping['version'] == 'chart-span-nps-sr-low7-extrapolation-v2'

    condition = nps_star_calibration.resolve_condition(policy, {'min': 8.0, 'max': 9.0}, offset=-1)
    assert condition['mapping_id'] == policy['mapping_id']
    assert condition['mapping_sha256'] == policy['mapping_sha256']
    with pytest.raises(ValueError, match='哈希不匹配'):
        nps_star_calibration.load_mapping({**policy, 'mapping_sha256': '0' * 64})


def test_half_star_token_class_inside_nps_range_wins_over_closer_continuous_class():
    policy = frozen_policy()
    nps_range = {'min': 8.0, 'max': 9.0}
    result = nps_star_calibration.resolve_condition(policy, nps_range, offset=-1)

    assert result['continuous_sr'] * 2 < 6  # The inverse alone would select token class 5.
    assert result['sr'] == 3.0
    assert result['difficulty_class'] == 6  # 24 classes across max difficulty 12.
    assert nps_range['min'] <= result['fitted_nps'] <= nps_range['max']
    assert result['no_class_within_range'] is False


def test_requests_are_independent_per_difficulty_and_split_at_bpm_class_changes():
    policy = frozen_policy()
    sample_rate = direct_v32.SR
    plan = {
        'samples': 12 * sample_rate,
        'sections': [
            {'id': 'low-bpm-offset', 'core': [0, 4 * sample_rate], 'bpm_bucket_id': 0, 'bpm_bucket_offset': -1.0},
            {'id': 'high-bpm-offset-a', 'core': [4 * sample_rate, 8 * sample_rate], 'bpm_bucket_id': 1, 'bpm_bucket_offset': 1.0},
            {'id': 'high-bpm-offset-b', 'core': [8 * sample_rate, 12 * sample_rate], 'bpm_bucket_id': 2, 'bpm_bucket_offset': 1.0},
        ],
    }
    variants = [
        {'key': 'balanced--hard', 'pattern': 'balanced', 'difficulty': 'hard'},
        {'key': 'balanced--expert', 'pattern': 'balanced', 'difficulty': 'expert'},
    ]
    settings = {
        'seed': 123,
        'dynamic_enabled': True,
        'difficulty_rules': {},
        'nps_ranges': {'hard': {'min': 8.5, 'max': 10.2}, 'expert': {'min': 10.0, 'max': 16.0}},
    }

    requests = direct_v32.requests_for(plan, variants, settings, policy, [0, 12 * sample_rate])
    by_variant = {variant['key']: [row for row in requests if row['variant'] == variant['key']] for variant in variants}

    assert len(requests) == 4
    assert len({row['key'] for row in requests}) == len(requests)
    for variant in variants:
        rows = by_variant[variant['key']]
        assert len(rows) == 2
        assert rows[0]['core'][0] == 0
        assert rows[0]['core'][1] == rows[1]['core'][0]
        assert rows[1]['core'][1] == 12 * sample_rate
        assert rows[0]['sr'] != rows[1]['sr']
        assert [detail['section_id'] for detail in rows[1]['sections']] == ['high-bpm-offset-a', 'high-bpm-offset-b']
        assert all(detail['sr'] == row['sr'] for row in rows for detail in row['sections'])

    # The same BPM section receives distinct requests/classes for each difficulty.
    section_classes = {}
    for variant in variants:
        for row in by_variant[variant['key']]:
            for detail in row['sections']:
                section_classes.setdefault(detail['section_id'], {})[variant['key']] = row['sr']
    assert section_classes['low-bpm-offset']['balanced--hard'] != section_classes['low-bpm-offset']['balanced--expert']
    assert section_classes['high-bpm-offset-a']['balanced--hard'] != section_classes['high-bpm-offset-a']['balanced--expert']


def test_seven_plus_token_mapping_is_explicitly_marked_as_extrapolated():
    policy = frozen_policy()
    result = nps_star_calibration.resolve_condition(policy, {'min': 20.0, 'max': 21.0}, offset=0.0)

    assert result['sr'] >= 7.0
    assert result['difficulty_class'] >= 14
    assert result['extrapolated'] is True


def test_chart_span_density_counts_long_note_tail_as_last_event():
    notes = [Note(start=100.0, lane=0), Note(start=500.0, lane=1, end=2100.0)]

    result = nps_star_calibration.chart_span_density(notes, {'min': 0.9, 'max': 1.1})

    assert result['status'] == 'in_range'
    assert result['denominator'] == 'first_head_to_last_head_or_tail'
    assert result['note_count'] == 2
    assert result['first_head_ms'] == 100.0
    assert result['last_event_ms'] == 2100.0
    assert result['span_seconds'] == pytest.approx(2.0)
    assert result['measured_nps'] == pytest.approx(1.0)
