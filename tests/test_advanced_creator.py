"""Creator metadata is editable without changing immutable musical evidence."""
import copy
import io
import json
import zipfile

import numpy as np
import pytest
import soundfile as sf

from malody_studio import advanced as a
from malody_studio.naming import DEFAULT_CHART_CREATOR


@pytest.fixture
def project(tmp_path, monkeypatch):
    source = tmp_path / 'input.wav'
    sf.write(source, np.sin(np.arange(a.SR * 2) * .1) * .1, a.SR, subtype='FLOAT')
    monkeypatch.setattr(a, 'audio_metadata', lambda data: (
        [.1] * 2400, {'bpm':120., 'beat_times':[0, .5, 1, 1.5],
                      'beat_variability':0, 'uncertain':False, 'warnings':[]}))
    store = a.ProjectStore(tmp_path / 'outputs' / 'advanced')
    return store, store.create(source, 'Creator fixture', 'Artist'), source


def test_creator_create_update_and_revision_conflict(project):
    store, p, source = project
    assert DEFAULT_CHART_CREATOR == p['creator'] == 'Startrail'
    custom = store.create(source, 'Custom', creator='  自定义谱师  ')
    assert custom['creator'] == store.load(custom['id'])['creator'] == '自定义谱师'
    p = store.update(p['id'], {'creator':'  A / 谱师  '}, expected=p['revision'])
    assert p['creator'] == store.load(p['id'])['creator'] == 'A / 谱师'
    with pytest.raises(ValueError, match='项目已更新'):
        store.update(p['id'], {'creator':'Stale'}, expected=p['revision'] - 1)
    assert store.load(p['id']) == p
    assert store.update(p['id'], {'creator':'華' * 120})['creator'] == '華' * 120


@pytest.mark.parametrize('value', ['', '   ', '華' * 121, None, 42, [], True])
def test_invalid_creator_never_writes_project(project, value):
    store, p, source = project
    path = store.directory(p['id']) / 'project.json'
    frozen = path.read_bytes()
    directories = set(store.root.iterdir())
    with pytest.raises(ValueError):
        store.create(source, 'Invalid', creator=value)
    assert set(store.root.iterdir()) == directories
    with pytest.raises(ValueError):
        store.update(p['id'], {'creator':value}, expected=p['revision'])
    assert path.read_bytes() == frozen


def test_historical_missing_creator_is_read_only_default(project):
    store, p, _ = project
    del p['creator']
    path = store.directory(p['id']) / 'project.json'
    a.atomic(path, p)
    before, modified = path.read_bytes(), path.stat().st_mtime_ns
    loaded = store.load(p['id'])
    assert loaded == {**p, 'creator':'Startrail'}
    loaded['creator'] = 'snapshot only'
    assert store.load(p['id'])['creator'] == 'Startrail'
    assert path.read_bytes() == before and path.stat().st_mtime_ns == modified


def archive_contents(path):
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        charts = {name:json.loads(archive.read(name)) for name in archive.namelist()
                  if name.endswith('.mc')}
        audio = archive.read('0/audio.ogg')
    return charts, sf.read(io.BytesIO(audio), dtype='float32', always_2d=True)


@pytest.mark.parametrize('cache_kind', ['current', 'legacy-fingerprint', 'no-fingerprint'])
def test_creator_reexport_real_zip_preserves_old_evidence(project, cache_kind):
    store, p, source = project
    p = store.update(p['id'], {'patterns':['balanced'], 'difficulties':['easy','hard']})
    p = store.add_segment(p['id'], {'start_sample':0, 'end_sample':2 * a.SR})
    for variant in p['variants']:
        store.add_revision(p['id'], p['segments'][0]['id'], variant['key'], [
            {'id':a.uid(), 'start_ms':250, 'end_ms':750, 'lane':0},
            {'id':a.uid(), 'start_ms':1000, 'end_ms':None, 'lane':2}],
            p['settings'], 'model_raw')
    directory = store.directory(p['id'])
    revisions = {path:path.read_bytes() for path in (directory / 'revisions').glob('*.json')}
    sources = {path:path.read_bytes() for path in directory.glob('source*')}
    first = a.assemble(store, p['id'], preroll=0)
    assert first['creator'] == 'Startrail'
    assert a.assemble(store, p['id'], preroll=0)['reused']
    old_archive = a.assembly_archive(store, p['id'], first['id'])
    old_charts, (old_pcm, old_rate) = archive_contents(old_archive)
    assert len(old_charts) == 2
    assert all(chart['meta']['creator'] == 'Startrail' for chart in old_charts.values())
    frozen_files = {path:path.read_bytes() for path in old_archive.parent.rglob('*') if path.is_file()}
    current = store.load(p['id'])
    selected = copy.deepcopy(current['segments'])
    if cache_kind != 'current':
        old = current['assemblies'][-1]
        if cache_kind == 'no-fingerprint':
            old.pop('fingerprint')
        else:
            old['fingerprint'] = 'pre-creator-fingerprint'
        old.pop('creator')
        a.atomic(directory / 'project.json', current)
        # Exercise the historical cache path before any metadata mutation.
        fresh = a.assemble(store, p['id'], preroll=0)
        assert fresh['id'] != first['id'] and fresh['creator'] == 'Startrail'
        current = store.load(p['id'])
    updated = store.update(p['id'], {'creator':'  新谱师 / 谱师  '}, expected=current['revision'])
    for rid in selected[0]['active'].values():
        local = store.local_chart(p['id'], rid)
        assert local['creator'] == local['chart']['meta']['creator'] == '新谱师 / 谱师'
    second = a.assemble(store, p['id'], preroll=0, expected=updated['revision'])
    assert second['id'] != first['id'] and second['fingerprint'] != first['fingerprint']
    assert second['creator'] == '新谱师 / 谱师'
    report = a.read(directory / 'assemblies' / second['id'] / 'report.json')
    assert report['creator'] == second['creator']
    charts, (pcm, rate) = archive_contents(a.assembly_archive(store, p['id'], second['id']))
    assert charts.keys() == old_charts.keys()
    for name, chart in charts.items():
        assert chart['meta']['creator'] == second['creator']
        assert chart['note'] == old_charts[name]['note']
        assert chart['time'] == old_charts[name]['time']
        assert chart['effect'] == old_charts[name]['effect']
    assert rate == old_rate
    np.testing.assert_array_equal(pcm, old_pcm)
    assert store.load(p['id'])['segments'] == selected
    assert all(path.read_bytes() == blob for path, blob in {**revisions, **sources, **frozen_files}.items())
    assert source.read_bytes() == sources[directory / 'source-original.wav']
    reused = a.assemble(store, p['id'], preroll=0)
    assert reused['id'] == second['id'] and reused['reused'] and reused['creator'] == second['creator']
