import json
import zipfile

from malody_studio import server
from malody_studio.charts import Note, package, serialize
from malody_studio.difficulty import calibrate
from malody_studio.naming import chart_id, chart_stem
from malody_studio.pipeline import _variant_seed


def test_multiple_pattern_settings_keep_independent_v32_tags():
    options = server.settings(
        'song', 'artist', '["hard", "expert"]', .15, 50, 42, None, 'v32',
        pattern='jackspeed', patterns='["jackspeed", "stream"]',
        v32_descriptors='style/clean',
    )

    assert options['patterns'] == ['jackspeed', 'stream']
    assert options['v32_descriptors_base'] == ['style/clean']
    assert options['v32_descriptors'] == ['style/clean', 'skillset/speedjack']
    assert options['difficulties'] == ['hard', 'expert']


def test_chart_names_identify_model_pattern_and_difficulty():
    assert chart_id('stream', 'hard') == 'stream--hard'
    name = chart_stem('Song: A/B', 'v32', 'stream', 'hard')
    assert name == 'Song_ A_B_Mapperatorinator-V32_Stream_Hard'


def test_jack_stream_and_speed_are_measurably_distinct():
    candidates = [(i * 250.0, 1.0, [Note(i * 250.0, i % 4)]) for i in range(80)]
    summaries = {}
    for pattern in ('jackspeed', 'stream', 'speed'):
        notes, metrics = calibrate(candidates, 20_000, 'hard', 0, 42, {}, pattern=pattern)
        summaries[pattern] = metrics
        assert notes
        assert metrics['pattern'] == pattern

    assert summaries['jackspeed']['same_lane_repeat_ratio'] > summaries['stream']['same_lane_repeat_ratio']
    assert summaries['speed']['hand_alternation_ratio'] > summaries['stream']['hand_alternation_ratio']
    assert _variant_seed(42, 'stream') == _variant_seed(42, 'stream')
    assert _variant_seed(42, 'stream') != _variant_seed(42, 'jackspeed')


def test_package_keeps_named_pattern_charts_and_legacy_layout(tmp_path):
    output = tmp_path / 'bundle'
    output.mkdir()
    audio = tmp_path / 'audio.ogg'
    audio.write_bytes(b'ogg-test')
    chart = serialize([Note(1000, 0, 1500)], 'Song', 'Artist', 'Song_MuG_Stream_Hard', 120)
    filename = 'Song_MuG-Diffusion_Stream_Hard.mc'
    report = {'duration': 3, 'difficulties': [{'chart_id': 'stream--hard', 'filename': filename}]}

    archive_path = package(output, {'stream--hard': chart}, audio, report,
                           filenames={'stream--hard': filename})

    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        assert '0/' in archive.namelist()
        assert f'0/{filename}' in archive.namelist()
        assert '0/audio.ogg' in archive.namelist()
        assert json.loads(archive.read(f'0/{filename}'))['meta']['mode'] == 0
