import json

import pytest
from fastapi.testclient import TestClient

from malody_studio import server
from malody_studio.difficulty import calibrate
from malody_studio.charts import Note, chart_stats
from malody_studio.engine import mug_condition_features
from malody_studio.mapperatorinator import build_worker_request


def test_settings_validates_chart_rules_and_model_controls():
    options = server.settings(
        't', 'a', '["hard"]', .25, 100, 42, 180, 'v32',
        difficulty_rules='{"hard":{"rate":11,"chord":4,"gap":45,"peak":24,"hold_ms":800}}',
        pattern='jumpstream', pattern_strength=28, mug_difficulty=6.2,
        mug_style='loved', mug_guidance=3.5, mug_eta=.2,
        v32_difficulty=7.5, v32_temperature=1.1, v32_top_p=.8,
        v32_column_temperature=.6, v32_cfg_scale=2, v32_year=2020,
        v32_descriptors='style/clean', v32_negative_descriptors='style/messy, style/handstream',
    )
    assert options['difficulty_rules']['hard'] == {
        'rate': 11.0, 'chord': 4, 'gap': 45, 'peak': 24, 'hold_ms': 800,
    }
    assert options['pattern'] == 'jumpstream'
    assert options['v32_descriptors'] == ['style/clean', 'style/jumpstream']
    assert options['v32_negative_descriptors'] == ['style/messy', 'style/handstream']
    assert options['v32_temperature'] == 1.1 and options['v32_year'] == 2020


def test_mug_difficulty_default_preserves_previous_conditioning():
    options = server.settings('t', 'a', '["easy"]', .15, 50, 42, None)
    assert options['mug_difficulty'] == 8


@pytest.mark.parametrize('kwargs', [
    {'difficulty_rules': '{"hard":{"rate":99}}'},
    {'difficulty_rules': '{"unknown":{"rate":8}}'},
    {'pattern': 'invented'},
    {'v32_descriptors': 'not/a/real/tag'},
    {'v32_negative_descriptors': 'style/messy'},
    {'v32_negative_descriptors': 'style/messy', 'v32_descriptors': 'style/clean', 'v32_cfg_scale': 1},
])
def test_invalid_advanced_parameters_are_rejected(kwargs):
    with pytest.raises(server.HTTPException) as error:
        server.settings('t', 'a', '["hard"]', .15, 50, 42, None, **kwargs)
    assert error.value.status_code == 400


def test_custom_difficulty_rules_reach_chart_calibration():
    candidates = [(float(t), 1.0, [Note(float(t), t // 100 % 4, float(t + 700))])
                  for t in range(100, 20000, 100)]
    notes, report = calibrate(candidates, 20000, 'hard', .2, 12,
                              {'rate': 18, 'chord': 3, 'gap': 75, 'peak': 9, 'hold_ms': 320})
    assert report['target_active_nps'] == 18
    assert report['max_chord'] == 3 and report['min_lane_gap_ms'] == 75
    assert report['max_hold_ms'] == 320 and report['peak_cap'] == 9
    assert chart_stats(notes, 20)['peak_nps'] <= 9


def test_model_condition_options_are_mapped_to_both_engines():
    options = {'title': 'song', 'artist': 'artist', 'seed': 123, 'ln_ratio': .35,
               'mug_difficulty': 6.5, 'mug_style': 'loved', 'pattern': 'chordjack',
               'pattern_strength': 25, 'v32_difficulty': 7, 'v32_temperature': 1.2,
               'v32_top_p': .82, 'v32_column_temperature': .65, 'v32_cfg_scale': 2.2,
               'v32_year': 2018, 'v32_descriptors': ['style/clean'],
               'v32_negative_descriptors': ['style/messy']}
    features = mug_condition_features(options)
    assert features['sr'] == 6.5 and features['rank_status'] == 'loved'
    assert features['chordjack'] == 1 and features['chordjack_ett'] == 25
    request = build_worker_request('input.wav', 'output', options)
    assert request['presets'][0]['sr'] == 7
    assert request['temperature'] == 1.2 and request['top_p'] == .82
    assert request['mania_column_temperature'] == .65 and request['cfg_scale'] == 2.2
    assert request['year'] == 2018 and request['descriptors'] == ['style/clean']


def test_upload_forwards_custom_values_to_generation(monkeypatch):
    captured = []

    def capture(source, options):
        captured.append((source, options))
        return {'id': 'parameter-upload'}

    monkeypatch.setattr(server, 'ensure_engine', lambda options: None)
    monkeypatch.setattr(server, 'new_job', capture)
    rules = {'lunatic': {'rate': 31, 'chord': 4, 'gap': 30, 'peak': 56, 'hold_ms': 150}}
    with TestClient(server.app) as client:
        response = client.post('/api/jobs', files={'file': ('track.wav', b'RIFFtest')}, data={
            'title': 'song', 'engine': 'v32', 'difficulty_rules': json.dumps(rules),
            'pattern': 'stream', 'v32_difficulty': '9', 'v32_temperature': '1.15',
            'v32_top_p': '.75', 'v32_column_temperature': '.7', 'v32_cfg_scale': '2',
            'v32_year': '2019', 'v32_descriptors': 'style/clean',
        })
    assert response.status_code == 200
    source, options = captured[0]
    try:
        assert source.read_bytes() == b'RIFFtest'
        assert options['difficulty_rules'] == rules
        assert options['pattern'] == 'stream'
        assert options['v32_difficulty'] == 9 and options['v32_temperature'] == 1.15
        assert options['v32_descriptors'] == ['style/clean', 'skillset/streams']
    finally:
        source.unlink(missing_ok=True)
