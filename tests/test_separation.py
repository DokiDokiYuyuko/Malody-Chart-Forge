import copy
import numpy as np
import pytest
import soundfile as sf

from malody_studio import separation as s
from malody_studio.stem_generation import _seed, materialize_stems
from malody_studio import stem_generation as sg


def manifest(directory, frames=4410):
    data = np.zeros((frames, 2), dtype=np.float32)
    data[200:230] = .2
    rows = []
    for role, amplitude in (('vocals', 1.), ('accompaniment', .5)):
        audio = data * amplitude; path = directory / (role + '.wav')
        sf.write(path, audio, s.SR, subtype='FLOAT')
        rows.append({'role': role, 'source_id': 'set:' + role, 'file': path.name, 'path': str(path),
                     'pcm_sha': s.pcm_hash(audio), 'file_sha256': s.file_hash(path), 'frames': frames})
    return {'id': 'set', 'recipe_hash': 'recipe', 'adapter_version': s.VERSION,
            'frame_count': frames, 'origin_sample': 0, 'sample_rate': s.SR, 'stems': rows}


def test_float_frames_source_origin_and_integrity(tmp_path):
    m = manifest(tmp_path)
    assert s.validate_manifest(tmp_path, m, 'recipe')['frame_count'] == 4410
    for field, value in (('sample_rate', 22050), ('origin_sample', 1), ('frame_count', 4411)):
        bad = {**m, field: value}
        with pytest.raises(ValueError): s.validate_manifest(tmp_path, bad)
    data, rate = sf.read(tmp_path / 'vocals.wav', dtype='float32', always_2d=True)
    data[0] = .3; sf.write(tmp_path / 'vocals.wav', data, rate, subtype='FLOAT')
    with pytest.raises(ValueError): s.validate_manifest(tmp_path, m)


def test_materialized_sources_stay_within_project_and_keep_float_pcm(tmp_path):
    cache = tmp_path / 'cache'; cache.mkdir(); m = manifest(cache)
    project = tmp_path / 'project'
    local = materialize_stems(project, m)
    assert all(__import__('pathlib').Path(row['path']).is_relative_to(project) for row in local['stems'])
    assert [row['pcm_sha'] for row in m['stems']] == [row['pcm_sha'] for row in local['stems']]
    assert m['stems'][0]['path'] != local['stems'][0]['path']


def test_distinct_stem_seeds_and_strict_configuration():
    assert _seed(10, 'vocals', 'same') != _seed(10, 'accompaniment', 'same')
    assert _seed(10, 'vocals', 'same') == _seed(10, 'vocals', 'same')
    assert _seed(10, 'vocals', 'other') != _seed(10, 'vocals', 'same')
    for params in ({'model': 'hpss'}, {'overlap': float('nan')}, {'segment': 8}, {'shifts': True}, {'seed': 1.2}):
        with pytest.raises(ValueError): s.validated_settings(params)


def test_separation_browser_controls_match_backend_limits():
    import json
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to verify browser/backend parameter compatibility')
    result = subprocess.run(
        [node, '-e', "console.log(JSON.stringify(require('./web/advanced-workflow.js').separationFields))"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding='utf-8', check=True,
    )
    for key, title, default, minimum, maximum, step, help_text in json.loads(result.stdout):
        assert (minimum, maximum) == s.PARAMETER_LIMITS[key][:2], key
        assert default == s.DEFAULTS[key], key


def test_inference_verification_is_model_specific_and_keeps_legacy_manifests():
    old={'inference_verified':True,'verification':{'model':'htdemucs'}}
    assert s.model_inference_verified(old,'htdemucs')
    assert not s.model_inference_verified(old,'htdemucs_ft')
    newer={'inference_verified':True,'verification':{'model':'htdemucs'},'verified_models':{
        'htdemucs':{'status':'passed','model':'htdemucs'},
        'htdemucs_ft':{'status':'passed','model':'htdemucs_ft'}}}
    assert s.model_inference_verified(newer,'htdemucs')
    assert s.model_inference_verified(newer,'htdemucs_ft')
    newer['verified_models']['htdemucs_ft']['status']='failed'
    assert not s.model_inference_verified(newer,'htdemucs_ft')


def test_demucs_progress_parser_reads_carriage_return_tqdm_output():
    assert s.model_progress_from_log('0%| | 0/12\r 42%|██ | 5/12') == 42
    assert s.model_progress_from_log('loading model; no progress yet') is None


def test_partial_or_escaping_stem_is_never_cache_hit(tmp_path):
    m = manifest(tmp_path)
    bad = copy.deepcopy(m); bad['stems'].pop()
    with pytest.raises(ValueError): s.validate_manifest(tmp_path, bad)
    bad = copy.deepcopy(m); bad['stems'][0]['file'] = '../vocals.wav'
    with pytest.raises(ValueError): s.validate_manifest(tmp_path, bad)


def test_parent_stage_preserves_strategy_and_reuses_raw_components(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from malody_studio import advanced_generation
    project_id = 'a' * 32; project = tmp_path / 'outputs' / 'advanced' / project_id
    project.mkdir(parents=True); cache = tmp_path / 'components'; cache.mkdir()
    m = manifest(cache); m['source_pcm_sha'] = 'pcm'; m['parent_source_id'] = 'pcm'
    source = project / 'source.wav'
    sf.write(source, np.full((4410, 2), .2, dtype=np.float32), s.SR, subtype='FLOAT')
    plan = {'id': 'b' * 32, 'source_pcm_sha': 'pcm', 'sections': [{'core': [0, 4410], 'per_difficulty': {
        'medium': {'target_heads_soft': 2, 'model_condition': 8, 'hard_caps': {'chord': 2}}}}]}
    monkeypatch.setitem(sys.modules, 'malody_studio.section_plan', SimpleNamespace(build_plan=lambda *_: plan, validate_plan=lambda *_: plan))
    monkeypatch.setattr(sg, 'ROOT', tmp_path)
    monkeypatch.setattr(sg, 'ensure_stems', lambda *_: copy.deepcopy(m))
    calls = []
    def generate(path, directory, options, progress):
        snapshot = options['_advanced']; calls.append(copy.deepcopy(snapshot))
        return {'advanced_result': [{'variant': 'balanced--medium', 'events': [{'id': snapshot['source']['source_role'],
            'start_ms': 5., 'lane': 0, 'end_ms': None}], 'settings': snapshot['settings'], 'provenance': {}}], 'errors': []}
    monkeypatch.setattr(advanced_generation, 'run', generate)
    settings = {'strategy': 'fast', 'dynamic_enabled': True, 'seed': 23, 'difficulty_rules': {}}
    segment = {'id': 'c' * 32, 'start_sample': 0, 'end_sample': 4410, 'versions': {}}
    options = {'_advanced': {'project': {'id': project_id, 'tempo': {}}, 'segment': segment, 'settings': settings,
                            'variants': [{'key': 'balanced--medium', 'pattern': 'balanced', 'difficulty': 'medium'}]}}
    result = sg.run(source, tmp_path / 'job', options, lambda *_: None)
    assert len(calls) == 2
    assert all(call['_stem_raw_only'] and call['settings']['strategy'] == 'fast' and call['settings']['dynamic_enabled'] for call in calls)
    assert calls[0]['settings']['seed'] != calls[1]['settings']['seed']
    raw = [r for r in result['advanced_result'] if r['kind'] == 'stem_raw']
    assert len(raw) == 2 and all(r['activate_initial'] is False for r in raw)
    assert result['advanced_result'][-1]['kind'] == 'fusion'
    assert result['advanced_result'][-1]['provenance']['parents'] == [r['id'] for r in raw]
    recovered = sg.run(source, tmp_path / 'recovery-job', options, lambda *_: None)
    assert len(calls) == 2
    assert [r['id'] for r in recovered['advanced_result'] if r['kind'] == 'stem_raw'] == [r['id'] for r in raw]
    revisions = project / 'revisions'; revisions.mkdir()
    from malody_studio.advanced import atomic
    for row in raw:
        atomic(revisions / (row['id'] + '.json'), row)
    segment['versions'] = {'balanced--medium': [{'id': row['id']} for row in raw]}
    repeated = sg.run(source, tmp_path / 'repeat-job', options, lambda *_: None)
    assert len(calls) == 2
    assert [r['kind'] for r in repeated['advanced_result']] == ['fusion']




def test_changed_tempo_reference_invalidates_stem_inference_cache(tmp_path,monkeypatch):
    monkeypatch.setattr(sg,'ROOT',tmp_path)
    settings={'engine':'v32','seed':5};source={'source_id':'stem:vocals','pcm_sha':'pcm'}
    segment={'start_sample':0,'end_sample':44100}
    first=sg._raw_key(settings,source,segment,'speed--medium',{'sections':[],'reference_hash':'original-reference'})
    changed=sg._raw_key(settings,source,segment,'speed--medium',{'sections':[],'reference_hash':'edited-reference'})
    assert first!=changed
