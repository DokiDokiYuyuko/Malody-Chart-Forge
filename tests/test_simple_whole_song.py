import copy
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from malody_studio import simple_generation as simple


@pytest.fixture(autouse=True)
def isolate_unused_mug_adapter(monkeypatch):
    from malody_studio import pipeline
    monkeypatch.setattr(pipeline.engine, 'unload', lambda: None)


def test_queue_freezes_ordinary_jobs_and_passes_advanced_snapshots_unchanged(tmp_path, monkeypatch):
    from malody_studio import server, quality_workflow
    ordinary = {'title': 'simple', 'artist': 'artist', 'dynamic_enabled': True,
                'density_policy': {'old': True}, 'quality_policy': {'old': True},
                'timing_reference': 'old.osu', 'start_time': 1000, 'end_time': 2000}
    advanced = {'title': 'advanced', 'artist': 'artist', '_advanced': {
        'settings': {'dynamic_enabled': True}, 'density_policy': {'existing': 'frozen'},
        'section_plan': {'id': 'unchanged'}, 'generation_context_policy': {'existing': 'continuous'}}}
    baseline = copy.deepcopy([ordinary, advanced])
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    (tmp_path/'outputs').mkdir()
    monkeypatch.setattr(server, 'jobs', {})
    monkeypatch.setattr(server, 'queue_reservations', {})
    monkeypatch.setattr(server, 'store', lambda *_: None)
    monkeypatch.setattr(server, 'dispatch_next', lambda: None)
    monkeypatch.setattr(quality_workflow, 'freeze', lambda *_: pytest.fail('simple submission used advanced calibration'))
    ids = server.enqueue_jobs([(ordinary, {'type': 'upload'}), (advanced, {'type': 'project_file'})])
    frozen = server.jobs[ids[0]['id']]['options']
    assert frozen['simple_generation_policy'] == simple.contract()
    assert frozen['dynamic_enabled'] is False
    assert 'density_policy' not in frozen and 'timing_reference' not in frozen
    assert frozen['candidate_policy']['acoustic_new_heads'] is False
    assert server.jobs[ids[1]['id']]['options'] is advanced
    assert [ordinary, advanced] == baseline
    assert simple.freeze(frozen) == frozen


def test_corrupt_simple_policy_is_rejected_before_any_job_is_created(tmp_path, monkeypatch):
    from malody_studio import server
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    policy = {**simple.contract(), 'automatic_density_retries': 2}
    with pytest.raises(ValueError, match='策略版本'):
        server.enqueue_jobs([({'title': 'test', 'simple_generation_policy': policy}, {})])
    assert not (tmp_path/'outputs').exists()
    with pytest.raises(ValueError, match='高级任务'):
        simple.freeze({'_advanced': {'settings': {}}})


@pytest.mark.parametrize('tail_enabled', [False, True])
def test_density_target_uses_full_pcm_channels_and_fixed_music_activity(tmp_path, tail_enabled):
    sr = 44100
    pcm = np.zeros((8*sr, 2), np.float32)
    pcm[2*sr:5*sr] = [.1, -.1]
    source = tmp_path/'retained.wav'
    sf.write(source, pcm[:5*sr] if tail_enabled else pcm, sr, subtype='FLOAT')
    result = simple.density_target(source, {'difficulty_rules': {'expert': {'rate': 13}}})
    assert result['sections'][0]['active_seconds'] == pytest.approx(3)
    assert result['sections'][0]['per_difficulty']['expert']['target_heads_soft'] == pytest.approx(39)
    assert result['samples'] == (5 if tail_enabled else 8)*sr
    assert result['sections'][0]['core'] == [0, result['samples']]
    assert len(result['sections']) == 1


def model_osu():
    heads = '\n'.join(f'{64+(i%4)*128},192,{500+i*400},1,0,0:0:0:0:' for i in range(16))
    return ('[General]\nMode:3\n[Difficulty]\nCircleSize:4\n'
            '[TimingPoints]\n0,500,4,2,0,100,1,0\n4000,400,4,2,0,100,1,0\n'
            '[HitObjects]\n'+heads+'\n')


@pytest.mark.parametrize('dynamic', [False, True])
@pytest.mark.parametrize('bpm', [None, 137])
def test_actual_simple_adapter_sends_one_full_request_for_all_six_difficulties(
        tmp_path, monkeypatch, dynamic, bpm):
    from malody_studio import pipeline, mapperatorinator, resident, advanced_generation, section_plan, difficulty, quality, library, quality_workflow
    sr = 44100
    pcm = np.column_stack([np.sin(np.arange(sr*8)*2*np.pi*440/sr)*.1]*2).astype(np.float32)
    source = tmp_path/'input.wav'
    sf.write(source, pcm, sr, subtype='FLOAT')
    output = tmp_path/'output'
    raw_calls = []
    def worker(engine, message, progress):
        request = json.loads(Path(message['request_path']).read_text(encoding='utf-8'))
        raw_calls.append(request)
        assert engine == 'v32'
        assert request['seed'] == 42 and request['difficulty'] == 6
        assert sf.info(request['audio']).duration == pytest.approx(8)
        assert len(request['presets']) == 1
        assert request['presets'][0] == {'label': '共享母谱', 'sr': 6, 'key': 'master'}
        assert not any(field in request for field in ('start_time', 'end_time', 'timing_reference', 'timing_fallback_reference'))
        model_path = Path(request['output'])/'master.osu'
        model_path.write_text(model_osu(), encoding='utf-8')
        return {'charts': {'master': str(model_path)}, 'device': 'test'}
    def forbidden(*args, **kwargs):
        pytest.fail('ordinary whole-song task entered segmentation, timing injection or acoustic note supply')
    monkeypatch.setattr(mapperatorinator, 'ready', lambda: True)
    monkeypatch.setattr(resident, 'call', worker)
    monkeypatch.setattr(advanced_generation, 'generate_sectioned', forbidden)
    monkeypatch.setattr(advanced_generation, 'attach_timing_reference', forbidden)
    monkeypatch.setattr(pipeline, 'analyze', forbidden)
    monkeypatch.setattr(section_plan, 'build_plan', forbidden)
    monkeypatch.setattr(section_plan, '_boundaries', forbidden)
    monkeypatch.setattr(difficulty, 'attacks', forbidden)
    monkeypatch.setattr(quality, 'assess', lambda *_: [])
    monkeypatch.setattr(library, 'publish', lambda *args, **kwargs: None)
    monkeypatch.setattr(quality_workflow, 'evidence_for', lambda *_: {'sources': {}})
    options = simple.freeze({'simple_generation_policy':simple.contract(), 'title': 'test', 'artist': 'artist', 'creator':'测试谱师 谱师', 'engine': 'v32',
        'seed': 42, 'v32_difficulty': 6, 'ln_ratio': .15, 'steps': 50,
        'bpm': bpm, 'dynamic_enabled': dynamic, 'patterns': ['balanced'],
        'difficulties': list(pipeline.PRESETS), 'tail_trim_enabled': True})
    report, archive = pipeline.run(source, output, options, lambda *_: None)
    assert report['creator'] == report['generation_settings']['creator'] == options['creator']
    assert len(raw_calls) == 1
    assert len(report['difficulties']) == 6
    assert report['section_plan_id'] is None
    assert report['bpm'] == 120  # Model timing, even if the UI supplied 137.
    assert report['analysis']['timing_source'] == 'model_output'
    assert report['analysis']['beat_times'] == []
    assert len(report['analysis']['waveform']) == 420
    assert not (output/'sectioned-models').exists()
    assert not (output/'section-plan.json').exists()
    originals, _, _ = mapperatorinator.read_osu(output/'v32-original'/'balanced'/'v32-original'/'master.osu')
    for row in report['difficulties']:
        preview = report['previews'][row['chart_id']]
        assert {head[0] for head in preview} <= {n.start for n in originals}
        assert row['difficulty_adjustment']['selected_acoustic_heads'] == 0
        density = row['density_validation']
        assert density['actual_heads'] == len(preview)
        assert density['attempts'] == 0 and density['retry_allowed'] is False
        if row['difficulty'] == 'expert':
            assert density['status'] == 'underfilled' and density['passed'] is False
    import zipfile
    with zipfile.ZipFile(archive) as package:
        assert package.testzip() is None
        assert len([name for name in package.namelist() if name.endswith('.mc')]) == 6
        assert all(json.loads(package.read(name))['meta']['creator'] == options['creator']
                   for name in package.namelist() if name.endswith('.mc'))


def test_multiple_patterns_make_one_whole_song_call_each_and_keep_successful_siblings(tmp_path, monkeypatch):
    from malody_studio import pipeline, mapperatorinator, quality_workflow, quality, library
    from malody_studio.charts import Note
    sr = 44100
    source = tmp_path/'input.wav'
    sf.write(source, np.full((8*sr, 2), .1, np.float32), sr, subtype='FLOAT')
    calls = []
    def generate(source, folder, options, progress):
        calls.append(options)
        assert not options.get('_advanced_presets')
        if options['pattern'] == 'technical':
            raise ValueError('missing model timing')
        return {key: ([Note(500+i*400, i%4) for i in range(16)], [[0, 120]], 0)
                for key in options['difficulties']}, {}
    monkeypatch.setattr(mapperatorinator, 'generate', generate)
    monkeypatch.setattr(quality_workflow, 'evidence_for', lambda *_: {'sources': {}})
    monkeypatch.setattr(quality, 'assess', lambda *_: [])
    monkeypatch.setattr(library, 'publish', lambda *args, **kwargs: None)
    options = simple.freeze({'simple_generation_policy':simple.contract(), 'title': 'test', 'artist': 'artist', 'engine': 'v32', 'seed': 42,
        'v32_difficulty': 6, 'ln_ratio': .15, 'steps': 50, 'patterns': ['balanced', 'technical'],
        'difficulties': ['hard', 'expert'], 'dynamic_enabled': True})
    report, _ = pipeline.run(source, tmp_path/'out', options, lambda *_: None)
    assert len(calls) == 2
    assert report['partial'] is True
    assert report['patterns'] == ['balanced']
    assert [row['difficulty'] for row in report['difficulties']] == ['hard', 'expert']
    assert 'missing model timing' in report['pattern_errors'][0]['error']
    assert report['bpm'] == 120  # Use successful model timing, not a second detector.


def test_mug_keeps_one_existing_whole_song_mother_call(tmp_path, monkeypatch):
    from malody_studio import pipeline, quality_workflow, quality, library
    from malody_studio.charts import Note
    sr = 44100
    source = tmp_path/'input.wav'
    sf.write(source, np.full((8*sr, 2), .1, np.float32), sr, subtype='FLOAT')
    calls = []
    monkeypatch.setattr(pipeline.engine, 'prepare', lambda *args: 'full-wave')
    def generate(wave, unused, options, progress):
        calls.append(options)
        assert wave == 'full-wave' and not options.get('_advanced')
        return [Note(500+i*400, i%4) for i in range(16)]
    monkeypatch.setattr(pipeline.engine, 'generate', generate)
    monkeypatch.setattr(quality_workflow, 'evidence_for', lambda *_: {'sources': {}})
    monkeypatch.setattr(quality, 'assess', lambda *_: [])
    monkeypatch.setattr(library, 'publish', lambda *args, **kwargs: None)
    options = simple.freeze({'title': 'test', 'artist': 'artist', 'engine': 'mug', 'seed': 42,
        'mug_difficulty': 4, 'ln_ratio': .15, 'steps': 20, 'patterns': ['balanced'],
        'difficulties': ['easy', 'hard'], 'dynamic_enabled': True, 'bpm': 120})
    report, _ = pipeline.run(source, tmp_path/'out', options, lambda *_: None)
    assert len(calls) == 1 and len(report['difficulties']) == 2


def test_rule_revision_uses_cached_model_heads_and_keeps_denominator(tmp_path, monkeypatch):
    from malody_studio import quality_workflow, section_plan
    from malody_studio.charts import Note
    from malody_studio.adaptive_difficulty import model_candidates
    pcm = np.zeros((8*44100,2),np.float32);pcm[44100:7*44100]=.1
    source = tmp_path/'source.wav';sf.write(source,pcm,44100,subtype='FLOAT')
    target = simple.density_target(source,{})
    raw = [{'id':str(i),'start_ms':1000+i*400,'lane':i%4,'end_ms':None} for i in range(12)]
    candidates = model_candidates([Note(e['start_ms'],e['lane']) for e in raw])
    cache = {'simple_generation_policy':simple.contract(),'density_plan':target,
             'raw_events':raw,'raw_count':12}
    original=copy.deepcopy(cache)
    settings=simple.freeze({'ln_ratio':.15,'difficulty_rules':{'expert':{'rate':10}}})
    monkeypatch.setattr(quality_workflow,'evidence_for',lambda *_:{'sources':{}})
    monkeypatch.setattr(section_plan,'build_plan',lambda *_:pytest.fail('rule change reanalyzed music'))
    notes,result=simple.finish_rule_revision([Note(e['start_ms'],e['lane']) for e in raw],
        candidates,cache,settings,tmp_path,'expert','balanced',{'candidate_model_heads':12})
    assert len(notes)==12 and cache==original
    assert result['density_validation']['active_seconds']==6
    assert result['density_validation']['target_heads']==60
    assert result['density_validation']['actual_heads']==12
    assert result['density_validation']['status']=='underfilled'
    assert result['density_validation']['retry_allowed'] is False


def test_simple_native_rhythm_hydration_preserves_tap_and_hold_coordinates(tmp_path):
    from malody_studio.charts import Note
    path=tmp_path/'master.osu'
    events=[['t',100],['snap',4],['pos_x',64],['hold_note',0],
            ['t',120],['snap',2],['pos_x',192],['circle',0],
            ['t',300],['snap',1],['pos_x',64],['hold_note_end',0]]
    (tmp_path/'model-events.json').write_text(json.dumps({'types_first':False,'keycount':4,
        'events':events,'timing':['0,500,4,2,0,100,1,0']}),encoding='utf-8')
    notes=[Note(100,0,300),Note(120,1)];before=copy.deepcopy(notes)
    simple.hydrate_rhythm(notes,{'charts':{'master':str(path)}})
    assert notes==before
    assert notes[0].model_rhythm['snap_divisor']==4
    assert notes[1].model_rhythm['snap_divisor']==2
