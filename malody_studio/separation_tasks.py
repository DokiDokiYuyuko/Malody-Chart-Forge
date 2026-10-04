"""Independent queue stage: full source separation, never chart inference."""
import copy
from pathlib import Path
from .advanced import SR, now, atomic
from .paths import ROOT
from .separation import ensure_stems, validated_settings, pcm_hash, validate_manifest
from .stem_generation import materialize_stems
import soundfile as sf


def present(manifest):
    result = copy.deepcopy(manifest)
    settings = result.get('settings', {})
    model = settings.get('model', 'htdemucs')
    result.setdefault('name', '人声与伴奏 · ' + model + ' · ' + result.get('created', '已缓存版本'))
    detail = (f"重叠窗口 {settings.get('overlap_count', 4)} 次" if model == 'melband_roformer_kim'
              else f"重叠 {settings.get('overlap', .25):.0%} · 平移 {settings.get('shifts', 1)} 次")
    result['summary'] = f"{model} · {detail} · 全曲 {result['frame_count'] / SR:.2f} 秒"
    return result


def run(source, directory, options, progress):
    snapshot = options['_advanced']; p = snapshot['project']
    project = ROOT / 'outputs' / 'advanced' / p['id']
    original = project / 'source.wav'
    if Path(source).resolve() != original.resolve():raise ValueError('分离须使用项目完整原曲')
    data, rate = sf.read(original, dtype='float32', always_2d=True)
    if rate != SR or len(data) != p['samples'] or pcm_hash(data) != p['source_pcm_sha256']:
        raise ValueError('分离原曲与提交快照不匹配')
    manifest = materialize_stems(project, ensure_stems(original, directory, snapshot['settings'], progress))
    manifest.setdefault('created', now())
    manifest.setdefault('settings', validated_settings(snapshot['settings']))
    manifest['name'] = '人声与伴奏 · ' + manifest['settings']['model'] + ' · ' + manifest['created']
    atomic(project / 'stems' / manifest['id'] / 'manifest.json', manifest)
    progress('人声和伴奏已保存，原曲时钟校验通过', 100)
    return {'stem_set_id': manifest['id'], 'stem_set': present(manifest)}
