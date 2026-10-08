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


def _clear_failure_frames(exc):
    """Drop tensors held by failed stream tracebacks before a smaller retry."""
    import traceback
    seen = set()
    pending = [exc]
    while pending:
        error = pending.pop()
        if error is None or id(error) in seen:
            continue
        seen.add(id(error))
        pending.extend((error.__cause__, error.__context__))
        traceback.clear_frames(error.__traceback__)
        error.__traceback__ = None


def _run_lockstep_presets(request, args, inference_module, run_preset, record_factory):
    """Run an already admitted direct request; the caller owns sequential reruns.

    The scheduler's baton is the only yield boundary. Save/restore the grammar
    module's active request there, so suspended model_generate calls retain their
    own state without changing the serializer or grammar implementation.
    """
    import copy
    import gc
    import torch
    from malody_studio import v32_batch_streams as streams, v32_batch_decode as decode
    from malody_studio import v32_grammar_mask as grammar
    presets = request['presets']
    engine = decode.BatchEngine()
    original_processor = inference_module.Processor
    router = streams.ProcessorRouter(original_processor)
    original_grammar = grammar._current
    device_type = torch.device(args.device).type
    autocast_enabled = torch.is_autocast_enabled(device_type)
    autocast_dtype = torch.get_autocast_dtype(device_type)
    autocast_cache = torch.is_autocast_cache_enabled()

    def autocast():
        # Autocast and grad mode are thread-local; a spawned stream inherits neither.
        return torch.autocast(device_type, enabled=autocast_enabled,
                              dtype=autocast_dtype, cache_enabled=autocast_cache)

    def body(stream):
        local_args = copy.deepcopy(args)
        preset = copy.deepcopy(presets[stream.index])
        submit = stream.submit

        def routed_submit(window):
            active = grammar._current
            grammar._current = None
            try:
                return submit(window)
            finally:
                grammar._current = active

        stream.submit = routed_submit
        with torch.no_grad(), autocast():
            stream.rng = decode.make_generator(preset.get('seed', request['seed'] + stream.index), local_args.device)
            rec = record_factory()
            try:
                run_preset(stream.index, preset, local_args, rec, batched=True)
                return rec
            finally:
                grammar._current = None

    def decode_round(requests, batch):
        with torch.no_grad(), autocast():
            engine.decode(requests, batch)

    def classify_failure(exc):
        oom = streams.is_cuda_oom(exc)
        # Called after all group threads have joined. Do not clear a live frame.
        _clear_failure_frames(exc)
        if oom:
            engine.release()
            gc.collect()
        return oom

    entries = [(i, p['key'], (p.get('end_time', request.get('end_time')) or 0)
                - (p.get('start_time', request.get('start_time')) or 0), p['sr'])
               for i, p in enumerate(presets)]
    uninstall = None
    started = time.monotonic()
    try:
        inference_module.Processor = router
        grammar._current = None
        uninstall = decode.install_hook(streams)
        outcome = streams.run_lockstep(entries, streams.clamp_streams(request.get('parallel_streams')),
                                      body=body, decode=decode_round, is_oom=classify_failure)
    finally:
        try:
            if uninstall is not None:
                uninstall()
        finally:
            inference_module.Processor = original_processor
            grammar._current = original_grammar
            engine.release()
    keys = [p['key'] for p in presets]
    record = {'policy': decode.BATCH_DECODE_POLICY,
              'requested': streams.clamp_streams(request.get('parallel_streams')),
              'attempts': outcome['groups'], 'fallbacks': outcome['fallbacks'],
              'groups': [group for attempt in outcome['groups'] for group in attempt['groups']],
              'sequential_reruns': [keys[i] for i in outcome['sequential']],
              'batched_presets': [keys[i] for i in sorted(outcome['outcomes'])],
              'engine': dict(engine.stats), 'batched_seconds': round(time.monotonic() - started, 3)}
    return outcome, record


def _attempt_directory(parent, mode):
    parent = Path(parent) / 'decode-attempts'
    parent.mkdir(parents=True, exist_ok=True)
    index = 1
    while True:
        folder = parent / f'{index:04d}-{mode}'
        # Numbering includes both modes; never overwrite an earlier failed attempt.
        if any(parent.glob(f'{index:04d}-*')):
            index += 1
            continue
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            index += 1


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


def density_supply_deficits(hit_times, sections):
    """Count core supply and independent measured audio heads, never padding."""
    deficits=[]
    for row in sections:
        core_times=[t for t in hit_times if row['start_time']<=t<row['end_time']]
        acoustic=[t for t in row.get('acoustic_times',[]) if row['start_time']<=t<row['end_time']]
        independent=sum(not any(abs(t-onset)<=15 for t in core_times) for onset in acoustic)
        target=row.get('density_target_heads',row['density_min_heads'])
        if len(core_times)+independent<target:
            deficits.append({**row,'model_heads':len(core_times),'independent_acoustic_heads':independent,
                             'deficit':target-len(core_times)-independent})
    return deficits


def apply_experimental_conditioning(args, request):
    """Freeze non-clock inheritance only for explicit parameter experiments."""
    if request.get('experimental_parameter_snapshot') is not True:return
    values=request.get('experimental_conditioning',{})
    allowed={'gamemode','keycount','difficulty','beatmap_id','mapper_id','descriptors',
             'hitsounded','hp_drain_rate','circle_size','overall_difficulty','approach_rate',
             'slider_multiplier','slider_tick_rate','hold_note_ratio','scroll_speed_ratio',
             'title','title_unicode','artist','artist_unicode','creator','source','background','preview_time'}
    if set(values)-allowed:raise ValueError('Experimental conditioning must not replace timing or sampler parameters')
    for key,value in values.items():setattr(args,key,value)


def compile_generation_args(args, request, compiler, timing_context):
    """A TIMING reference supplies a clock, never chart style or difficulty."""
    reference = request.get('timing_reference')
    if reference:
        path = Path(reference)
        if not path.is_file() or path.suffix.lower() != '.osu':
            raise ValueError('节拍参考文件不存在或格式错误')
    # Upstream imports every unspecified generation condition from beatmap_path.
    # Compile the same defaults as an audio-only request before adding TIMING.
    args.beatmap_path = ''
    apply_experimental_conditioning(args, request)
    compiler(args)
    apply_experimental_conditioning(args, request)
    if reference:
        args.beatmap_path = str(Path(reference).resolve())
        args.in_context = [timing_context]
    return 'timing-context-only-v1'

def _run_request(request_path=None, resident=None, progress=None):
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
    from malody_studio.workflow_log import stage, event
    import inference as upstream_inference
    from malody_studio.v32_event_serialization import install as install_hold_serializer
    install_hold_serializer(upstream_inference, terminal_policy=request.get('native_event_policy'))
    from malody_studio.v32_grammar_mask import install as install_grammar_mask
    install_grammar_mask(upstream_inference, policy=request.get('grammar_policy'))
    # Same tokens for a fixed seed; only host-side per-token overhead is removed.
    from malody_studio import v32_fast_decode
    v32_fast_decode.install()
    from pcm_audio import load_wave
    from osuT5.osuT5.inference import preprocessor
    audio_seconds=0.
    def timed_audio(*args,**kwargs):
        nonlocal audio_seconds
        started=time.monotonic()
        try:return load_wave(*args,**kwargs)
        finally:audio_seconds+=time.monotonic()-started
    preprocessor.load_audio_file = timed_audio
    if request.get('action') == 'timing_only':
        return timing_only(request, resident, status)
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
    args.resnap_events = False
    args.use_server = False
    if request.get('start_time') is not None:
        args.start_time = request['start_time']
    if request.get('end_time') is not None:
        args.end_time = request['end_time']
    from osuT5.osuT5.tokenizer import ContextType
    reference_policy = compile_generation_args(args, request, compile_args, ContextType.TIMING)
    setup_inference_environment(args.seed)
    torch.set_num_threads(4)
    if not torch.cuda.is_available():raise RuntimeError('V32 需要可用 CUDA，不自动转 CPU')
    torch.cuda.reset_peak_memory_stats()
    loader_options = dict(max_batch_size=requested_batch_size, use_server=False, precision=args.precision,
                          attn_implementation='sdpa', gamemode=3, auto_select_gamemode_model=False)
    load_started=time.monotonic()
    model_key=(args.model_path,args.precision,args.attn_implementation,str(args.device))
    model_reused=resident is not None and resident.get('model_key')==model_key
    with stage('v32.load_or_reuse_mania_model', model_path=args.model_path, device=str(args.device),
               precision=args.precision, attention=args.attn_implementation, batch_size=requested_batch_size,
               model_reused=model_reused):
        if model_reused:model,tokenizer=resident['model']
        else:
            model,tokenizer=load_model_with_server(args.model_path,args.train,args.device,**loader_options)
            if resident is not None:resident.update(model_key=model_key,model=(model,tokenizer))
    from malody_studio.v32_cfg import install_cfg_batch_order
    install_cfg_batch_order(model)
    timing_reused=False
    with stage('v32.load_or_reuse_timing_model', timing_reference=bool(request.get('timing_reference')),
               device=str(args.device), precision=args.precision):
        if request.get('timing_reference'):
            timing_model,timing_tokenizer=None,None
        elif resident is not None and 'timing' in resident:
            timing_model,timing_tokenizer=resident['timing'];timing_reused=True
        else:
            timing_model,timing_tokenizer=load_model_with_server(str(ROOT/'models/mapperatorinator/v32-timing'),args.train,args.device,**loader_options)
            if resident is not None:resident['timing']=(timing_model,timing_tokenizer)
    if timing_model is not None:install_cfg_batch_order(timing_model)
    load_seconds=time.monotonic()-load_started
    started = time.monotonic()
    original_context=list(args.in_context);original_beatmap=args.beatmap_path
    timing_attempted_keys=set()
    sequential_fallback = False
    batch_fallbacks = 0
    isolate_attempts = False
    attempt_records = []

    class Record:
        """Outputs of one preset; merged in preset order whichever way the presets ran."""
        def __init__(self):
            self.results={};self.retry_records=[];self.failures={};self.timing_recoveries=[]
            self.condition_records=[];self.main_seconds=self.retry_seconds=0.

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

    def run_preset(index, preset, args, rec, batched=False):
        """One preset exactly as the sequential loop ran it. Batched streams fail instead of recovering."""
        def infer(generation_config, beatmap_config):
            nonlocal sequential_fallback, batch_fallbacks
            attempt_root = Path(args.output_path)
            while True:
                attempt = None
                if isolate_attempts:
                    mode = 'batch' if batched else 'sequential'
                    args.output_path = str(_attempt_directory(attempt_root, mode))
                    attempt = {'key': preset['key'], 'mode': mode, 'output': args.output_path,
                               'seed': args.seed, 'condition': args.difficulty,
                               'effective_batch_size': args.max_batch_size, 'parallel': args.parallel,
                               'status': 'started'}
                    attempt_records.append(attempt)
                    import dataclasses
                    def plain_config(value):
                        return dataclasses.asdict(value) if dataclasses.is_dataclass(value) else vars(value)
                    snapshot = {'args': plain_config(args), 'generation_config': plain_config(generation_config),
                                'beatmap_config': plain_config(beatmap_config), 'request': request,
                                'decode_execution': dict(attempt), 'reference_conditioning_policy': reference_policy}
                    (Path(args.output_path) / 'resolved-parameters.json').write_text(
                        json.dumps(snapshot, indent=2, default=str), encoding='utf-8')
                try:
                    from malody_studio.v32_attempt_diagnostics import generate_with_diagnostics
                    with stage('v32.generate_attempt',preset=preset.get('key'),difficulty=preset.get('difficulty_key'),
                               condition=args.difficulty,seed=args.seed,batch_size=args.max_batch_size,
                               start_time=args.start_time,end_time=args.end_time,
                               retry_kind=failure_key if failure_key!=preset.get('key') else 'primary'):
                        generated = generate_with_diagnostics(generate, upstream_inference, args,
                                        model=model, tokenizer=tokenizer, timing_model=timing_model,
                                        timing_tokenizer=timing_tokenizer, generation_config=generation_config,
                                        beatmap_config=beatmap_config, request=request,
                                        reference_policy=reference_policy)
                        if attempt is not None:
                            attempt.update(status='completed', chart=str(generated[1]))
                        return generated
                except AssertionError as exc:
                    if attempt is not None:attempt.update(status='failed', error=str(exc), error_type=type(exc).__name__)
                    if batched:raise
                    from malody_studio.v32_recovery import recovery_reference
                    fallback_identity=request.get('timing_fallback_identity')
                    fallback_reference=request.get('timing_fallback_reference')
                    if fallback_identity is not None:
                        from malody_studio.density_timing_fallback import validate_staged_reference
                        fallback_reference=validate_staged_reference(fallback_reference,fallback_identity,ROOT)
                    args.beatmap_path=recovery_reference(exc,preset.get('difficulty_key',preset['key']),fallback_reference,timing_attempted_keys)
                    from osuT5.osuT5.tokenizer import ContextType
                    args.in_context=[ContextType.TIMING]
                    generation_config,beatmap_config=get_config(args)
                    record={'key':preset['key'],'source':'project_reference' if fallback_identity is None else 'model-derived-whole-song',
                            'attempts':1}
                    if fallback_identity is not None:record['fallback_identity']=fallback_identity
                    rec.timing_recoveries.append(record)
                    status('节拍缺失，使用实验模型参考恢复一次' if fallback_identity is not None else '节拍缺失，使用项目参考恢复一次',30)
                except RuntimeError as exc:
                    if attempt is not None:attempt.update(status='failed', error=str(exc), error_type=type(exc).__name__)
                    if batched:raise
                    message = str(exc).lower()
                    oom_type = getattr(torch.cuda, 'OutOfMemoryError', None)
                    is_cuda_oom = (oom_type is not None and isinstance(exc, oom_type)) or 'cuda out of memory' in message
                    if not is_cuda_oom or args.max_batch_size <= 1:
                        raise
                    # Halve the batch after each allocation failure until it fits
                    # the current desktop/application memory load or becomes serial.
                    if isolate_attempts:
                        _clear_failure_frames(exc)
                        import gc
                        gc.collect()
                    torch.cuda.empty_cache()
                    args.max_batch_size = max(1, args.max_batch_size // 2)
                    # Reduce only the encoder precompute batch. Keep sequential
                    # window decoding even when the batch falls to one.
                    args.parallel = False
                    sequential_fallback = True
                    batch_fallbacks += 1
                    setup_inference_environment(args.seed)
                    generation_config, beatmap_config = get_config(args)
                except Exception as exc:
                    if attempt is not None:attempt.update(status='failed', error=str(exc), error_type=type(exc).__name__)
                    raise
                finally:
                    if attempt is not None:
                        (Path(attempt['output']) / 'decode-attempt-result.json').write_text(
                            json.dumps(attempt, indent=2), encoding='utf-8')


        failure_key = preset['key']
        try:
            args.in_context=list(original_context);args.beatmap_path=original_beatmap
            status('V32 生成' + preset['label'] + '谱面与节拍', 22 + index * 70 / len(request['presets']))
            args.seed = preset.get('seed', request['seed'] + index)
            setup_inference_environment(args.seed)
            args.difficulty = preset['sr']
            rec.condition_records.append({'key':preset['key'],'requested_condition':preset.get('requested_condition',preset['sr']),
                'executed_condition':args.difficulty,'seed':args.seed,
                'condition_rationale':preset.get('condition_rationale','frozen_requested_condition'),
                'core':preset.get('core'),'context':preset.get('context'),'members':preset.get('_member_keys',[])})
            args.version = '4K ' + preset['label'] + ' / V32'
            # A single loaded model serves every frozen core request. These values
            # must reset each iteration, including the seed and native source range.
            args.start_time = preset.get('start_time', request.get('start_time'))
            args.end_time = preset.get('end_time', request.get('end_time'))
            preset_folder = destination / preset['key']
            preset_folder.mkdir(exist_ok=True)
            args.output_path = str(preset_folder)
            generation_config, beatmap_config = get_config(args)
            import dataclasses
            def config_record(value):
                return dataclasses.asdict(value) if dataclasses.is_dataclass(value) else vars(value)
            snapshot={'args':config_record(args),'generation_config':config_record(generation_config),
                      'beatmap_config':config_record(beatmap_config),'request':request,
                      'reference_experiment_only':bool(request.get('reference_experiment_only')),
                      'reference_conditioning_policy':reference_policy}
            if not isolate_attempts:
                (preset_folder/'resolved-parameters.json').write_text(json.dumps(snapshot,indent=2,default=str),encoding='utf-8')
            infer_started=time.monotonic()
            _, path = infer(generation_config, beatmap_config)
            rec.main_seconds+=time.monotonic()-infer_started
            rec.results[preset['key']] = str(path)
            minimum = preset.get('retry_min_heads', 0)
            retry_sections=preset.get('retry_sections') or []
            checks=(retry_sections if retry_sections else ([{'start_time':preset.get('core_start_time',args.start_time),'end_time':preset.get('core_end_time',args.end_time),
                                                             'retry_min_heads':minimum}] if minimum else []))
            initial_times=hit_times(path)
            sparse=underfilled_sections(initial_times,checks)
            if sparse and not preset.get('density_policy') and not preset.get('disable_density_retry') and preset.get('retry_condition', args.difficulty) > args.difficulty:
                # Test each original core independently. A dense neighboring core
                # must not hide an empty but clearly active segment after coalescing.
                first_counts=[(row['start_time'],row['end_time'],row['first_heads'],row['retry_min_heads']) for row in sparse]
                if first_counts:
                    args.seed = preset['retry_seed']; setup_inference_environment(args.seed)
                    args.difficulty = preset['retry_condition']
                    args.version += ' / local retry'
                    failure_key = preset['key'] + '__retry'
                    retry_folder = destination / failure_key; retry_folder.mkdir(exist_ok=True)
                    args.output_path = str(retry_folder)
                    generation_config, beatmap_config = get_config(args)
                    retry_record={'key':preset['key'],'attempt_key':failure_key,'condition':args.difficulty,'seed':args.seed,
                                  'bounded_attempts':1,'status':'started'}
                    rec.retry_records.append(retry_record)
                    infer_started=time.monotonic()
                    _, retry_path = infer(generation_config, beatmap_config)
                    rec.retry_seconds+=time.monotonic()-infer_started
                    retry_times=hit_times(retry_path)
                    retry_detail=[{'start_time':start,'end_time':end,'first_heads':heads,'retry_heads':count_heads(retry_times,start,end),
                                   'retry_min_heads':threshold}
                                  for start,end,heads,threshold in first_counts]
                    rec.results[preset['key'] + '__retry'] = str(retry_path)
                    retry_record.update(status='completed',sections=retry_detail)
            if preset.get('density_policy') and not preset.get('disable_density_retry'):
                minimum=int(preset.get('density_min_heads',0))
                for round_index,round_spec in enumerate(preset.get('density_rounds',[])[:2],1):
                    density_sections=preset.get('density_sections') or [{'start_time':preset.get('core_start_time',args.start_time),
                        'end_time':preset.get('core_end_time',args.end_time),'density_min_heads':minimum}]
                    if not density_supply_deficits(initial_times,density_sections):break
                    args.seed=round_spec['seed'];setup_inference_environment(args.seed)
                    args.difficulty=round_spec['condition']
                    retry_key=preset['key']+'__density_retry'+str(round_index)
                    failure_key = retry_key
                    retry_folder=destination/retry_key;retry_folder.mkdir(exist_ok=True)
                    args.output_path=str(retry_folder)
                    generation_config,beatmap_config=get_config(args)
                    retry_record={'key':preset['key'],'attempt_key':failure_key,'condition':args.difficulty,'seed':args.seed,
                                  'bounded_attempts':round_index,'policy':'density-validation-v1','status':'started'}
                    rec.retry_records.append(retry_record)
                    infer_started=time.monotonic()
                    _,retry_path=infer(generation_config,beatmap_config)
                    rec.retry_seconds+=time.monotonic()-infer_started
                    rec.results[retry_key]=str(retry_path)
                    # Complete attempts are kept separately; never union their notes.
                    initial_times=hit_times(retry_path)
                    retry_record['status']='completed'
        except Exception as exc:
            if batched:raise
            if not isolate_attempts and not isinstance(exc, (AssertionError, ValueError)):raise
            rec.failures[failure_key]=str(exc)
            if rec.retry_records and rec.retry_records[-1].get('key')==preset['key'] and rec.retry_records[-1].get('status')=='started':
                rec.retry_records[-1].update(status='failed',error=str(exc))

    presets = request['presets']
    records = {}
    sequential_indices = list(range(len(presets)))
    batching_record = {'policy': 'sequential', 'requested': 0, 'reason': 'disabled'}
    from malody_studio import v32_batch_streams as batch_streams
    requested_streams = batch_streams.clamp_streams(request.get('parallel_streams'))
    batched_seconds = 0.
    if requested_streams:
        refusal = batch_streams.batching_refusal(request, v32_fast_decode.enabled())
        if refusal:
            batching_record = {'policy': 'sequential', 'requested': requested_streams, 'reason': refusal}
        else:
            isolate_attempts = True
            phase_started = time.monotonic()
            try:
                with stage('v32.lockstep_decode', requested=requested_streams, presets=[p['key'] for p in presets]):
                    outcome, batching_record = _run_lockstep_presets(request, args, upstream_inference, run_preset, Record)
                records.update(outcome['outcomes'])
                sequential_indices = outcome['sequential']
            except Exception as exc:
                # Hook/setup failures also rerun complete presets with fresh state.
                _clear_failure_frames(exc)
                batching_record = {'policy': 'sequential', 'requested': requested_streams,
                                   'reason': 'batch_setup_failed', 'error': str(exc),
                                   'sequential_reruns': [p['key'] for p in presets]}
            batched_seconds = time.monotonic() - phase_started
    if isolate_attempts and sequential_indices:
        sequential_fallback = True
    for index in sequential_indices:
        import copy
        records[index] = Record()
        run_preset(index, presets[index], copy.deepcopy(args) if isolate_attempts else args, records[index])
    results = {};retry_records = [];failures = {};timing_recoveries = [];condition_records = []
    main_seconds = batched_seconds;retry_seconds = 0.
    for index in range(len(presets)):
        record = records[index]
        results.update(record.results);retry_records.extend(record.retry_records);failures.update(record.failures)
        timing_recoveries.extend(record.timing_recoveries);condition_records.extend(record.condition_records)
        if index in sequential_indices:main_seconds += record.main_seconds
        retry_seconds += record.retry_seconds

    result = {'charts': results,'errors':failures,'timing_recoveries':timing_recoveries, 'device': str(args.device),
              'resident':{'pid':os.getpid(),'model_reused':model_reused,'timing_reused':timing_reused,
                  'load_seconds':round(load_seconds,4),'main_inference_seconds':round(main_seconds,4),
                  'retry_seconds':round(retry_seconds,4),'audio_decode_seconds':round(audio_seconds,4),'request_seconds':round(time.monotonic()-request_started,4)},
              'generation_seconds': round(time.monotonic() - started, 2),
              'peak_allocated_vram_mb': round(torch.cuda.max_memory_allocated() / 1024**2, 1),
              'parallel_inference': bool(args.parallel), 'requested_batch_size': requested_batch_size,
              'effective_batch_size': args.max_batch_size, 'batch_fallbacks': batch_fallbacks,
              'sequential_fallback': sequential_fallback,
              'window_overlap_preserved': True,
              'resnap_events': False,
              'inference_policy': V32_INFERENCE_POLICY,
              'decode_loop': v32_fast_decode.LEAN_DECODE_POLICY if v32_fast_decode.enabled() else 'upstream-compiled',
              'reference_conditioning_policy':reference_policy,
              'timing_model_loaded': timing_model is not None,
              'local_retries': retry_records,'actual_conditions':condition_records,
              'independent_generate_resets_output_context':True}
    batching_record['preset_attempts'] = attempt_records
    if requested_streams:
        from malody_studio.v32_batch_streams import batch_identity
        batching_record['identity'] = batch_identity({'parallel_streams': requested_streams})
    result['decode_batching'] = batching_record
    (destination / 'worker-result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    status('V32 原始谱面生成完成', 92)
    return result

def timing_only(request, resident=None, progress=None):
    """Independent timing tokens only. Never consumes notes or reference rescue."""
    import numpy as np
    import torch
    from hydra import initialize_config_dir, compose
    from omegaconf import OmegaConf
    from inference import compile_args, setup_inference_environment, load_model_with_server, get_config
    from osuT5.osuT5.tokenizer import ContextType, EventType
    from osuT5.osuT5.inference.preprocessor import Preprocessor
    from osuT5.osuT5.inference.processor import Processor
    from osuT5.osuT5.inference.super_timing_generator import SuperTimingGenerator
    from malody_studio.resident import atomic
    if request.get('timing_reference') or request.get('timing_fallback_reference'):
        raise ValueError('Independent timing cannot use reference or rescue')
    with initialize_config_dir(config_dir=str(VENDOR/'configs/inference'),version_base='1.1'):
        args=OmegaConf.to_object(compose(config_name='v32'))
    args.model_path=str(ROOT/'models/mapperatorinator/v32-timing')
    args.auto_select_gamemode_model=False;args.use_server=False
    args.audio_path=request['audio'];args.output_path=request['output']
    args.gamemode=3;args.keycount=4;args.seed=request['seed'];args.in_context=[]
    args.output_type=[ContextType.TIMING];args.super_timing=bool(request.get('super_timing',False))
    args.timer_iterations=int(request.get('timer_iterations',20))
    args.parallel=False;args.fast_decoder_loop=True;args.precision='bf16';args.attn_implementation='sdpa'
    args.super_timing_fast_loop=bool(request.get('super_timing_fast_loop',True))
    compile_args(args,verbose=False);setup_inference_environment(args.seed)
    if not torch.cuda.is_available():raise RuntimeError('V32 timing requires CUDA')
    torch.set_num_threads(4);torch.cuda.reset_peak_memory_stats()
    started=time.perf_counter();loaded=time.perf_counter()
    key=(args.model_path,args.precision,args.attn_implementation,str(args.device))
    reused=resident is not None and resident.get('timing_only_key')==key
    if reused:model,tokenizer=resident['timing_only']
    else:
        model,tokenizer=load_model_with_server(args.model_path,args.train,args.device,max_batch_size=32,
            use_server=False,precision=args.precision,attn_implementation=args.attn_implementation,gamemode=3,auto_select_gamemode_model=False)
        if resident is not None:resident.update(timing_only_key=key,timing_only=(model,tokenizer))
    load_seconds=time.perf_counter()-loaded
    pre=Preprocessor(args);configuration,_=get_config(args)
    # Timing analysis consumes original channels before any channel mean can
    # cancel antiphase material. Resampling never changes the source clock.
    from scipy.io import wavfile
    from scipy.signal import resample_poly
    from malody_studio.beat_analysis import mono_audio
    import math
    original_sr,original=wavfile.read(request['audio'])
    if original.dtype.kind=='i':original=original.astype(np.float32)/float(2**(original.dtype.itemsize*8-1))
    elif original.dtype.kind=='u':original=(original.astype(np.float32)-128)/128
    if request.get('effective_end_sample') is not None:original=original[:request['effective_end_sample']]
    audio,mono_policy=mono_audio(original)
    if not len(audio) or np.all(audio==0):
        result={'beat_samples':[],'downbeat_samples':[],'reasons':['silence'],
                'provenance':{'adapter':'v32_super' if args.super_timing else 'v32_ordinary','reference_used':False,'rescue_used':False,'mono_policy':mono_policy},
                'cost':{'total_seconds':time.perf_counter()-started,'load_seconds':load_seconds}}
        atomic(Path(request['output'])/'worker-result.json',result);return result
    model_sr=args.train.data.sample_rate
    if original_sr!=model_sr:
        divisor=math.gcd(original_sr,model_sr);audio=resample_poly(audio,model_sr//divisor,original_sr//divisor).astype(np.float32)
    peak=np.max(np.abs(audio))
    if pre.normalize_audio and peak>0:audio=audio/peak
    if progress:progress('V32 独立节拍推理',20)
    if args.super_timing:
        events,times=SuperTimingGenerator(args,model,tokenizer).generate(audio,configuration,verbose=True)
    else:
        events,times=Processor(args,model,tokenizer).generate(sequences=pre.segment(audio),generation_config=configuration,
            in_context=[ContextType.NONE],out_context=[ContextType.TIMING],verbose=True)[0]
    # Read actual predicted BEAT/MEASURE markers. Redlines alone never imply
    # detected phase/meter, and upstream's fallback 4/4 is not accepted evidence.
    sr=request['sample_rate'];beat=[];down=[];tokens=[]
    step=1 if args.train.data.types_first else -1
    for i,event in enumerate(events):
        tokens.append({'type':str(event.type.value),'value':int(event.value)})
        j=i+step
        if event.type in (EventType.BEAT,EventType.MEASURE) and 0<=j<len(events) and events[j].type==EventType.TIME_SHIFT:
            sample=round(events[j].value*sr/1000)
            beat.append(sample)
            if event.type==EventType.MEASURE:down.append(sample)
    resolved={field:getattr(args,field) for field in ('seed','super_timing','timer_iterations','timer_num_beams',
        'timer_bpm_threshold','timer_cfg_scale','super_timing_fast_loop','parallel','fast_decoder_loop',
        'precision','max_batch_size','lookback','lookahead')}
    # Preserve the ordinary model's actual redlines for generation recovery.
    # These are unconfirmed model output, never certified detection evidence.
    from osuT5.osuT5.inference.postprocessor import Postprocessor
    native_timing = Postprocessor(args).generate_timing(events) if beat else []
    model_timing_points = [[tp.offset.total_seconds()*1000, 60000/tp.ms_per_beat]
                           for tp in native_timing if tp.parent is None and tp.ms_per_beat > 0]
    result={'beat_samples':sorted(set(beat)),'downbeat_samples':sorted(set(down)),
        'model_timing_points':model_timing_points,
        'provenance':{'adapter':'v32_super' if args.super_timing else 'v32_ordinary','resolved_config':resolved,'mono_policy':mono_policy,'analysis_range':[0,request.get('effective_end_sample')],
                      'training_overlap':'unknown','reference_used':False,'rescue_used':False},
        'raw_tokens':tokens,'reasons':['beat_level_unverified'],
        'cost':{'total_seconds':time.perf_counter()-started,'load_seconds':load_seconds,
                'peak_allocated_vram_mb':torch.cuda.max_memory_allocated()/1024**2},
        'resident':{'pid':os.getpid(),'model_reused':reused,'load_seconds':load_seconds,'work_seconds':time.perf_counter()-started}}
    atomic(Path(request['output'])/'worker-result.json',result)
    if progress:progress('V32 独立节拍完成',100)
    return result

def main(request_path=None, resident=None, progress=None):
    from malody_studio.workflow_log import attach_external, current_context, stage
    path=Path(request_path or sys.argv[1])
    request=json.loads(path.read_text(encoding='utf-8'))
    context=request.get('workflow_trace')
    attached=current_context()
    if attached and context and attached.get('path')==context.get('path') and attached.get('run_id')==context.get('run_id'):
        with stage('v32.worker_request',request_path=path,request_id=request.get('workflow_trace',{}).get('run_id'),
                   audio=request.get('audio'),output=request.get('output'),preset_count=len(request.get('presets',[]))):
            return _run_request(str(path),resident,progress)
    with attach_external(context,'v32_resident'):
        with stage('v32.worker_request',request_path=path,request_id=request.get('workflow_trace',{}).get('run_id'),
                   audio=request.get('audio'),output=request.get('output'),preset_count=len(request.get('presets',[]))):
            return _run_request(str(path),resident,progress)


if __name__ == '__main__':
    main()
