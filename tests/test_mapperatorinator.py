import json
import pytest
from malody_studio.charts import Note, beat_value, validate_chart, package
from malody_studio.mapperatorinator import read_osu, serialize_with_timing


def test_only_advanced_requests_enable_native_attempt_diagnostics():
    from malody_studio.mapperatorinator import build_worker_request
    options = {'title':'source', 'artist':'artist', 'seed':17, 'ln_ratio':.2}
    simple = build_worker_request('input.wav', 'output', options)
    assert 'capture_native_trace' not in simple
    assert 'native_event_policy' not in simple and 'grammar_policy' not in simple
    presets = [{'key':'expert', 'label':'Expert', 'sr':5.9}]
    advanced = build_worker_request('input.wav', 'output', {**options, '_advanced_presets':presets})
    assert advanced['capture_native_trace'] is True and advanced['presets'] == presets
    assert advanced['native_event_policy'] == 'mania-unlocated-fragments-v2'
    assert advanced['grammar_policy'] == 'mania-grammar-mask-v1'
    assert {k:v for k,v in advanced.items() if k not in ('presets','capture_native_trace','native_event_policy','grammar_policy')} == {
        k:v for k,v in simple.items() if k != 'presets'}

def test_multi_bpm_preserves_taps_and_hold_release_in_audio_time():
    # A hold crosses two tempo changes, with an initial timing offset before audio zero.
    notes = [Note(250, 0, 2600), Note(1200, 1), Note(3100, 3)]
    chart = serialize_with_timing(notes, '曲名', '作者', 'V32', [(-250, 120), (1000, 60), (2000, 180)])
    assert validate_chart(chart, 4000)['valid']
    assert len(chart['time']) == 3
    def audio_ms(value):
        beat = float(beat_value(value))
        elapsed = 0
        for index, point in enumerate(chart['time']):
            start = float(beat_value(point['beat']))
            end = float(beat_value(chart['time'][index + 1]['beat'])) if index + 1 < len(chart['time']) else beat
            if beat <= start:
                break
            elapsed += (min(beat, end) - start) * 60000 / point['bpm']
        return elapsed - chart['note'][-1]['offset']
    for note, event in zip(notes, chart['note']):
        assert audio_ms(event['beat']) == pytest.approx(note.start, abs=.6)
        if note.end is not None:
            assert audio_ms(event['endbeat']) == pytest.approx(note.end, abs=.6)
    assert 'Mapperatorinator' in chart['meta']['creator']

def test_osu_4k_parser_and_mode_rejection(tmp_path):
    path = tmp_path / 'sample.osu'
    text = '[General]\nMode: 3\n[Difficulty]\nCircleSize:4\n[TimingPoints]\n-250,500,4,2,1,100,1,0\n1000,1000,4,2,1,100,1,0\n1500,-100,4,2,1,100,0,0\n[HitObjects]\n64,192,250,128,0,2600:0:0:0:0:\n448,192,3100,1,0,0:0:0:0:\n'
    path.write_text(text, encoding='utf-8')
    notes, timing, inherited = read_osu(path)
    assert notes == [Note(250, 0, 2600), Note(3100, 3)]
    assert timing == [(-250, 120), (1000, 60)] and inherited == 1
    for malformed in (text.replace('Mode: 3', 'Mode: 0'), text.replace('CircleSize:4', 'CircleSize:7'),
                      text.replace('448,192,3100,1', '448,192,3100,2')):
        path.write_text(malformed, encoding='utf-8')
        with pytest.raises(ValueError):
            read_osu(path)

def test_rare_invalid_lanes_are_discarded_without_clipping_and_excess_is_rejected(tmp_path):
    path = tmp_path / 'model.osu'
    header = '[General]\nMode:3\n[Difficulty]\nCircleSize:4\n[TimingPoints]\n0,500,4,2,1,100,1,0\n[HitObjects]\n'
    valid = ''.join(f'64,192,{i*200},1,0,0:0:0:0:\n' for i in range(200))
    path.write_text(header + valid + '576,192,0,1,0,0:0:0:0:\n')
    with pytest.raises(ValueError): read_osu(path)
    diagnostics = {}
    notes, _, _ = read_osu(path, discard_invalid_lanes=True, diagnostics=diagnostics)
    assert len(notes) == 200 and all(n.lane == 0 for n in notes)
    assert diagnostics['discarded_invalid_lane_notes'] == 1
    path.write_text(header + valid + '576,192,0,1,0,0:0:0:0:\n' * 10)
    with pytest.raises(ValueError): read_osu(path, discard_invalid_lanes=True)


def test_bad_primary_does_not_discard_valid_retry_and_sibling(tmp_path):
    from malody_studio.mapperatorinator import read_worker_charts
    header = '[General]\nMode:3\n[Difficulty]\nCircleSize:4\n[TimingPoints]\n0,500,4,2,1,100,1,0\n[HitObjects]\n'
    bad = tmp_path/'bad.osu'; good = tmp_path/'good.osu'; empty = tmp_path/'empty.osu'
    bad.write_text(header+'832,192,100,1,0,0:0:0:0:\n')
    good.write_text(header+'192,192,200,1,0,0:0:0:0:\n')
    empty.write_text(header)
    result={'charts':{'hard':str(bad),'hard__retry':str(good),'expert':str(empty)}}
    charts,meta=read_worker_charts(result)
    assert set(charts)=={'hard__retry','expert'}
    assert charts['hard__retry'][0]==[Note(200,1)] and charts['expert'][0]==[]
    assert 'hard' in meta['rejected_charts'] and len(meta['chart_diagnostics'])==3
    assert '832' in bad.read_text()  # Original model output remains unchanged.
    with pytest.raises(ValueError,match='所有输出'):
        read_worker_charts({'charts':{'hard':str(bad)}})
