"""Persistent isolated CUDA owner. File RPC only; no network listener."""
import gc
import os
from pathlib import Path
import sys
import time
import traceback
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from malody_studio.resident import HOME,IDLE_SECONDS,atomic,read


def serve(engine,session,version):
    state={'pid':os.getpid(),'session':session,'engine':engine,'version':version,'phase':'idle','loaded':False,'started':time.time()}
    cache={};last=time.monotonic();handled=0
    def publish(**fields):
        state.update(fields,updated=time.time());atomic(HOME/'state.json',state)
    def progress(message,percent):publish(message=message,percent=percent)
    publish()
    request_path=HOME/(session+'.request');stop=HOME/(session+'.stop')
    try:
        while not stop.exists() and time.monotonic()-last<IDLE_SECONDS:
            if not request_path.exists():time.sleep(.1);continue
            request=read(request_path);request_path.unlink(missing_ok=True)
            if not request.get('id'):continue
            publish(phase='running',request_id=request['id'],message='准备常驻模型请求',percent=10)
            try:
                if engine=='v32':
                    from mapperatorinator_worker import main
                    request_file=Path(request['payload']['request_path']).resolve();request_file.relative_to(ROOT)
                    data=read(request_file)
                    for field in ('audio','output','timing_reference'):
                        if data.get(field):Path(data[field]).resolve().relative_to(ROOT)
                    result=main(str(request_file),cache,progress)
                else:result=mug(request['payload'],cache,progress)
                response={'result':result}
            except Exception as exc:
                traceback.print_exc();cache.clear();gc.collect()
                if 'torch' in sys.modules:
                    import torch
                    torch.cuda.empty_cache()
                response={'error':str(exc)}
            handled+=1;atomic(HOME/(request['id']+'.result'),response)
            last=time.monotonic()
            gpu={}
            if 'torch' in sys.modules:
                import torch
                if torch.cuda.is_available():gpu={'allocated_vram_mb':round(torch.cuda.memory_allocated()/1024**2,1),'reserved_vram_mb':round(torch.cuda.memory_reserved()/1024**2,1),'device_name':torch.cuda.get_device_name(0)}
            publish(phase='idle',loaded=bool(cache),requests=handled,message='模型驻留 GPU，等待下一任务' if cache else '模型未加载，等待下一任务',percent=100,**gpu)
    finally:
        cache.clear();gc.collect()
        if 'torch' in sys.modules:
            import torch
            torch.cuda.empty_cache()
        stop.unlink(missing_ok=True)
        publish(phase='stopped',loaded=False,message='显存已释放',percent=0,allocated_vram_mb=0,reserved_vram_mb=0)


def mug(payload,cache,progress):
    import numpy as np
    import torch
    from malody_studio.engine import Engine
    for field in ('input','output'):
        if field in payload:Path(payload[field]).resolve().relative_to(ROOT)
    if not torch.cuda.is_available():raise RuntimeError('常驻 MuG 需要可用 CUDA，不自动转 CPU')
    reused='mug' in cache
    if not reused:cache['mug']=Engine()
    model=cache['mug'];started=time.monotonic();model.load(progress);load_seconds=time.monotonic()-started
    torch.cuda.reset_peak_memory_stats();started=time.monotonic()
    if payload['action']=='prepare':
        wave=model.prepare(np.load(payload['input'],allow_pickle=False),payload['sr'],progress)
        parts=wave if isinstance(wave,(list,tuple)) else [wave]
        with Path(payload['output']).open('wb') as stream:np.savez(stream,**{'wave_'+str(i):part.detach().cpu().numpy() for i,part in enumerate(parts)})
        result={'z_length':model.model.z_length,'wave_kind':'list' if isinstance(wave,(list,tuple)) else 'tensor'}
        del wave
    elif payload['action']=='generate':
        model.model.z_length=payload['z_length']
        with np.load(payload['input'],allow_pickle=False) as archive:
            parts=[torch.from_numpy(archive['wave_'+str(i)]).to(model.device) for i in range(len(archive.files))]
        wave=parts if payload.get('wave_kind')=='list' else parts[0]
        notes=model.generate(wave,None,payload['options'],lambda fraction:progress('MuG 常驻推理',20+fraction*65))
        result={'notes':[[n.start,n.lane,n.end] for n in notes]};del wave
    else:raise ValueError('未知 MuG 操作')
    result['resident']={'pid':os.getpid(),'model_reused':reused,'load_seconds':round(load_seconds,4),'work_seconds':round(time.monotonic()-started,4),'peak_allocated_vram_mb':round(torch.cuda.max_memory_allocated()/1024**2,1)}
    return result


if __name__=='__main__':serve(*sys.argv[1:4])
