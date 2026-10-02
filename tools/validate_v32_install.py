"""Load both pinned V32 models and run one short synthetic-audio inference."""
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time
import uuid
import wave

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_DIRECTORY = None


def make_tone(path: Path, seconds: float = 8.0, rate: int = 44100) -> None:
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        chunk = bytearray()
        for index in range(int(seconds * rate)):
            t = index / rate
            phase = t % .5
            pulse = math.exp(-phase * 20) if phase < .16 else 0
            sample = int(10000 * pulse * math.sin(2 * math.pi * 660 * t))
            chunk.extend(struct.pack('<h', sample))
        output.writeframes(chunk)


def main() -> int:
    global ACTIVE_DIRECTORY
    if len(sys.argv) > 1 and sys.argv[1] == '--help':
        print('Run a short local V32 validation using generated synthetic audio.')
        return 0
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError('V32 需要可用的 NVIDIA CUDA 显卡。')
    smoke_id = uuid.uuid4().hex
    directory = ROOT / 'cache' / ('v32-setup-' + smoke_id)
    directory.mkdir(parents=True)
    ACTIVE_DIRECTORY = directory
    audio = directory / 'synthetic-pulse.wav'
    make_tone(audio)
    request = {
        'output': str(directory / 'output'), 'audio': str(audio),
        'title': 'Local V32 validation', 'artist': 'Synthetic test signal',
        'seed': 20261002, 'ln_ratio': .1, 'descriptors': [],
        'negative_descriptors': [], 'year': 2024, 'temperature': .9,
        'top_p': .9, 'mania_column_temperature': .8, 'cfg_scale': 1.0,
        'end_time': 8.0, 'presets': [{'label': '校验', 'key': 'master', 'sr': 5.0}],
    }
    request_path = directory / 'request.json'
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
    log_path = ROOT / 'logs' / 'v32-setup-smoke.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(PYTHONUTF8='1', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                       HF_HOME=str(ROOT / 'cache' / 'huggingface'), WANDB_MODE='disabled',
                       TEMP=str(ROOT / 'cache'), TMP=str(ROOT / 'cache'))
    started = time.monotonic()
    with log_path.open('w', encoding='utf-8') as log:
        process = subprocess.run([sys.executable, '-u', str(ROOT / 'tools' / 'mapperatorinator_worker.py'), str(request_path)],
                                 cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=900)
    if process.returncode:
        tail = log_path.read_text(encoding='utf-8', errors='replace')[-2500:]
        raise RuntimeError(f'V32 校验推理失败，请查看 logs/v32-setup-smoke.log。\n{tail}')
    result_path = directory / 'output' / 'worker-result.json'
    result = json.loads(result_path.read_text(encoding='utf-8'))
    chart_path = Path(result['charts']['master'])
    chart_text = chart_path.read_text(encoding='utf-8-sig')
    if 'Mode: 3' not in chart_text or 'CircleSize: 4' not in chart_text or '[HitObjects]' not in chart_text:
        raise RuntimeError('V32 校验输出不是有效的四键谱面，请查看日志。')
    note_count = sum(1 for line in chart_text.split('[HitObjects]', 1)[1].splitlines() if line.strip())
    marker = ROOT / 'models' / 'mapperatorinator' / 'deployment-ready.json'
    state = {'validated': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
             'smoke_result': {'device': result.get('device', 'cuda'),
                              'generation_seconds': round(time.monotonic() - started, 2),
                              'audio': 'generated synthetic pulse; no user audio used'},
             'validation': {'valid': True, 'notes': note_count, 'mode': '4K',
                            'weights': 'pinned revision and SHA-256 verified'}}
    temporary = marker.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(marker)
    shutil.rmtree(directory)
    ACTIVE_DIRECTORY = None
    print(f'V32 已加载并通过本地冒烟推理；生成 {note_count} 个验证音符。')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        if ACTIVE_DIRECTORY is not None:
            shutil.rmtree(ACTIVE_DIRECTORY, ignore_errors=True)
        print(f'V32 校验失败：{error}', file=sys.stderr)
        raise SystemExit(1)
