import copy
import json
import zipfile
import pytest
from malody_studio.charts import Note, serialize, validate_chart, clean_notes, package, beat_value, ChartStructureError
from malody_studio.paths import PRESETS

def test_preserve_time_and_negative_bgm_offset():
    chart = serialize([Note(1000, 0, 1750), Note(1800, 1), Note(2437, 2)], '曲名', '作者', '4K', 120, origin=500)
    assert chart['note'][-1]['offset'] == -500
    assert float(beat_value(chart['note'][0]['beat'])) * 500 + 500 == 1000
    assert abs(float(beat_value(chart['note'][2]['beat'])) * 500 + 500 - 2437) < .3
    assert validate_chart(chart, 3000)['notes'] == 3

def test_hold_across_bpm_change():
    chart = serialize([Note(0, 0)], 'test', 'test', '4K', 120)
    chart['time'].append({'beat': [2, 0, 1], 'bpm': 60})
    chart['note'][0] = {'beat': [1, 0, 1], 'endbeat': [3, 0, 1], 'column': 0}
    assert validate_chart(chart, 2000)['valid']
    with pytest.raises(ValueError):
        validate_chart(chart, 1700)

def test_track_conflict_reports_lane_and_timestamp():
    chart=serialize([Note(0,0,1000),Note(500,0)],'test','test','4K',120)
    with pytest.raises(ChartStructureError) as caught:
        validate_chart(chart,3000)
    assert caught.value.lane==0
    assert caught.value.start_ms==500

@pytest.mark.parametrize('event', [
    {'beat': [1, 0, 1], 'column': 0},
    {'beat': [1, 0, 1], 'column': 4},
    {'beat': [3, 0, 1], 'endbeat': [2, 0, 1], 'column': 1},
])
def test_reject_lane_overlap_and_invalid_holds(event):
    chart = serialize([Note(0, 0, 1500)], 'test', 'test', '4K', 120)
    chart['note'].insert(1, event)
    with pytest.raises(ValueError):
        validate_chart(chart, 3000)

def test_phone_limits_and_audio_tail():
    notes = [Note(t, lane, t + 250 if lane == 0 else None) for t in range(0, 4000, 40) for lane in range(4)]
    clean = clean_notes(notes, 3000, PRESETS['easy'])
    assert len(clean) < len(notes)
    assert all(n.start < 3000 and (n.end or n.start) <= 3000 for n in clean)
    assert validate_chart(serialize(clean, 'test', 'test', 'easy', 120), 3000)['valid']

def test_zip_contains_real_ogg_and_utf8_chart(tmp_path):
    import numpy as np
    import soundfile as sf
    audio = tmp_path / 'source.ogg'
    sf.write(audio, np.zeros(44100), 44100, format='OGG', subtype='VORBIS')
    chart = serialize([Note(500, 2)], '天狼星', 'ヰ世界情緒', '入门', 120)
    chart['time'].extend([
        {'beat': [1, 0, 1], 'bpm': 120.0},
        {'beat': [2, 0, 1], 'bpm': 90.0},
    ])
    archive = package(tmp_path, {'easy': chart}, audio, {'duration': 1})
    with zipfile.ZipFile(archive) as z:
        assert '0/' not in z.namelist()
        assert '0/generation.txt' not in z.namelist()
        assert z.read('0/audio.ogg')[:4] == b'OggS'
        packed = json.loads(z.read('0/easy.mc'))
        assert packed['meta']['song']['title'] == '天狼星'
        assert packed['time'] == [chart['time'][0], chart['time'][2]]
        assert z.testzip() is None
