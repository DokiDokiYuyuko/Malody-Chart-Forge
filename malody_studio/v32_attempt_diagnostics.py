"""Preserve each opt-in Advanced inference attempt without changing generation."""

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys

from .v32_generation_diagnostics import _plain, capture_processor_diagnostics


SCHEMA = 'v32-inference-attempt-v1'
SIDECARS = ('model-events.json', 'serialization-error.json', 'v32-event-audit.json', 'v32-grammar-mask.json')


def _identity(path):
    path = Path(path)
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return {'path': str(path.resolve()), 'sha256': digest.hexdigest(),
            'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def _write(path, value):
    # Every attempt has a newly allocated directory. Never overwrite evidence.
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(_plain(value), stream, ensure_ascii=False, indent=2)


def _config(value):
    return _plain(asdict(value) if is_dataclass(value) else vars(value))


def _new_attempt(output):
    parent = output / 'inference-attempts'
    parent.mkdir(parents=True, exist_ok=True)
    index = 1
    while True:
        folder = parent / f'{index:04d}'
        try:
            folder.mkdir()
        except FileExistsError:
            index += 1
        else:
            return index, folder


def generate_with_diagnostics(generate, inference_module, args, *, generation_config,
                              beatmap_config, request, reference_policy, **model_kwargs):
    """Call the original generator once, keeping its return/exception and inputs.

    The opt-in flag is supplied only by Advanced worker requests. A distinct
    directory records every real call, including calls made after an OOM or
    timing fallback and density retries. The original output path stays intact.
    """
    def invoke():
        return generate(args, generation_config=generation_config,
                        beatmap_config=beatmap_config, **model_kwargs)

    if request.get('capture_native_trace') is not True:
        return invoke()

    folder = None
    errors = []
    before = {}
    outcome = {'schema': SCHEMA, 'status': 'started'}
    output = Path(args.output_path)
    try:
        index, folder = _new_attempt(output)
        outcome.update(attempt=index, started_at=datetime.now(timezone.utc).isoformat(),
                       original_output_path=str(output))
        snapshot = {'args': _config(args), 'generation_config': _config(generation_config),
                    'beatmap_config': _config(beatmap_config), 'request': _plain(request),
                    'reference_experiment_only': bool(request.get('reference_experiment_only')),
                    'reference_conditioning_policy': _plain(reference_policy),
                    'inference_attempt': dict(outcome),
                    'resolved_audio_identity': _identity(args.audio_path)}
        _write(folder / 'resolved-parameters.json', snapshot)
        for name in SIDECARS:
            path = output / name
            before[name] = _identity(path) if path.is_file() else None
    except Exception as exc:
        errors.append({'stage': 'snapshot', 'type': type(exc).__name__, 'error': str(exc)})

    returned_path = None
    try:
        if folder is None:
            result = invoke()
        else:
            with capture_processor_diagnostics(inference_module, folder, enabled=True):
                result = invoke()
        outcome['status'] = 'completed'
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], (str, Path)):
            returned_path = Path(result[1])
        return result
    except BaseException as exc:
        outcome.update(status='failed', error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        if folder is not None:
            artifacts = []
            try:
                trace = folder / 'v32-generation-diagnostics.json'
                if trace.is_file():
                    identity = _identity(trace)
                    artifacts.append({**identity, 'archived_path': str(trace),
                                      'archived_sha256': identity['sha256'],
                                      'kind': 'native_trace', 'updated_by_this_attempt': True})
            except Exception as exc:
                errors.append({'stage': 'trace_identity', 'type': type(exc).__name__, 'error': str(exc)})
            for name in SIDECARS:
                try:
                    source = output / name
                    if not source.is_file():
                        continue
                    identity = _identity(source)
                    updated = before.get(name) != identity
                    artifact = {**identity, 'updated_by_this_attempt': updated}
                    if updated:
                        archived = folder / name
                        shutil.copyfile(source, archived)
                        artifact['archived_path'] = str(archived)
                        artifact['archived_sha256'] = _identity(archived)['sha256']
                    artifacts.append(artifact)
                except Exception as exc:
                    errors.append({'stage': 'archive_' + name, 'type': type(exc).__name__, 'error': str(exc)})
            if returned_path is not None:
                try:
                    identity = _identity(returned_path)
                    archived = folder / 'returned-chart.osu'
                    shutil.copyfile(returned_path, archived)
                    artifacts.append({**identity, 'archived_path': str(archived),
                                      'archived_sha256': _identity(archived)['sha256'],
                                      'kind': 'returned_chart'})
                except Exception as exc:
                    errors.append({'stage': 'archive_returned_chart', 'type': type(exc).__name__, 'error': str(exc)})
            outcome.update(finished_at=datetime.now(timezone.utc).isoformat(),
                           artifacts=artifacts, diagnostic_errors=errors)
            try:
                _write(folder / 'attempt-result.json', outcome)
            except Exception as exc:
                errors.append({'stage': 'attempt_result', 'type': type(exc).__name__, 'error': str(exc)})
        if errors:
            # Optional recording must not replace the model result or error.
            try:
                print('V32 attempt diagnostic error: ' + json.dumps(errors), file=sys.stderr)
            except Exception:
                pass
