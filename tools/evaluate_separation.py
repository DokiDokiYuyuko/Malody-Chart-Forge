"""Aligned listening clips and descriptive measurements, never blind quality scores."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from malody_studio.separation import SR, validate_manifest

BANDS = [(20, 200), (200, 1000), (1000, 4000), (4000, 8000), (8000, 16000)]


def measure(data):
    data = np.asarray(data, dtype=np.float64)
    rms = float(np.sqrt(np.mean(data ** 2)))
    window = np.hanning(len(data))[:, None]
    power = np.abs(np.fft.rfft(data * window, axis=0)) ** 2
    frequency = np.fft.rfftfreq(len(data), 1 / SR)
    energies = [float(power[(frequency >= lo) & (frequency < hi)].sum()) for lo, hi in BANDS]
    total = max(1e-30, sum(energies))
    return {'rms': rms, 'peak': float(np.abs(data).max(initial=0)),
            'bands': {f'{lo}-{hi}': {'energy': e, 'fraction': e / total}
                      for (lo, hi), e in zip(BANDS, energies)}}


def selected_manifests(project):
    seen = set()
    found = {}
    paths = list((ROOT/'outputs'/'advanced'/project['id']/'stems').glob('*/manifest.json'))
    paths += list((ROOT/'cache'/'separation').glob('*/manifest.json'))
    for path in paths:
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('id') in seen or record.get('source_pcm_sha') != project['source_pcm_sha256']:
            continue
        seen.add(record.get('id'))
        if record.get('scope') == 'preview_only' or record.get('frame_count') != project['samples']:
            continue
        settings = record.get('settings', {})
        model = settings.get('model')
        label = {'htdemucs': 'Demucs-standard', 'htdemucs_ft': 'Demucs-FT'}.get(model)
        if model == 'melband_roformer_kim':
            label = 'Kim-cover-' + str(settings.get('overlap_count'))
        if not label or label in found:
            continue
        found[label] = validate_manifest(path.parent, record)
    required = {'Demucs-standard', 'Demucs-FT', 'Kim-cover-2', 'Kim-cover-4'}
    if required - found.keys():
        raise RuntimeError('缺少同源完整分离对照：' + ', '.join(sorted(required - found.keys())))
    return found


def export(project_id, ranges, output):
    directory = ROOT/'outputs'/'advanced'/project_id
    project = json.loads((directory/'project.json').read_text(encoding='utf-8'))
    mix, rate = sf.read(directory/'source.wav', dtype='float32', always_2d=True)
    if rate != SR or len(mix) != project['samples']:
        raise ValueError('原曲时钟与项目不符')
    parent_hash = hashlib.sha256(np.asarray(mix, dtype='<f4').tobytes()).hexdigest()
    if parent_hash != project['source_pcm_sha256']:
        raise ValueError('原曲 PCM 哈希与项目不符')
    manifests = selected_manifests(project)
    output.mkdir(parents=True, exist_ok=True)
    audio = {'Original': {'mix': mix}}
    for label, manifest in manifests.items():
        audio[label] = {row['role']: sf.read(row['path'], dtype='float32', always_2d=True)[0]
                        for row in manifest['stems']}
    report = {'project': project_id, 'title': project['title'], 'source_pcm_sha256': parent_hash,
              'sample_rate': SR, 'frames': len(mix), 'versions': manifests, 'windows': [],
              'interpretation': 'No isolated reference stems: these measurements are not SDR, leakage rate or quality scores. '
                                'Listening and chart alignment must be judged separately. RMS match is optional audition gain only.'}
    lines = [f'# {project["title"]}：同源分离对照', '',
             '四个分离结果均使用旧项目的同一份不可变原曲 PCM。以下数字描述声音变化，不代表质量分。', '',
             '试听请比较串音、人声完整度、和声、鼓点和金属感。所有文件保持相同起止采样，默认采用共同防削波增益；原始结果未改写。', '']
    for a, b in ranges:
        start, end = round(a*SR), round(b*SR)
        if not (0 <= start < end <= len(mix)):
            raise ValueError('对照片段越过完整原曲')
        window_id = f'{a:g}-{b:g}s'
        clip_directory = output/window_id
        clip_directory.mkdir(exist_ok=True)
        slices = {label: {role: data[start:end] for role, data in roles.items()} for label, roles in audio.items()}
        gain = min(1., .96 / max(float(np.abs(data).max(initial=0)) for roles in slices.values() for data in roles.values()))
        reference = measure(mix[start:end])
        row = {'range_samples': [start, end], 'range_seconds': [a, b], 'common_preview_gain': gain,
               'original': reference, 'versions': {}}
        lines += [f'## {window_id}', '', '| 版本 | 人声 RMS | 伴奏 RMS | 伴奏 200 Hz 以下能量占比 | 重组相对 RMS |',
                  '|---|---:|---:|---:|---:|']
        for label, roles in slices.items():
            measurements = {role: measure(data) for role, data in roles.items()}
            files = {}
            for role, data in roles.items():
                path = clip_directory/(label+'-'+role+'.wav')
                sf.write(path, data*gain, SR, subtype='FLOAT')
                files[role] = str(path.resolve())
            if label != 'Original':
                residual = mix[start:end].astype(np.float64)-roles['vocals']-roles['accompaniment']
                relative = float(np.sqrt(np.mean(residual**2)) / max(1e-30, reference['rms']))
                values = measurements['accompaniment']['bands']
                lines.append(f'| {label} | {measurements["vocals"]["rms"]:.4f} | {measurements["accompaniment"]["rms"]:.4f} | '
                             f'{values["20-200"]["fraction"]*100:.1f}% | {relative*100:.4f}% |')
                measurements['reconstruction_relative_rms'] = relative
            row['versions'][label] = {'metrics': measurements, 'files': files}
        lines += ['', '重组误差仅检查运算与输出尺度。Kim 伴奏采用原曲减人声，因此该项接近零是构造结果，不能据此证明分离更干净。', '',
                  '人声 / 伴奏听感与制谱采音评价：待人工 A/B。', '']
        report['windows'].append(row)
    (output/'comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (output/'comparison.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--ranges', nargs='+', required=True, help='start:end seconds')
    parser.add_argument('--output', type=Path, required=True)
    arguments = parser.parse_args()
    result = export(arguments.project, [tuple(map(float, value.split(':'))) for value in arguments.ranges], arguments.output)
    print(json.dumps({'title': result['title'], 'windows': len(result['windows']), 'output': str(arguments.output)}, ensure_ascii=False))
