"""Reuse verified, immutable full-song rhythm analysis for API submissions."""
import threading

from .advanced import SR, atomic, read
from .separation import canonical_hash
from . import section_plan

_locks = {}
_guard = threading.Lock()


def get_or_build_plan(store, project, settings):
    # Serialize only analysis of this project, without blocking edits or other
    # projects behind a global store lock. Concurrent requests share the result.
    with _guard:
        lock = _locks.setdefault(str(store.directory(project['id']).resolve()), threading.Lock())
    with lock:
        return _get_or_build_plan(store, project, settings)


def _get_or_build_plan(store, project, settings):
    directory = store.directory(project['id'])
    source = project.get('source_pcm_sha256')
    settings_hash = section_plan.plan_settings_hash(settings)
    tempo = project.get('tempo', {})
    reference_hash = canonical_hash(tempo)
    folder = directory / 'section-plans'
    # A fusion retuning is a different artifact, even if its audio evidence is
    # copied from the requested generation plan. Never treat it as that plan.
    for path in sorted(folder.glob('*.json')):
        try:
            plan = read(path)
            if (not source or plan.get('fusion_only') or plan.get('version') != section_plan.VERSION
                    or plan.get('source_pcm_sha') != source or plan.get('samples') != project['samples']
                    or plan.get('sample_rate') != SR or plan.get('settings_hash') != settings_hash
                    or plan.get('reference_hash') != reference_hash):
                continue
            section_plan.validate_plan(plan, source)
            return plan
        except (ValueError, KeyError, TypeError, OSError):
            # An invalid artifact must not become generation input or prevent
            # rebuilding from the immutable original source.
            continue
    plan = section_plan.build_plan(directory / 'source.wav', settings, tempo)
    section_plan.validate_plan(plan, source)
    if plan['samples'] != project['samples']:
        raise ValueError('原曲采样数量与项目不一致，请检查完整音源')
    atomic(folder / (plan['id'] + '.json'), plan)
    return plan
