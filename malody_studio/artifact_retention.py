"""Production artifact lifetime: original music and deliverables outlive scratch files."""
from __future__ import annotations
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

VERSION = 1
TASK_JSON = {'job.json', 'queue-worker.json', 'report.json', 'worker-failure.json',
             'tail-analysis.json', 'generation-cache.json', 'storage-retention.json',
             'resolved-parameters.json', 'attempt-result.json'}
DIAGNOSTIC_DIRS = {'event-evidence', 'acoustic-evidence', 'debug-log', 'model-diagnostics'}

def retain_debug(options):
    settings = (options.get('_advanced') or {}).get('settings') or {}
    value = options.get('retain_debug_artifacts', settings.get('retain_debug_artifacts', False))
    if type(value) is not bool:
        raise ValueError('保留调试文件须为布尔值')
    return value

def project_debug(directory):
    path = Path(directory) / 'project.json'
    if not path.is_file(): return False
    return retain_debug({'_advanced': {'settings': json.loads(path.read_text(encoding='utf-8')).get('settings', {})}})

def cleanup(directory, *, debug=False, project=False, keep_audio=()):
    """Only walk the owned directory, never follow links or edit structural revisions."""
    root = Path(directory).resolve()
    result = {'policy_version': VERSION, 'mode': 'debug' if debug else 'compact',
              'removed_files': 0, 'removed_bytes': 0, 'errors': []}
    if debug or not root.is_dir(): return result
    kept = {Path(p).resolve() for p in keep_audio}
    if project:
        kept.add(root / 'source.wav')
        kept.update(p.resolve() for p in root.glob('source-original.*'))
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if not Path(current, name).is_symlink()
                          and not getattr(Path(current, name).lstat(), 'st_file_attributes', 0) & 0x400]
        for name in filenames:
            path = Path(current, name)
            relative = path.relative_to(root)
            if path.is_symlink() or not path.resolve().is_relative_to(root) or path.resolve() in kept: continue
            suffix = path.suffix.lower()
            diagnostic = any(part in DIAGNOSTIC_DIRS for part in relative.parts[:-1])
            remove = suffix == '.wav' or (diagnostic and suffix in ('.json', '.jsonl', '.npz', '.npy'))
            if not project and suffix in ('.json', '.jsonl', '.npz', '.npy'):
                # Chart version manifests and reusable mother-chart facts remain functional data.
                remove = remove or (name not in TASK_JSON and 'versions' not in relative.parts
                                    and 'chart-versions' not in relative.parts)
            if not remove: continue
            try:
                size = path.stat().st_size
                path.unlink()
                result['removed_files'] += 1; result['removed_bytes'] += size
            except OSError as exc:
                if len(result['errors']) < 20: result['errors'].append(str(exc))
    return result

def restore_inputs(root, options, directory, progress):
    """Recreate pruned stems inside the existing queued worker, never on a GET."""
    snapshot = options.get('_advanced') or {}
    pid = snapshot.get('project', {}).get('id')
    if not pid: return
    from .advanced import identifier, read
    from .separation import ensure_stems, validate_manifest
    from .stem_generation import materialize_stems
    project = Path(root) / 'outputs' / 'advanced' / identifier(pid)
    descriptors = list(snapshot.get('input_sources', []))
    if snapshot.get('source'): descriptors.append(snapshot['source'])
    ids = {row['stem_set_id'] for row in descriptors if row.get('stem_set_id')}
    if snapshot.get('stem_set_id'): ids.add(snapshot['stem_set_id'])
    for stem_id in ids:
        if len(stem_id) != 64 or any(c not in '0123456789abcdef' for c in stem_id):
            raise ValueError('分离版本 ID 无效')
        target = project / 'stems' / stem_id
        manifest = read(target / 'manifest.json')
        if not manifest.get('storage', {}).get('audio_pruned'): continue
        progress('中间音轨已清理，正在按保存参数从原曲重新分离', 2)
        restored = ensure_stems(project / 'source.wav', directory, manifest['settings'], progress)
        if restored['id'] != stem_id or restored['recipe_hash'] != manifest['recipe_hash']:
            raise ValueError('分离模型或配方已变化，请重新分离并提交新任务')
        restored = materialize_stems(project, restored)
        validate_manifest(target, restored, manifest['recipe_hash'])
        # Frozen sources must still identify the exact PCM consumed by this retry.
        for old in descriptors:
            if old.get('stem_set_id') != stem_id: continue
            match = next((row for row in restored['stems'] if row['source_id'] == old['source_id']), None)
            if not match or any(old.get(key) != match.get(key) for key in ('pcm_sha', 'file_sha256')):
                raise ValueError('重建声部与冻结音源不一致，请提交新候选，不可替换旧任务来源')

def prune_project(project, root, *, debug=False, prune_stems=False):
    project, root = Path(project).resolve(), Path(root).resolve()
    if not project.is_relative_to(root / 'outputs' / 'advanced'):
        raise ValueError('项目清理路径无效')
    if (root/'logs'/'artifact-cleanup-approval.pending.json').is_file():
        return {'policy_version':VERSION,'mode':'awaiting_legacy_cleanup_approval',
                'removed_files':0,'removed_bytes':0,'errors':[]}
    keep = []
    if not prune_stems:
        # A separation-only job has not consumed these inputs yet.
        keep = list((project / 'stems').glob('**/*.wav'))
    result = cleanup(project, debug=debug, project=True, keep_audio=keep)
    if debug or not prune_stems: return result
    from .advanced import read, atomic
    for path in (project / 'stems').glob('*/manifest.json'):
        if not path.resolve().is_relative_to(project): continue
        manifest = read(path)
        if not manifest.get('settings') or manifest.get('scope') == 'preview_only': continue
        manifest['storage'] = {'audio_pruned': True, 'rebuild_from_original': True, 'policy_version': VERSION}
        atomic(path, manifest)
        key = manifest.get('recipe_hash', '')
        if len(key) == 64 and all(c in '0123456789abcdef' for c in key):
            cache = root / 'cache' / 'separation' / key
            if cache.is_dir() and cache.resolve().is_relative_to(root / 'cache' / 'separation'):
                saved = cleanup(cache)
                result['removed_files'] += saved['removed_files']; result['removed_bytes'] += saved['removed_bytes']
                result['errors'] = (result['errors'] + saved['errors'])[:20]
    return result

def ephemeral_audio(path):
    """Playback scratch paths are uniquely named and removed after streaming closes."""
    path=Path(path)
    return path.parent.name in ('preview','preview-audio') and '.transient-' in path.name and path.suffix.lower() == '.wav'

def remove_ephemeral(path):
    if ephemeral_audio(path):
        try: Path(path).unlink(missing_ok=True)
        except OSError: pass

@contextmanager
def working_original(directory, source, *, debug=False):
    """Recreate original PCM only for a synchronous single-chart rule revision."""
    directory=Path(directory); target=directory/'source.wav'
    try:
        if not target.is_file():
            from .audio import ffmpeg
            temporary=directory/'source.transient-restore.wav'
            result=subprocess.run([ffmpeg(),'-hide_banner','-loglevel','error','-y','-i',str(source),
                                   '-vn','-ar','44100','-c:a','pcm_f32le',str(temporary)],
                                  capture_output=True,text=True,timeout=120)
            if result.returncode:raise ValueError('从原曲恢复临时音频失败：'+result.stderr[-300:])
            temporary.replace(target)
        yield target
    finally:
        cleanup(directory,debug=debug)
