import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from malody_studio.v32_attempt_diagnostics import generate_with_diagnostics


def context(tmp_path):
    audio = tmp_path / 'input.wav'
    audio.write_bytes(b'frozen input')
    output = tmp_path / 'expert__group-0'
    output.mkdir()
    args = SimpleNamespace(audio_path=str(audio), output_path=str(output), seed=13,
                           difficulty=5.9, in_context=[], max_batch_size=32)
    return output, args, SimpleNamespace(Processor=type('Processor', (), {}))


def call(generate, module, args, enabled=True):
    return generate_with_diagnostics(generate, module, args,
        generation_config=SimpleNamespace(difficulty=args.difficulty),
        beatmap_config=SimpleNamespace(mode=3), request={'capture_native_trace': enabled},
        reference_policy={'source': 'none'}, model='unchanged-model')


def test_every_call_preserves_actual_parameters_artifacts_and_result(tmp_path):
    output, args, module = context(tmp_path)
    legacy = output / 'resolved-parameters.json'
    legacy.write_bytes(b'original snapshot')
    original_class = module.Processor
    values = []

    def generate(actual, **kwargs):
        assert actual is args and kwargs['model'] == 'unchanged-model'
        value = f'{actual.seed}:{actual.difficulty}:{actual.in_context}:{actual.max_batch_size}'
        (output / 'model-events.json').write_text(value, encoding='utf-8')
        path = output / 'chart.osu'
        path.write_text(value, encoding='utf-8')
        result = (object(), path)
        values.append((value, result))
        return result

    assert call(generate, module, args) is values[0][1]
    args.seed, args.difficulty, args.in_context, args.max_batch_size = 29, 10., ['timing'], 16
    assert call(generate, module, args) is values[1][1]
    assert module.Processor is original_class and legacy.read_bytes() == b'original snapshot'
    assert args.output_path == str(output)
    for ordinal, (value, _) in enumerate(values, 1):
        folder = output / 'inference-attempts' / f'{ordinal:04d}'
        snapshot = json.loads((folder / 'resolved-parameters.json').read_text(encoding='utf-8'))
        outcome = json.loads((folder / 'attempt-result.json').read_text(encoding='utf-8'))
        assert snapshot['args']['seed'] == (13 if ordinal == 1 else 29)
        assert snapshot['args']['in_context'] == ([] if ordinal == 1 else ['timing'])
        assert snapshot['args']['max_batch_size'] == (32 if ordinal == 1 else 16)
        assert snapshot['generation_config']['difficulty'] == (5.9 if ordinal == 1 else 10.)
        assert snapshot['resolved_audio_identity']['sha256'] == hashlib.sha256(b'frozen input').hexdigest()
        assert (folder / 'model-events.json').read_text(encoding='utf-8') == value
        assert (folder / 'returned-chart.osu').read_text(encoding='utf-8') == value
        assert outcome['status'] == 'completed' and not outcome['diagnostic_errors']
        trace = folder / 'v32-generation-diagnostics.json'
        trace_artifact = next(a for a in outcome['artifacts'] if a.get('kind') == 'native_trace')
        assert trace_artifact['archived_sha256'] == hashlib.sha256(trace.read_bytes()).hexdigest()


def test_failure_keeps_partial_evidence_and_does_not_attribute_stale_sidecars(tmp_path):
    output, args, module = context(tmp_path)
    failure = ValueError('missing LN position')

    def failed(actual, **kwargs):
        (output / 'model-events.json').write_text('native failure', encoding='utf-8')
        (output / 'serialization-error.json').write_text('missing position', encoding='utf-8')
        raise failure

    with pytest.raises(ValueError) as caught:
        call(failed, module, args)
    assert caught.value is failure
    first = output / 'inference-attempts' / '0001'
    result = json.loads((first / 'attempt-result.json').read_text(encoding='utf-8'))
    assert result['status'] == 'failed' and result['error'] == str(failure)
    assert (first / 'model-events.json').read_text(encoding='utf-8') == 'native failure'
    returned = object()
    assert call(lambda *a, **k: returned, module, args) is returned
    second = output / 'inference-attempts' / '0002'
    result = json.loads((second / 'attempt-result.json').read_text(encoding='utf-8'))
    assert not any(a['updated_by_this_attempt'] for a in result['artifacts'] if a.get('kind') != 'native_trace')
    assert len([a for a in result['artifacts'] if a.get('kind') == 'native_trace']) == 1
    assert not (second / 'serialization-error.json').exists()


def test_simple_request_is_noop_and_preserves_exception_identity(tmp_path):
    output, args, module = context(tmp_path)
    failure = RuntimeError('upstream failure')
    original_class = module.Processor

    def generate(actual, **kwargs):
        assert actual is args and kwargs['generation_config'].difficulty == 5.9
        raise failure

    with pytest.raises(RuntimeError) as caught:
        call(generate, module, args, enabled=False)
    assert caught.value is failure and module.Processor is original_class
    assert list(output.iterdir()) == []


def test_diagnostic_disk_failure_does_not_change_generation(tmp_path, monkeypatch, capsys):
    from malody_studio import v32_attempt_diagnostics as diagnostics
    output, args, module = context(tmp_path)

    def reject(*args):
        raise OSError('disk write refused')

    monkeypatch.setattr(diagnostics, '_write', reject)
    returned = object()
    assert call(lambda *a, **k: returned, module, args) is returned
    assert 'disk write refused' in capsys.readouterr().err
