"""Run pinned upstream V32 in its own environment; use local weights only."""
import json
import hashlib
import os
from pathlib import Path
import sys
import shutil
import time

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / 'vendor' / 'Mapperatorinator'

def main():
    request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    destination = Path(request['output'])
    destination.mkdir(parents=True, exist_ok=True)
    status_path = destination / 'worker-status.json'
    def status(message, percent):
        temporary = status_path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'message': message, 'percent': percent}, ensure_ascii=False), encoding='utf-8')
        temporary.replace(status_path)

    os.chdir(VENDOR)
    sys.path.insert(0, str(VENDOR))
    # Upstream constructors request backbone JSON even with a complete trained
    # config. Restore pinned configuration only; no backbone weights are needed.
    records = json.loads((ROOT / 'models' / 'mapperatorinator' / 'backbone-configs.json').read_text(encoding='utf-8'))
    for record in records:
        source = ROOT / 'models' / 'mapperatorinator' / 'backbone-configs' / record['repo'].replace('/', '-') / 'config.json'
        with source.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != record['sha256']:
                raise RuntimeError('Backbone configuration checksum mismatch')
        cache = ROOT / 'cache' / 'huggingface' / 'hub' / ('models--' + record['repo'].replace('/', '--'))
        snapshot = cache / 'snapshots' / record['revision']
        snapshot.mkdir(parents=True, exist_ok=True)
        (cache / 'refs').mkdir(exist_ok=True)
        shutil.copyfile(source, snapshot / 'config.json')
        (cache / 'refs' / 'main').write_text(record['revision'], encoding='utf-8')
    from hydra import initialize_config_dir, compose
    from omegaconf import OmegaConf
    import torch
    # Feed uncompressed WAV to pydub; no external decoding executable is needed.
    from inference import compile_args, setup_inference_environment, load_model_with_server, get_config, generate
    status('加载 V32 生成与节拍模型', 12)
    with initialize_config_dir(config_dir=str(VENDOR / 'configs' / 'inference'), version_base='1.1'):
        args = OmegaConf.to_object(compose(config_name='v32'))
    args.model_path = str(ROOT / 'models' / 'mapperatorinator' / 'v32-mania')
    args.auto_select_gamemode_model = False
    args.audio_path = request['audio']
    args.output_path = str(destination)
    args.gamemode, args.keycount = 3, 4
    args.title, args.artist = request['title'], request['artist']
    args.creator = 'Malody Studio / Mapperatorinator V32 (AI)'
    args.seed = request['seed']
    args.hold_note_ratio = request['ln_ratio']
    args.scroll_speed_ratio = 0.0
    args.descriptors = request.get('descriptors') or None
    args.negative_descriptors = request.get('negative_descriptors') or None
    args.year = request.get('year', 2024)
    args.temperature = request.get('temperature', .9)
    args.top_p = request.get('top_p', .9)
    args.mania_column_temperature = request.get('mania_column_temperature', .8)
    args.output_type = args.output_type[:2]  # TIMING + MAP; no SV effects.
    args.max_batch_size = 4
    args.fast_decoder_loop = True
    args.attn_implementation = 'sdpa'
    args.precision = 'bf16'
    args.cfg_scale = request.get('cfg_scale', 1.0)
    args.use_server = False
    if request.get('end_time'):
        args.end_time = request['end_time']
    compile_args(args)
    setup_inference_environment(args.seed)
    torch.set_num_threads(4)
    torch.cuda.reset_peak_memory_stats()
    loader_options = dict(max_batch_size=4, use_server=False, precision=args.precision,
                          attn_implementation='sdpa', gamemode=3, auto_select_gamemode_model=False)
    model, tokenizer = load_model_with_server(args.model_path, args.train, args.device, **loader_options)
    timing_model, timing_tokenizer = load_model_with_server(
        str(ROOT / 'models' / 'mapperatorinator' / 'v32-timing'), args.train, args.device, **loader_options)
    started = time.monotonic()
    results = {}
    for index, preset in enumerate(request['presets']):
        status('V32 生成' + preset['label'] + '谱面与节拍', 22 + index * 70 / len(request['presets']))
        args.seed = request['seed'] + index
        setup_inference_environment(args.seed)
        args.difficulty = preset['sr']
        args.version = '4K ' + preset['label'] + ' / V32'
        generation_config, beatmap_config = get_config(args)
        _, path = generate(args, model=model, tokenizer=tokenizer, timing_model=timing_model,
                           timing_tokenizer=timing_tokenizer, generation_config=generation_config,
                           beatmap_config=beatmap_config)
        results[preset['key']] = str(path)
    result = {'charts': results, 'device': str(args.device),
              'generation_seconds': round(time.monotonic() - started, 2),
              'peak_allocated_vram_mb': round(torch.cuda.max_memory_allocated() / 1024**2, 1)}
    (destination / 'worker-result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    status('V32 原始谱面生成完成', 92)

if __name__ == '__main__':
    main()
