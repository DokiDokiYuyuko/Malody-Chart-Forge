"""Run pinned upstream V32 in its own environment; use local weights only."""
import json
import hashlib
import os
from pathlib import Path
import sys
import shutil
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / 'vendor' / 'Mapperatorinator'
sys.path.insert(0, str(ROOT))
from malody_studio.inference_policy import V32_INFERENCE_POLICY


def underfilled_sections(hit_times, sections):
    """Return active section retry checks that are sparse on their own."""
    sparse=[]
    for section in sections:
        start,end=section.get('start_time'),section.get('end_time')
        count=sum((start is None or timestamp>=start) and (end is None or timestamp<end)
                  for timestamp in hit_times)
        if count<section['retry_min_heads']:
            sparse.append({**section,'first_heads':count})
    return sparse

def main(request_path=None, resident=None, progress=None):
    request_started=time.monotonic()
    request = json.loads(Path(request_path or sys.argv[1]).read_text(encoding='utf-8'))
    destination = Path(request['output'])
    destination.mkdir(parents=True, exist_ok=True)
    status_path = destination / 'worker-status.json'
    def status(message, percent):
        if progress:progress(message,percent)
        temporary = status_path.with_name(status_path.name+'.'+uuid.uuid4().hex+'.tmp')
        temporary.write_text(json.dumps({'message': message, 'percent': percent}, ensure_ascii=False), encoding='utf-8')
        try:
            for attempt in range(10):
                try:
                    temporary.replace(status_path)
                    break
                except PermissionError:
                    # Progress polling can briefly lock the old file on Windows.
                    # Losing one telemetry update must not destroy inference.
                    if attempt == 9:break
                    time.sleep(.025*(attempt+1))
        finally:temporary.unlink(missing_ok=True)

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
        config = snapshot / 'config.json'
        if not config.is_file() or hashlib.sha256(config.read_bytes()).hexdigest() != record['sha256']:
            temporary = snapshot / (f'config.{os.getpid()}.{uuid.uuid4().hex}.tmp')
            try:
                shutil.copyfile(source, temporary)
                if hashlib.sha256(temporary.read_bytes()).hexdigest() != record['sha256']:
                    raise RuntimeError('Backbone configuration copy checksum mismatch')
                os.replace(temporary, config)
            finally:
                temporary.unlink(missing_ok=True)
        ref = cache / 'refs' / 'main'
        if not ref.is_file() or ref.read_text(encoding='utf-8').strip() != record['revision']:
            temporary = ref.with_name(f'main.{os.getpid()}.{uuid.uuid4().hex}.tmp')
            try:
                temporary.write_text(record['revision'], encoding='utf-8')
                os.replace(temporary, ref)
            finally:
                temporary.unlink(missing_ok=True)
    from hydra import initialize_config_dir, compose
    from omegaconf import OmegaConf
    import torch
    # Feed uncompressed WAV to pydub; no external decoding executable is needed.
    from inference import compile_args, setup_inference_environment, load_model_with_server, get_config, generate
    from pcm_audio import load_wave
    from osuT5.osuT5.inference import preprocessor
    audio_seconds=0.
    def timed_audio(*args,**kwargs):
        nonlocal audio_seconds
        started=time.monotonic()
        try:return load_wave(*args,**kwargs)
        finally:audio_seconds+=time.monotonic()-started
    preprocessor.load_audio_file = timed_audio
    status('复用 GPU 上的 V32 模型' if resident and resident.get('model') else '加载 V32 生成与节拍模型', 12)
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
    requested_batch_size = 32 if torch.cuda.is_available() else 1
    args.max_batch_size = requested_batch_size
    # Keep autoregressive overlap and previous-window context. Upstream's
    # parallel mode changes both audio stride and decoder context, so batching
    # is limited to the semantics-preserving encoder precompute path.
    args.parallel = False
    args.fast_decoder_loop = True
    args.attn_implementation = 'sdpa'
    args.precision = 'bf16'
    args.cfg_scale = request.get('cfg_scale', 1.0)
    args.use_server = False
    if request.get('start_time') is not None:
        args.start_time = request['start_time']
    if request.get('end_time') is not None:
        args.end_time = request['end_time']
    if request.get('timing_reference'):
        args.beatmap_path = request['timing_reference']
        from osuT5.osuT5.tokenizer import ContextType
        args.in_context = [ContextType.TIMING]
    compile_args(args)
    setup_inference_environment(args.seed)
    torch.set_num_threads(4)
    if not torch.cuda.is_available():raise RuntimeError('V32 需要可用 CUDA，不自动转 CPU')
    torch.cuda.reset_peak_memory_stats()
    loader_options = dict(max_batch_size=requested_batch_size, use_server=False, precision=args.precision,
                          attn_implementation='sdpa', gamemode=3, auto_select_gamemode_model=False)
    load_started=time.monotonic()
    model_key=(args.model_path,args.precision,args.attn_implementation,str(args.device))
    model_reused=resident is not None and resident.get('model_key')==model_key
    if model_reused:model,tokenizer=resident['model']
    else:
        model,tokenizer=load_model_with_server(args.model_path,args.train,args.device,**loader_options)
        if resident is not None:resident.update(model_key=model_key,model=(model,tokenizer))
    timing_reused=False
    if request.get('timing_reference'):
        timing_model,timing_tokenizer=None,None
    elif resident is not None and 'timing' in resident:
        timing_model,timing_tokenizer=resident['timing'];timing_reused=True
    else:
        timing_model,timing_tokenizer=load_model_with_server(str(ROOT/'models/mapperatorinator/v32-timing'),args.train,args.device,**loader_options)
        if resident is not None:resident['timing']=(timing_model,timing_tokenizer)
    load_seconds=time.monotonic()-load_started
    main_seconds=retry_seconds=0.
    started = time.monotonic()
    results = {}
    retry_records = []
    sequential_fallback = False
    batch_fallbacks = 0

    def infer(generation_config, beatmap_config):
        nonlocal sequential_fallback, batch_fallbacks
        while True:
            try:
                return generate(args, model=model, tokenizer=tokenizer, timing_model=timing_model,
                                timing_tokenizer=timing_tokenizer, generation_config=generation_config,
                                beatmap_config=beatmap_config)
            except RuntimeError as exc:
                message = str(exc).lower()
                oom_type = getattr(torch.cuda, 'OutOfMemoryError', None)
                is_cuda_oom = (oom_type is not None and isinstance(exc, oom_type)) or 'cuda out of memory' in message
                if not is_cuda_oom or args.max_batch_size <= 1:
                    raise
                # Halve the batch after each allocation failure until it fits
                # the current desktop/application memory load or becomes serial.
                torch.cuda.empty_cache()
                args.max_batch_size = max(1, args.max_batch_size // 2)
                # Reduce only the encoder precompute batch. Keep sequential
                # window decoding even when the batch falls to one.
                args.parallel = False
                sequential_fallback = True
                batch_fallbacks += 1
                setup_inference_environment(args.seed)
                generation_config, beatmap_config = get_config(args)

    def hit_times(path):
        times=[];in_hits=False
        for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
            if line.startswith('['):in_hits=line.strip()=='[HitObjects]'
            elif in_hits and ',' in line:
                try:times.append(float(line.split(',')[2]))
                except (ValueError,IndexError):continue
        return times

    def count_heads(times, start, end):
        return sum((start is None or timestamp>=start) and (end is None or timestamp<end)
                   for timestamp in times)

    for index, preset in enumerate(request['presets']):
        status('V32 生成' + preset['label'] + '谱面与节拍', 22 + index * 70 / len(request['presets']))
        args.seed = preset.get('seed', request['seed'] + index)
        setup_inference_environment(args.seed)
        args.difficulty = preset['sr']
        args.version = '4K ' + preset['label'] + ' / V32'
        # A single loaded model serves every frozen core request. These values
        # must reset each iteration, including the seed and native source range.
        args.start_time = preset.get('start_time', request.get('start_time'))
        args.end_time = preset.get('end_time', request.get('end_time'))
        preset_folder = destination / preset['key']
        preset_folder.mkdir(exist_ok=True)
        args.output_path = str(preset_folder)
        generation_config, beatmap_config = get_config(args)
        infer_started=time.monotonic()
        _, path = infer(generation_config, beatmap_config)
        main_seconds+=time.monotonic()-infer_started
        results[preset['key']] = str(path)
        minimum = preset.get('retry_min_heads', 0)
        retry_sections=preset.get('retry_sections') or []
        checks=(retry_sections if retry_sections else ([{'start_time':args.start_time,'end_time':args.end_time,
                                                         'retry_min_heads':minimum}] if minimum else []))
        initial_times=hit_times(path)
        sparse=underfilled_sections(initial_times,checks)
        if sparse and preset.get('retry_condition', args.difficulty) > args.difficulty:
            # Test each original core independently. A dense neighboring core
            # must not hide an empty but clearly active segment after coalescing.
            first_counts=[(row['start_time'],row['end_time'],row['first_heads'],row['retry_min_heads']) for row in sparse]
            if first_counts:
                args.seed = preset['retry_seed']; setup_inference_environment(args.seed)
                args.difficulty = preset['retry_condition']
                args.version += ' / local retry'
                retry_folder = destination / (preset['key'] + '__retry'); retry_folder.mkdir(exist_ok=True)
                args.output_path = str(retry_folder)
                generation_config, beatmap_config = get_config(args)
                infer_started=time.monotonic()
                _, retry_path = infer(generation_config, beatmap_config)
                retry_seconds+=time.monotonic()-infer_started
                retry_times=hit_times(retry_path)
                retry_detail=[{'start_time':start,'end_time':end,'first_heads':heads,'retry_heads':count_heads(retry_times,start,end),
                               'retry_min_heads':threshold}
                              for start,end,heads,threshold in first_counts]
                results[preset['key'] + '__retry'] = str(retry_path)
                retry_records.append({'key':preset['key'],'condition':args.difficulty,'seed':args.seed,
                                      'bounded_attempts':1,'sections':retry_detail})
    result = {'charts': results, 'device': str(args.device),
              'resident':{'pid':os.getpid(),'model_reused':model_reused,'timing_reused':timing_reused,
                  'load_seconds':round(load_seconds,4),'main_inference_seconds':round(main_seconds,4),
                  'retry_seconds':round(retry_seconds,4),'audio_decode_seconds':round(audio_seconds,4),'request_seconds':round(time.monotonic()-request_started,4)},
              'generation_seconds': round(time.monotonic() - started, 2),
              'peak_allocated_vram_mb': round(torch.cuda.max_memory_allocated() / 1024**2, 1),
              'parallel_inference': bool(args.parallel), 'requested_batch_size': requested_batch_size,
              'effective_batch_size': args.max_batch_size, 'batch_fallbacks': batch_fallbacks,
              'sequential_fallback': sequential_fallback,
              'window_overlap_preserved': True,
              'inference_policy': V32_INFERENCE_POLICY,
              'timing_model_loaded': timing_model is not None,
              'local_retries': retry_records}
    (destination / 'worker-result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    status('V32 原始谱面生成完成', 92)
    return result

if __name__ == '__main__':
    main()
