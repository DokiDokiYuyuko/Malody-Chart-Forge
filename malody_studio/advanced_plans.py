"""Reuse verified, immutable full-song rhythm analysis for API submissions."""
import copy
import threading

from .advanced import SR, atomic, read
from .separation import canonical_hash
from . import section_plan, music_timing, arrangement, bpm_buckets
from .section_plan import plan_settings_hash as section_plan_settings_hash

PLANNER = 'beat-this-buckets-v1'


def default_analysis_policy():
    policy=music_timing.selected_policy()
    if not policy.get('adapter','').startswith('beat_this'):
        raise RuntimeError('高级台需要已部署的 Beat This 分析配置；不会退回旧 BPM 估计')
    return policy


def current_plan(plan, policy=None):
    policy=default_analysis_policy() if policy is None else policy
    return (plan.get('planner')==PLANNER and
            plan.get('detector_execution')=='beat-this-primary-v1' and
            plan.get('bpm_buckets',{}).get('version') in bpm_buckets.KNOWN_VERSIONS and
            plan.get('analysis_policy_hash')==canonical_hash(policy))

def frozen_plan_needs_rebuild(section_plan, settings):
    """True only when a frozen retry plan is not a usable Beat This bucket plan.

    A plan frozen with any KNOWN estimator version (v2 or v3) is accepted as it
    is: a retry or reload never silently re-buckets a historical job just because
    the current default estimator moved on.
    """
    return (section_plan.get('planner')!=PLANNER or
            section_plan.get('bpm_buckets',{}).get('version') not in bpm_buckets.KNOWN_VERSIONS or
            section_plan.get('settings_hash')!=section_plan_settings_hash(settings))

_locks = {}
_guard = threading.Lock()


def get_or_build_plan(store, project, settings, bucket_version=None):
    # Serialize only analysis of this project, without blocking edits or other
    # projects behind a global store lock. Concurrent requests share the result.
    with _guard:
        lock = _locks.setdefault(str(store.directory(project['id']).resolve()), threading.Lock())
    with lock:
        return _get_or_build_plan(store, project, settings, bucket_version or bpm_buckets.VERSION)


def _get_or_build_plan(store, project, settings, bucket_version):
    directory = store.directory(project['id'])
    source = project.get('source_pcm_sha256')
    settings_hash = section_plan.plan_settings_hash(settings)
    planner_options_hash=canonical_hash({**{key:settings.get(key) for key in ('region_granularity','region_max_count')},
                                         'bpm_bucket_version':bucket_version})
    tempo = project.get('tempo', {})
    reference_hash = canonical_hash(tempo)
    policy=default_analysis_policy()
    folder = directory / 'section-plans'
    # A fusion retuning is a different artifact, even if its audio evidence is
    # copied from the requested generation plan. Never treat it as that plan.
    for path in sorted(folder.glob('*.json')):
        try:
            plan = read(path)
            if (not source or not current_plan(plan,policy) or plan.get('fusion_only') or plan.get('version') != section_plan.VERSION
                    or plan.get('source_pcm_sha') != source or plan.get('samples') != project['samples']
                    or plan.get('sample_rate') != SR or plan.get('settings_hash') != settings_hash
                    or plan.get('planner_options_hash')!=planner_options_hash
                    or plan.get('reference_hash') != reference_hash):
                continue
            section_plan.validate_plan(plan, source)
            return plan
        except (ValueError, KeyError, TypeError, OSError):
            # An invalid artifact must not become generation input or prevent
            # rebuilding from the immutable original source.
            continue
    evidence=music_timing.load_or_analyze(directory,project,frozen_policy=policy)
    timing=music_timing.select_timing(evidence)
    if timing['provenance'].get('adapter')!=policy['adapter']:
        raise RuntimeError('Beat This 分析结果缺失；不会用旧 BPM 估计代替')
    # A new analysis uses the current estimator; the version is then frozen in
    # the plan, and planner_options_hash keeps older plans from being reused.
    result=arrangement.build(evidence,timing,evidence['acoustic'],settings,
                             project.get('variants',[]),project.get('profile','keyboard'),
                             bucket_version=bucket_version)
    plan={key:copy.deepcopy(value) for key,value in result['section_plan'].items()
          if key not in ('id','content_hash')}
    plan.update(planner=PLANNER,analysis_adapter=policy['adapter'],
                detector_execution='beat-this-primary-v1',
                analysis_policy_hash=canonical_hash(policy),planner_options_hash=planner_options_hash,
                music_evidence_id=evidence['id'],
                timing_map_id=timing['id'],reference_hash=reference_hash,
                tempo_reference=copy.deepcopy(tempo))
    identity=section_plan.canonical_hash(plan)
    plan.update(id=identity,content_hash=identity)
    section_plan.validate_plan(plan, source)
    if plan['samples'] != project['samples']:
        raise ValueError('原曲采样数量与项目不一致，请检查完整音源')
    atomic(directory/'music-evidence'/(evidence['id']+'.json'),evidence)
    atomic(directory/'timing-maps'/(timing['id']+'.json'),timing)
    atomic(folder / (plan['id'] + '.json'), plan)
    return plan
