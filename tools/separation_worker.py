"""Demucs runs only in its isolated environment; outputs preserve source frames."""
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import time

import numpy as np
import soundfile as sf
import torch
from demucs.apply import apply_model
from demucs.pretrained import get_model


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main(request_path):
    started = time.monotonic()
    request = json.loads(Path(request_path).read_text(encoding='utf-8'))
    directory = Path(request['directory'])
    data, rate = sf.read(request['source'], dtype='float32', always_2d=True)
    settings = request['settings']
    if rate != 44100 or data.shape != (request['frame_count'], 2) or not np.isfinite(data).all():
        raise ValueError('分离工作进程源 PCM 不匹配')
    if hashlib.sha256(np.asarray(data, dtype='<f4').tobytes()).hexdigest() != request['source_pcm_sha']:
        raise ValueError('分离工作进程源 PCM 哈希不匹配')
    if not torch.cuda.is_available():
        raise RuntimeError('CUDA 不可用；Demucs 已停止，请检查独立环境和显卡驱动后重试。')
    random.seed(settings['seed']); np.random.seed(settings['seed']); torch.manual_seed(settings['seed'])
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    torch.set_num_threads(4)
    model = get_model(settings['model'], repo=Path(request['model_root']))
    if model.samplerate != rate or model.audio_channels != 2:
        raise ValueError('分离模型采样时钟不匹配')
    model.eval()
    mix = torch.from_numpy(data.T.copy())
    ref = mix.mean(0)
    mean, std = ref.mean(), ref.std()
    # One mixture normalization; reverse identically for every model output.
    normalized = (mix - mean) / (std + 1e-8)
    if torch.cuda.is_available(): torch.cuda.reset_peak_memory_stats()
    with torch.inference_mode():
        estimate = apply_model(model, normalized[None], device='cuda',
                               shifts=settings['shifts'], split=True, overlap=settings['overlap'],
                               segment=settings['segment'], progress=True, num_workers=0)[0].cpu()
    estimate = estimate * (std + 1e-8) + mean
    if estimate.shape[-1] != len(data):
        raise ValueError('分离输出帧数与原曲不相等；禁止自动拉伸')
    names = list(model.sources)
    vocals = estimate[names.index('vocals')].numpy().T.copy()
    accompaniment = sum(estimate[names.index(name)] for name in ('drums', 'bass', 'other')).numpy().T.copy()
    rows = []
    for role, audio in (('vocals', vocals), ('accompaniment', accompaniment)):
        if audio.shape != data.shape or not np.isfinite(audio).all():
            raise ValueError('分离音源含无效值')
        target = directory / (role + '.wav')
        temporary = directory / (role + '.partial.wav')
        sf.write(temporary, audio, rate, subtype='FLOAT')
        temporary.replace(target)
        digest = hashlib.sha256(np.asarray(audio, dtype='<f4').tobytes()).hexdigest()
        rows.append({'role': role, 'source_id': request['recipe_hash'] + ':' + role,
                     'file': target.name, 'pcm_sha': digest, 'file_sha256': sha(target), 'frames': len(audio),
                     'origin_sample': 0, 'gain': 1.0, 'peak': float(np.max(np.abs(audio))),
                     'rms': float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))})
    residual = data.astype(np.float64) - vocals - accompaniment
    manifest = {k: request[k] for k in ('adapter_version', 'source_pcm_sha', 'sample_rate', 'frame_count', 'origin_sample', 'settings', 'deployment_hash', 'recipe_hash')}
    if 'cache_scope' in request:
        manifest.update(cache_scope=request['cache_scope'], scope='preview_only')
    manifest.update(schema=1, id=request['recipe_hash'], parent_source_id=request['source_pcm_sha'], stems=rows,
                    device='cuda',
                    performance={'wall_seconds': round(time.monotonic() - started, 3),
                                 'cuda_peak_allocated_bytes': torch.cuda.max_memory_allocated() if torch.cuda.is_available() else 0,
                                 'cuda_peak_reserved_bytes': torch.cuda.max_memory_reserved() if torch.cuda.is_available() else 0},
                    alignment={'frame_count_exact': True, 'offset_samples': 0, 'gain_applied': False,
                               'reconstruction_rms': float(np.sqrt(np.mean(residual ** 2))),
                               'reconstruction_relative_rms': float(np.sqrt(np.mean(residual ** 2)) / max(1e-12, np.sqrt(np.mean(data.astype(np.float64) ** 2))))})
    temporary = directory / 'manifest.partial.json'
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(directory / 'manifest.json')
    print(json.dumps({'complete': True, 'frames': len(data), 'device': manifest['device']}))


if __name__ == '__main__':
    main(sys.argv[1])
