"""CUDA-only Kim MelBand inference; full-song overlap buffers remain on CPU.

Chunk padding/fades follow the MIT MSST demix implementation at e247dfe4.
Only internal padding is removed. Model output shape and source clock must match
exactly; malformed/nonfinite output fails rather than being repaired.
"""
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import threading
import time

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'melband_roformer_kim'
VERSION = 'melband-roformer-kim-adapter-v1'
WEIGHT_NAME = 'MelBandRoformer.ckpt'
WEIGHT_SHA = '87201f4d31afb5bc79993230fc49446918425574db48c01c405e44f365c7559e'
CONFIG_NAME = 'config_vocals_mel_band_roformer_kj.yaml'
CHUNK_SIZE = 352800


def sha(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def pcm_sha(audio):
    return hashlib.sha256(np.asarray(audio, dtype='<f4', order='C').tobytes(order='C')).hexdigest()


def require_cuda(torch):
    if not torch.cuda.is_available():
        raise RuntimeError('CUDA 不可用；RoFormer 已停止，请检查独立环境和显卡驱动后重试。')


def load_config(path):
    import yaml

    class ConfigLoader(yaml.SafeLoader):
        pass

    ConfigLoader.add_constructor('tag:yaml.org,2002:python/tuple',
                                 lambda loader, node: tuple(loader.construct_sequence(node)))
    config = yaml.load(Path(path).read_text(encoding='utf-8'), Loader=ConfigLoader)
    audio, model = config['audio'], config['model']
    if (audio['chunk_size'] != CHUNK_SIZE or audio['sample_rate'] != 44100 or audio['num_channels'] != 2
            or model['sample_rate'] != 44100 or model['stereo'] is not True or model['num_stems'] != 1
            or model['stft_hop_length'] != 441 or config['training']['target_instrument'] != 'vocals'):
        raise ValueError('Kim RoFormer 配置与固定的采样时钟、窗口或人声目标不匹配')
    return config


def demix_vocals(model, mix, overlap_count=4, *, device='cuda', amp=True, progress=lambda *_: None):
    """MSST batch=1 overlap-add, with strict shape checks and CPU accumulation.

    ``device`` is injectable for small deterministic unit tests. Production main
    requires CUDA before loading the checkpoint and always passes ``cuda``.
    """
    import torch
    from torch.nn import functional as functional

    if mix.ndim != 2 or mix.shape[0] != 2 or not mix.shape[1] or not torch.isfinite(mix).all():
        raise ValueError('RoFormer 分块输入须为有限双声道 PCM')
    if overlap_count not in (2, 4, 8):
        raise ValueError('RoFormer 窗口覆盖次数须为 2、4 或 8')
    mix = mix.detach().to(device='cpu', dtype=torch.float32)
    source_frames = mix.shape[-1]
    step = CHUNK_SIZE // overlap_count
    border = CHUNK_SIZE - step
    padded = source_frames > 2 * border
    if padded:
        mix = functional.pad(mix, (border, border), mode='reflect')
    fade_size = CHUNK_SIZE // 10
    window = torch.ones(CHUNK_SIZE, dtype=torch.float32)
    window[:fade_size] = torch.linspace(0, 1, fade_size)
    window[-fade_size:] = torch.linspace(1, 0, fade_size)
    result = torch.zeros_like(mix)
    counter = torch.zeros((1, mix.shape[-1]), dtype=torch.float32)
    total_chunks = (mix.shape[-1] + step - 1) // step
    started = time.monotonic()
    with torch.inference_mode(), torch.autocast(device_type='cuda', dtype=torch.float16, enabled=amp):
        for index, start in enumerate(range(0, mix.shape[-1], step)):
            part = mix[:, start:start + CHUNK_SIZE].to(device)
            actual = part.shape[-1]
            mode = 'reflect' if actual > CHUNK_SIZE // 2 else 'constant'
            part = functional.pad(part, (0, CHUNK_SIZE - actual), mode=mode, value=0)
            estimate = model(part[None])
            if not isinstance(estimate, torch.Tensor) or tuple(estimate.shape) != (1, 2, CHUNK_SIZE):
                shape = tuple(estimate.shape) if isinstance(estimate, torch.Tensor) else type(estimate).__name__
                raise ValueError('RoFormer 输出形状不匹配；禁止自动改形或拉伸：' + str(shape))
            estimate = estimate[0].detach().to(device='cpu', dtype=torch.float32)
            if not torch.isfinite(estimate).all():
                raise ValueError('RoFormer 输出含非有限值；禁止替换为静音')
            chunk_window = window.clone()
            if start == 0:
                chunk_window[:fade_size] = 1
            elif start + step >= mix.shape[-1]:
                chunk_window[-fade_size:] = 1
            result[:, start:start + actual] += estimate[:, :actual] * chunk_window[:actual]
            counter[:, start:start + actual] += chunk_window[:actual]
            progress((index + 1) / total_chunks, index + 1, total_chunks, time.monotonic() - started)
    if not torch.isfinite(result).all() or not torch.isfinite(counter).all() or not torch.all(counter > 0):
        raise ValueError('RoFormer 叠加缓冲无效或未覆盖全部样本')
    result /= counter
    if padded:
        result = result[:, border:-border]
    if tuple(result.shape) != (2, source_frames) or not torch.isfinite(result).all():
        raise ValueError('RoFormer 输出与原曲采样时钟不一致')
    return result.numpy().T.copy(), {'chunk_size_samples': CHUNK_SIZE, 'step_samples': step,
                                    'overlap_count': overlap_count, 'batch_size': 1, 'chunk_count': total_chunks,
                                    'border_samples': border if padded else 0, 'accumulation_device': 'cpu'}


def residual_stems(source, vocals):
    if source.ndim != 2 or source.shape[1] != 2 or vocals.shape != source.shape:
        raise ValueError('分离人声输出帧数或声道不匹配')
    if source.dtype != np.float32 or vocals.dtype != np.float32 or not np.isfinite(source).all() or not np.isfinite(vocals).all():
        raise ValueError('分离输入输出须为有限 float32 PCM')
    accompaniment = np.subtract(source, vocals, dtype=np.float32)
    if not np.isfinite(accompaniment).all():
        raise ValueError('伴奏残差含非有限值')
    return vocals, accompaniment


class CudaTelemetry:
    """Sample free memory including other applications; collect phase peaks."""
    def __init__(self, torch):
        self.torch = torch
        self.phase = None
        self.rows = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _sample(self):
        cuda = self.torch.cuda
        free, total = cuda.mem_get_info()
        allocated, reserved = cuda.max_memory_allocated(), cuda.max_memory_reserved()
        with self.lock:
            if self.phase is not None:
                row = self.rows[self.phase]
                row['cuda_peak_allocated_bytes'] = max(row['cuda_peak_allocated_bytes'], allocated)
                row['cuda_peak_reserved_bytes'] = max(row['cuda_peak_reserved_bytes'], reserved)
                row['cuda_free_min_bytes'] = min(row['cuda_free_min_bytes'], free)
                row['cuda_total_bytes'] = total
                row['memory_samples'] += 1

    def _run(self):
        while not self.stop.wait(.05):
            self._sample()

    def begin(self, name):
        if self.phase is not None:
            self.end()
        self.torch.cuda.reset_peak_memory_stats()
        self.rows[name] = {'seconds': 0, 'cuda_peak_allocated_bytes': 0, 'cuda_peak_reserved_bytes': 0,
                           'cuda_free_min_bytes': float('inf'), 'cuda_total_bytes': 0, 'memory_samples': 0}
        self.phase = name
        self.phase_started = time.monotonic()
        self._sample()

    def end(self):
        self.torch.cuda.synchronize()
        self._sample()
        with self.lock:
            self.rows[self.phase]['seconds'] = round(time.monotonic() - self.phase_started, 3)
            self.phase = None

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        if self.phase is not None:
            self.end()
        self.stop.set()
        self.thread.join(timeout=1)

    def report(self, wall_seconds):
        rows = list(self.rows.values())
        return {'wall_seconds': round(wall_seconds, 3),
                'cuda_peak_allocated_bytes': max(row['cuda_peak_allocated_bytes'] for row in rows),
                'cuda_peak_reserved_bytes': max(row['cuda_peak_reserved_bytes'] for row in rows),
                'cuda_free_min_bytes': min(row['cuda_free_min_bytes'] for row in rows),
                'cuda_total_bytes': max(row['cuda_total_bytes'] for row in rows),
                'memory_sample_interval_seconds': .05, 'phases': self.rows}


def write_stems(directory, source, vocals, request):
    vocals, accompaniment = residual_stems(source, vocals)
    rows = []
    for role, audio in (('vocals', vocals), ('accompaniment', accompaniment)):
        target = directory / (role + '.wav')
        temporary = directory / (role + '.partial.wav')
        sf.write(temporary, audio, 44100, subtype='FLOAT')
        temporary.replace(target)
        rows.append({'role': role, 'source_id': request['recipe_hash'] + ':' + role,
                     'file': target.name, 'pcm_sha': pcm_sha(audio), 'file_sha256': sha(target), 'frames': len(audio),
                     'origin_sample': 0, 'gain': 1.0, 'peak': float(np.max(np.abs(audio))),
                     'rms': float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))})
    error = source.astype(np.float64) - vocals.astype(np.float64) - accompaniment.astype(np.float64)
    rms = float(np.sqrt(np.mean(error ** 2)))
    alignment = {'frame_count_exact': True, 'offset_samples': 0, 'gain_applied': False,
                 'accompaniment_method': 'source_minus_vocals', 'reconstruction_rms': rms,
                 'reconstruction_relative_rms': rms / max(1e-12, float(np.sqrt(np.mean(source.astype(np.float64) ** 2))))}
    return rows, alignment


def main(request_path):
    started = time.monotonic()
    request = json.loads(Path(request_path).read_text(encoding='utf-8'))
    if request.get('adapter_version') != VERSION or request.get('sample_rate') != 44100 or request.get('origin_sample') != 0:
        raise ValueError('RoFormer 请求版本或采样时钟不匹配')
    # The registry remains independent of app dependencies inside this runtime.
    import importlib.util
    specification = importlib.util.spec_from_file_location('separation_models', ROOT / 'malody_studio' / 'separation_models.py')
    registry = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(registry)
    settings = registry.validated_settings(request['settings'])
    if settings['model'] != MODEL:
        raise ValueError('RoFormer 工作进程模型不匹配')
    data, rate = sf.read(request['source'], dtype='float32', always_2d=True)
    if rate != 44100 or data.shape != (request['frame_count'], 2) or not len(data) or not np.isfinite(data).all() or pcm_sha(data) != request['source_pcm_sha']:
        raise ValueError('RoFormer 工作进程源 PCM 不匹配')
    source_read_seconds = time.monotonic() - started
    import torch
    require_cuda(torch)
    random.seed(settings['seed']); np.random.seed(settings['seed']); torch.manual_seed(settings['seed'])
    torch.cuda.manual_seed_all(settings['seed'])
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    torch.set_num_threads(4)
    model_root = Path(request['model_root'])
    weights, config_path = model_root / WEIGHT_NAME, model_root / CONFIG_NAME
    if sha(weights) != WEIGHT_SHA:
        raise ValueError('Kim RoFormer 官方权重校验失败')
    deployment = json.loads((model_root / 'manifest.json').read_text(encoding='utf-8'))
    if sha(config_path) != deployment['files'][CONFIG_NAME]['sha256']:
        raise ValueError('Kim RoFormer 配置校验失败')
    config = load_config(config_path)
    sys.path.insert(0, str(ROOT / 'vendor' / 'RoFormer'))
    from models.bs_roformer.mel_band_roformer import MelBandRoformer
    with CudaTelemetry(torch) as telemetry:
        telemetry.begin('model_load')
        model = MelBandRoformer(**config['model'])
        state = torch.load(weights, map_location='cpu', weights_only=True)
        for key in ('state', 'state_dict', 'model_state_dict'):
            if key in state:
                state = state[key]
                break
        model.load_state_dict(state, strict=True)
        del state
        model = model.eval().to('cuda')
        telemetry.end()
        print(json.dumps({'event': 'phase', 'phase': 'model_loaded', 'seconds': telemetry.rows['model_load']['seconds']}), flush=True)
        telemetry.begin('inference')
        def progress(fraction, chunks_done, chunk_count, seconds):
            print(json.dumps({'event': 'progress', 'phase': 'inference', 'fraction': fraction,
                              'chunks_done': chunks_done, 'chunk_count': chunk_count, 'seconds': round(seconds, 3)}), flush=True)
        vocals, effective = demix_vocals(model, torch.from_numpy(data.T.copy()), settings['overlap_count'],
                                        device='cuda', amp=True, progress=progress)
        telemetry.end()
        telemetry.begin('export')
        directory = Path(request['directory'])
        rows, alignment = write_stems(directory, data, vocals, request)
        telemetry.end()
    manifest = {key: request[key] for key in ('adapter_version', 'source_pcm_sha', 'sample_rate', 'frame_count',
                                             'origin_sample', 'settings', 'deployment_hash', 'recipe_hash')}
    if 'cache_scope' in request:
        manifest.update(cache_scope=request['cache_scope'], scope='preview_only')
    performance = telemetry.report(time.monotonic() - started)
    performance['source_read_seconds'] = round(source_read_seconds, 3)
    manifest.update(schema=1, id=request['recipe_hash'], parent_source_id=request['source_pcm_sha'], stems=rows,
                    device='cuda', device_name=torch.cuda.get_device_name(), performance=performance, alignment=alignment,
                    effective_inference={**effective, 'precision': 'cuda_amp_float16', 'tta': False,
                                         'normalization': 'none', 'torch_compile': False},
                    model_identity={'weight_sha256': WEIGHT_SHA, 'config_sha256': sha(config_path),
                                    'code_version': deployment['code_version'], 'code_files': deployment.get('code_files', {})})
    temporary = directory / 'manifest.partial.json'
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(directory / 'manifest.json')
    print(json.dumps({'complete': True, 'frames': len(data), 'device': 'cuda'}), flush=True)


if __name__ == '__main__':
    main(sys.argv[1])
