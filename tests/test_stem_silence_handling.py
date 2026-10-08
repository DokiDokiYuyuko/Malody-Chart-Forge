"""Silent-stem handling: timing-empty requests (A), silent-span note drops at fusion (B)."""
import copy
import json

import numpy as np
import pytest
import soundfile as sf

from malody_studio import advanced, direct_v32, mapperatorinator, stem_silence
from malody_studio.charts import Note

SR = advanced.SR
RECOVERY_FAILED = '模型未生成节拍，项目参考恢复失败；请确认 BPM 后重试'
KEY0, KEY1 = 'balanced--expert__direct0', 'balanced--expert__direct1'


def tone(seconds, level=.5):
    t = np.arange(int(seconds * SR))
    return (level * np.sin(2 * np.pi * 220 * t / SR)).astype(np.float32)


def stem_pcm(*parts):
    """Stereo PCM from (active?, seconds) parts."""
    mono = np.concatenate([tone(s) if on else np.zeros(int(s * SR), np.float32) for on, s in parts])
    return np.stack([mono, mono], axis=1)


def request(key, a, b, variant='balanced--expert', seed=1):
    return dict(key=key, label=variant, variant=variant, pattern='balanced', difficulty_key='expert', sr=6.5,
                seed=seed, core=[int(a * SR), int(b * SR)], sections=[], start_time=int(a * 1000),
                end_time=int(b * 1000), core_start_time=int(a * 1000), core_end_time=int(b * 1000),
                context=[int(a * SR), int(b * SR)])


@pytest.fixture
def infer_case(tmp_path, monkeypatch):
    """Ten seconds: 0-4 s active, 4-10 s silent; two requests covering both parts."""
    source = tmp_path / 'stem.wav'
    sf.write(source, stem_pcm((1, 4), (0, 6)), SR, subtype='FLOAT')
    requests = [request(KEY0, 0, 4), request(KEY1, 4, 10, seed=2)]
    snapshot = dict(direct_v32_policy={'version': 'test'}, settings={'seed': 1}, source=dict(source_role='vocals'),
                    project=dict(samples=10 * SR, tail_trim=dict(enabled=False), title='t', artist='a'),
                    segment=dict(start_sample=0, end_sample=10 * SR), section_plan=dict(id='plan', samples=10 * SR),
                    variants=[dict(key='balanced--expert', pattern='balanced', difficulty='expert')])
    state = dict(errors={}, requests=requests)
    monkeypatch.setattr(direct_v32, 'load_mapping', lambda _p: {})
    monkeypatch.setattr(direct_v32, 'requests_for', lambda *_a: copy.deepcopy(state['requests']))

    def generate(_audio, _folder, options, _progress):
        raw = {r['key']: ([Note(1000., 0, None)], [], 0) for r in options['_advanced_presets']
               if r['key'] not in state['errors']}
        return raw, dict(rejected_charts=dict(state['errors']))

    monkeypatch.setattr(mapperatorinator, 'generate', generate)
    return tmp_path, source, snapshot, state


def run_infer(case, role='vocals', errors=None):
    tmp_path, source, snapshot, state = case
    state['errors'] = errors if errors is not None else {KEY1: RECOVERY_FAILED}
    snapshot = copy.deepcopy(snapshot)
    if role is None:
        snapshot.pop('source')
    else:
        snapshot['source'] = dict(source_role=role)
    return direct_v32.infer(source, tmp_path / 'direct', snapshot, lambda *_: None)


@pytest.mark.parametrize('role', ['vocals', 'accompaniment'])
@pytest.mark.parametrize('message', [RECOVERY_FAILED, 'No timing points found in beatmap.'])
def test_timing_empty_request_on_a_silent_stem_core_is_an_empty_contribution(infer_case, role, message):
    supplied, records, errors, failed, _meta = run_infer(infer_case, role, {KEY1: message})
    assert failed == set()
    by_key = {r['key']: r for r in records}
    assert by_key[KEY0]['status'] == 'generated'
    empty = by_key[KEY1]
    assert empty['status'] == 'empty_silent_stem' and empty['owned_heads'] == empty['model_heads'] == 0
    assert empty['suppressed_error'] == message and 'error' not in empty
    silence = empty['silence']
    assert silence['policy'] == stem_silence.POLICY and silence['silent_fraction'] == 1.0
    assert silence['threshold_db'] == -50 and silence['longest_silent_run_ms'] == 6000
    assert 'peak_relative_rms_db' in silence and silence['source_audio'] == 'v32-direct-input.wav'
    assert [e['start_ms'] for e in supplied['balanced--expert']] == [1000.]  # only the generated request
    assert len(errors) == 1 and errors[0]['severity'] == 'warning' and errors[0]['non_fatal'] is True
    assert errors[0]['request_key'] == KEY1 and errors[0]['kind'] == 'empty_silent_stem'


def test_the_same_error_on_a_core_with_activity_still_fails_the_variant(infer_case):
    _supplied, records, errors, failed, _meta = run_infer(infer_case, errors={KEY0: RECOVERY_FAILED})
    assert failed == {'balanced--expert'}
    row = next(r for r in records if r['key'] == KEY0)
    assert row['status'] == 'failed' and row['error'] == RECOVERY_FAILED
    assert errors[0]['error'] == RECOVERY_FAILED and 'severity' not in errors[0]


def test_half_silent_core_is_the_threshold_and_less_than_half_fails(infer_case):
    state = infer_case[3]
    # silence starts at 4 s: a 2-8 s core is 67% silent, 0-8 s exactly 50%, 0-7 s 43%
    for a, b, expected_failed in ((2, 8, False), (0, 8, False), (0, 7, True)):
        state['requests'] = [request(KEY0, a, b)]
        _s, _records, _e, failed, _m = run_infer(infer_case, errors={KEY0: RECOVERY_FAILED})
        assert (failed == {'balanced--expert'}) is expected_failed, (a, b)


@pytest.mark.parametrize('role', [None, 'mix', 'original'])
def test_mix_and_original_input_never_get_the_empty_contribution(infer_case, role):
    _s, records, errors, failed, _m = run_infer(infer_case, role)
    assert failed == {'balanced--expert'}
    assert next(r for r in records if r['key'] == KEY1)['status'] == 'failed'
    assert not any(e.get('severity') for e in errors)


@pytest.mark.parametrize('message', ['模型未返回该独立请求的谱面', 'V32 没有生成有效节拍', RECOVERY_FAILED + '（附加）',
                                     'upstream: No timing points found in beatmap.', 'CUDA out of memory'])
def test_other_errors_on_a_silent_core_are_unaffected(infer_case, message):
    _s, records, errors, failed, _m = run_infer(infer_case, errors={KEY1: message})
    assert failed == {'balanced--expert'}
    assert next(r for r in records if r['key'] == KEY1)['status'] == 'failed'
    assert errors[0]['error'] == message


def test_empty_silent_variant_yields_a_stem_raw_row_and_separate_counters(infer_case):
    tmp_path, source, snapshot, state = infer_case
    state['errors'] = {KEY1: RECOVERY_FAILED}
    snapshot = copy.deepcopy(snapshot)
    snapshot.update(_stem_raw_only=True, settings={'seed': 1, 'nps_ranges': {'expert': {'min': 1, 'max': 9}}})
    result = direct_v32.advanced_run(source, tmp_path / 'adv', {'_advanced': snapshot}, lambda *_: None)
    [row] = result['advanced_result']
    assert row['kind'] == 'stem_raw' and len(row['events']) == 1
    assert [c['status'] for c in row['provenance']['core_conditions']] == ['generated', 'empty_silent_stem']
    assert result['errors'] == []
    [warning] = result['warnings']
    assert warning['non_fatal'] and warning['kind'] == 'empty_silent_stem'
    summary = json.loads((tmp_path / 'adv' / 'direct-result-summary.json').read_text(encoding='utf-8'))
    assert (summary['model_requests'], summary['generated_requests'], summary['empty_silent_requests'],
            summary['failed_requests']) == (2, 1, 1, 0)
    assert summary['errors'] == [] and len(summary['warnings']) == 1


def test_an_all_empty_silent_stem_still_returns_an_empty_stem_raw(infer_case):
    tmp_path, source, snapshot, state = infer_case
    state['requests'] = [request(KEY0, 5, 10)]
    state['errors'] = {KEY0: RECOVERY_FAILED}
    snapshot = copy.deepcopy(snapshot)
    snapshot.update(_stem_raw_only=True, settings={'seed': 1, 'nps_ranges': {'expert': {'min': 1, 'max': 9}}})
    result = direct_v32.advanced_run(source, tmp_path / 'adv2', {'_advanced': snapshot}, lambda *_: None)
    [row] = result['advanced_result']
    assert row['kind'] == 'stem_raw' and row['events'] == []


def test_a_real_failure_keeps_the_job_error_text_and_counts_it_separately(infer_case):
    tmp_path, source, snapshot, state = infer_case
    state['errors'] = {KEY0: RECOVERY_FAILED, KEY1: RECOVERY_FAILED}
    snapshot = copy.deepcopy(snapshot)
    snapshot.update(_stem_raw_only=True, settings={'seed': 1, 'nps_ranges': {'expert': {'min': 1, 'max': 9}}})
    with pytest.raises(ValueError, match='0/2 个请求已生成'):
        direct_v32.advanced_run(source, tmp_path / 'adv3', {'_advanced': snapshot}, lambda *_: None)


def test_reusable_cache_never_treats_an_empty_silent_request_as_generated(tmp_path, monkeypatch):
    parent = 'a' * 32
    folder = tmp_path / 'outputs' / parent
    folder.mkdir(parents=True)
    expected = [dict(key='k', variant='v', core=[0, 10])]
    settings, policy = {'seed': 1}, {'version': 'x'}
    cache = dict(policy=policy, source_pcm_sha256='s', failed_variants=[], errors=[], raw={'v': []},
                 requests=[dict(expected[0], status='empty_silent_stem', owned_heads=0)])
    (folder / 'direct-generation-cache.json').write_text(json.dumps(cache), encoding='utf-8')
    (folder / 'queue-worker.json').write_text(json.dumps(dict(options=dict(_advanced=dict(settings=settings)))), encoding='utf-8')
    monkeypatch.setattr(direct_v32, 'ROOT', tmp_path)
    snapshot = dict(retry_of=parent, settings=settings, direct_v32_policy=policy, variants=[dict(key='v')])
    assert direct_v32.reusable_cache(snapshot, expected, 's') is None
    cache['requests'][0]['status'] = 'generated'
    (folder / 'direct-generation-cache.json').write_text(json.dumps(cache), encoding='utf-8')
    assert direct_v32.reusable_cache(snapshot, expected, 's') is not None


# ---- B: notes inside a stem's own long silence are dropped at fusion -------------------------------------

from malody_studio import stem_merge  # noqa: E402


def ev(role, name, start, lane, end=None):
    return {'id': f'{role[0]}-{name}', 'start_ms': float(start), 'lane': lane, 'end_ms': None if end is None else float(end),
            'origins': [{'revision_id': role, 'note_id': name, 'stem_role': role, 'original_start_ms': float(start),
                         'original_lane': lane, 'original_end_ms': None if end is None else float(end)}]}


RUN = {'start_ms': 10000, 'end_ms': 20000, 'declared_start_ms': 10500, 'declared_end_ms': 19500, 'level_db': -80.}


def merged(vocals, accompaniment, spans, mode='vocals_priority', primary='vocals'):
    result = stem_merge.merge_stems(vocals, accompaniment, mode=mode, primary=primary, gap_ms=60., min_hold_ms=120.,
                                    silent_spans=spans)
    assert stem_merge.audit(vocals, accompaniment, result)
    return result


def test_only_heads_strictly_inside_the_guarded_run_are_dropped_and_each_is_recorded():
    vocals = [ev('vocals', 'before', 9000, 0), ev('vocals', 'edge-start', 10500, 1), ev('vocals', 'in1', 10501, 1),
              ev('vocals', 'in2', 15000, 2, 16000), ev('vocals', 'edge-end', 19500, 3), ev('vocals', 'after', 19501, 3),
              ev('vocals', 'guard', 10200, 0)]
    accompaniment = [ev('accompaniment', 'a-in', 15000, 0), ev('accompaniment', 'a-out', 21000, 0)]
    result = merged(vocals, accompaniment, {'vocals': [RUN]})
    dropped = {d['event_id']: d for d in result['decisions'] if d['reason'] == 'stem_silent_span'}
    assert set(dropped) == {'v-in1', 'v-in2'}
    assert all(d['action'] == 'dropped' and d['role'] == 'vocals' and d['silent_run']['declared_start_ms'] == 10500
               and d['silent_run']['level_db'] == -80. for d in dropped.values())
    assert dropped['v-in2']['end_ms'] == 16000 and dropped['v-in2']['origin']['note_id'] == 'in2'
    kept = {e['id'] for e in result['events']}
    assert {'v-before', 'v-edge-start', 'v-edge-end', 'v-after', 'v-guard', 'a-a-in', 'a-a-out'} <= kept
    # the accompaniment note at 15 s is in the *vocal* silence only: untouched
    counts = result['counts']
    assert counts['vocals'] == {'input': 7, 'kept': 5, 'dropped': 2, 'relaned': 0, 'shortened': 0, 'to_tap': 0, 'stem_silent': 2}
    assert 'stem_silent' not in counts['accompaniment'] and counts['accompaniment']['kept'] == 2


def test_a_hold_whose_head_is_in_active_audio_is_never_touched_even_if_its_tail_enters_the_run():
    hold = ev('vocals', 'long', 9000, 1, 12000)  # head active, tail inside the silent run
    result = merged([hold], [], {'vocals': [RUN]})
    [kept] = result['events']
    assert kept['end_ms'] == 12000. and not result['decisions']


@pytest.mark.parametrize('mode,primary', [('vocals_priority', 'vocals'), ('accompaniment_priority', 'vocals'),
                                          ('relane', 'accompaniment')])
def test_each_stem_uses_its_own_runs_in_every_mode_and_accounting_holds(mode, primary):
    vocals = [ev('vocals', str(i), 11000 + i * 700, i % 4) for i in range(10)] + [ev('vocals', 'x', 25000, 0)]
    accompaniment = [ev('accompaniment', str(i), 11000 + i * 700, (i + 1) % 4) for i in range(10)]
    acc_run = dict(RUN, declared_start_ms=12000, declared_end_ms=13000)
    result = merged(vocals, accompaniment, {'vocals': [RUN], 'accompaniment': [acc_run]}, mode, primary)
    counts = result['counts']
    assert counts['vocals']['stem_silent'] == 10 and counts['vocals']['dropped'] == 10 and counts['vocals']['kept'] == 1
    assert counts['accompaniment']['stem_silent'] == 1  # only the 12 400 note
    for role in stem_merge.ROLES:
        assert counts[role]['kept'] + counts[role]['dropped'] == counts[role]['input']
    # the primary may lose notes only through the silence rule or its own recorded conflicts
    assert all(d['reason'] == 'stem_silent_span' for d in result['decisions'] if d['role'] == primary)


def test_all_notes_of_a_silent_stem_can_be_dropped_and_the_other_stem_survives():
    vocals = [ev('vocals', str(i), 11000 + i * 500, 0) for i in range(5)]
    accompaniment = [ev('accompaniment', 'k', 11000, 0)]
    result = merged(vocals, accompaniment, {'vocals': [RUN]})
    assert [e['id'] for e in result['events']] == ['a-k'] and result['counts']['vocals']['kept'] == 0


def test_without_silent_spans_the_merge_is_unchanged_and_counts_keep_their_old_shape():
    vocals = [ev('vocals', 'a', 15000, 0)]
    plain = stem_merge.merge_stems(vocals, [], mode='relane', gap_ms=60., min_hold_ms=120.)
    explicit_none = stem_merge.merge_stems(vocals, [], mode='relane', gap_ms=60., min_hold_ms=120., silent_spans=None)
    assert plain == explicit_none and 'stem_silent' not in plain['counts']['vocals'] and 'silent_spans' not in plain
    empty = stem_merge.merge_stems(vocals, [], mode='relane', gap_ms=60., min_hold_ms=120., silent_spans={})
    assert len(empty['events']) == 1 and 'stem_silent' not in empty['counts']['vocals']


def test_audit_catches_silent_drop_accounting_errors():
    vocals = [ev('vocals', 'a', 15000, 0), ev('vocals', 'b', 25000, 0)]
    good = stem_merge.merge_stems(vocals, [], mode='relane', gap_ms=60., min_hold_ms=120., silent_spans={'vocals': [RUN]})
    stem_merge.audit(vocals, [], good)
    for mutate in (lambda r: r['counts']['vocals'].__setitem__('stem_silent', 0),
                   lambda r: r['decisions'].clear(),
                   lambda r: r['events'].append(copy.deepcopy(vocals[0]))):
        broken = copy.deepcopy(good)
        mutate(broken)
        with pytest.raises(ValueError):
            stem_merge.audit(vocals, [], broken)


def test_invalid_silent_span_declarations_are_rejected():
    for bad in ({'guitar': [RUN]}, {'vocals': [{'declared_start_ms': 5, 'declared_end_ms': 5}]}, [RUN]):
        with pytest.raises(ValueError):
            stem_merge.merge_stems([], [], mode='relane', silent_spans=bad)
