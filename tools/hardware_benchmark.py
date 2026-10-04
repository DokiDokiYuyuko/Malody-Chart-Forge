from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from malody_studio.inference_policy import V32_INFERENCE_POLICY
GIB=1024**3
DIFFICULTIES=['easy','medium','hard','expert','master','lunatic']


def atomic_json(path:Path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(path)


def worker(request_path:Path):
    request=json.loads(request_path.read_text(encoding='utf-8'))
    output=Path(request['output']).resolve();output.mkdir(parents=True,exist_ok=True)
    events=[];started=time.monotonic()
    def progress(message,percent):
        events.append({'seconds':round(time.monotonic()-started,2),'message':message,'progress':round(float(percent),1)})
    import torch
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats();device=torch.cuda.get_device_name(0)
    else:device='cpu'
    from malody_studio.pipeline import run
    report,archive=run(Path(request['audio']),output,request['options'],progress)
    memory={'allocated_peak_bytes':int(torch.cuda.max_memory_allocated()) if torch.cuda.is_available() else 0,
            'reserved_peak_bytes':int(torch.cuda.max_memory_reserved()) if torch.cuda.is_available() else 0}
    atomic_json(output/'worker-result.json',{'success':True,'device_name':device,'elapsed_seconds':round(time.monotonic()-started,2),
        'events':events,'memory':memory,'report':report,'archive':str(archive)})


def gpu_sample():
    try:
        raw=subprocess.run(['nvidia-smi','--query-gpu=name,driver_version,memory.total,memory.used',
            '--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=5,check=True).stdout.strip().splitlines()[0]
        name,driver,total,used=[value.strip() for value in raw.split(',')[:4]]
        return {'name':name,'driver_version':driver,'total_bytes':int(total)*1024**2,'used_bytes':int(used)*1024**2}
    except (OSError,subprocess.SubprocessError,ValueError,IndexError):return None


def options_for(audio:Path,engine:str,seed:int):
    return {'title':audio.stem[:120] or 'Benchmark','artist':'Local benchmark','difficulties':DIFFICULTIES,
        'ln_ratio':.15,'steps':50,'seed':seed,'bpm':None,'engine':engine,'difficulty_rules':{},
        'pattern':'balanced','pattern_strength':20,'mug_difficulty':8,'mug_style':'ranked',
        'mug_guidance':1.5,'mug_eta':0,'v32_difficulty':8,'v32_temperature':.9,'v32_top_p':.9,
        'v32_column_temperature':.8,'v32_cfg_scale':1,'v32_year':2024,
        'v32_descriptors':[],'v32_negative_descriptors':[],'artwork_video_id':None}


def artifact_valid(output:Path):
    archive=output/'malody-4k.mcz'
    if not archive.is_file():return False,'MCZ 文件不存在'
    try:
        with zipfile.ZipFile(archive) as package:
            if package.testzip() is not None:return False,'ZIP 完整性检查失败'
            names=set(package.namelist())
            required={'0/','0/audio.ogg'}|{f'0/{key}.mc' for key in DIFFICULTIES}
            if not required.issubset(names):return False,'曲包缺少音频或难度文件'
            for key in DIFFICULTIES:
                json.loads(package.read(f'0/{key}.mc'))
        return True,'通过 ZIP、音频引用和六档文件检查'
    except (OSError,ValueError,KeyError,zipfile.BadZipFile) as exc:return False,str(exc)


def run_case(audio:Path,engine:str,output:Path,timeout:int):
    output.mkdir(parents=True,exist_ok=True)
    request_path=output/'request.json'
    atomic_json(request_path,{'audio':str(audio.resolve()),'output':str(output.resolve()),
        'options':options_for(audio,engine,int(uuid.uuid4().int%(2**31-1)))})
    result_path=output/'worker-result.json';log_path=output/'worker.log'
    env=os.environ.copy();env['PYTHONUTF8']='1';env['STARTRAIL_RESIDENT_DISABLED']='1'
    process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker',str(request_path)],
        cwd=ROOT,env=env,stdout=log_path.open('w',encoding='utf-8'),stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    samples=[];started=time.monotonic()
    while process.poll() is None:
        sample=gpu_sample()
        if sample:samples.append(sample)
        if time.monotonic()-started>timeout:
            process.kill();process.wait();break
        time.sleep(.6)
    elapsed=round(time.monotonic()-started,2)
    result=None
    try:result=json.loads(result_path.read_text(encoding='utf-8'))
    except (OSError,ValueError):pass
    valid,detail=artifact_valid(output)
    gpu_peak=max((item['used_bytes'] for item in samples),default=0)
    total=max((item['total_bytes'] for item in samples),default=0)
    driver=samples[0]['driver_version'] if samples else None
    return {'success':process.returncode==0 and bool(result),'artifact_valid':valid,
        'artifact_detail':detail,'audio':str(audio),'duration_seconds':result.get('report',{}).get('duration') if result else None,
        'elapsed_seconds':elapsed,'stage_events':result.get('events',[]) if result else [],
        'memory':result.get('memory',{}) if result else {},'gpu_peak_used_bytes':gpu_peak,
        'min_free_vram_bytes':max(0,total-gpu_peak) if total else 0,'driver_version':driver,
        'model_version':result.get('report',{}).get('engine') if result else engine,
        'difficulty_count':len(result.get('report',{}).get('difficulties',[])) if result else 0,
        'error':None if process.returncode==0 else log_path.read_text(encoding='utf-8',errors='replace')[-4000:]}


def run_pair(engines,audio,output,timeout):
    output.mkdir(parents=True,exist_ok=True);processes=[];logs=[];results=[];samples=[];started=time.monotonic()
    for index,engine in enumerate(engines):
        folder=output/f'task-{index+1}-{engine}';folder.mkdir(parents=True,exist_ok=True)
        request_path=folder/'request.json'
        atomic_json(request_path,{'audio':str(audio.resolve()),'output':str(folder.resolve()),
            'options':options_for(audio,engine,int(uuid.uuid4().int%(2**31-1)))})
        log=(folder/'worker.log').open('w',encoding='utf-8');logs.append(log)
        env=os.environ.copy();env['PYTHONUTF8']='1';env['STARTRAIL_RESIDENT_DISABLED']='1'
        process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker',str(request_path)],
            cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        processes.append(process)
    timed_out=False
    while any(process.poll() is None for process in processes):
        sample=gpu_sample()
        if sample:samples.append(sample)
        if time.monotonic()-started>timeout:
            timed_out=True
            for process in processes:
                if process.poll() is None:process.kill()
            break
        time.sleep(.6)
    for process in processes:process.wait()
    for log in logs:log.close()
    elapsed=round(time.monotonic()-started,2)
    for index,(engine,process) in enumerate(zip(engines,processes)):
        folder=output/f'task-{index+1}-{engine}'
        try:result=json.loads((folder/'worker-result.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):result=None
        valid,detail=artifact_valid(folder)
        results.append({'success':process.returncode==0 and bool(result),'artifact_valid':valid,
            'artifact_detail':detail,'engine':engine,'elapsed_seconds':result.get('elapsed_seconds') if result else None,
            'memory':result.get('memory',{}) if result else {},'difficulty_count':len(result.get('report',{}).get('difficulties',[])) if result else 0,
            'error':None if process.returncode==0 else (folder/'worker.log').read_text(encoding='utf-8',errors='replace')[-3000:]})
    gpu_peak=max((item['used_bytes'] for item in samples),default=0);total=max((item['total_bytes'] for item in samples),default=0)
    return {'success':not timed_out and all(item['success'] and item['artifact_valid'] for item in results),
        'artifacts_valid':all(item['artifact_valid'] for item in results),'tasks':results,'elapsed_seconds':elapsed,
        'gpu_peak_used_bytes':gpu_peak,'min_free_vram_bytes':max(0,total-gpu_peak) if total else 0,
        'driver_version':samples[0]['driver_version'] if samples else None,'timed_out':timed_out}


def main():
    parser=argparse.ArgumentParser(description='MuG / V32 本机生成与双任务显存基准。需要提供可测试的本地音乐文件。')
    parser.add_argument('--short',type=Path,help='5–60 秒音频')
    parser.add_argument('--medium',type=Path,help='1–3 分钟音频')
    parser.add_argument('--long',type=Path,help='3–10 分钟音频')
    parser.add_argument('--output-dir',type=Path,default=ROOT/'benchmarks')
    parser.add_argument('--single-timeout',type=int,default=1800)
    parser.add_argument('--pair-timeout',type=int,default=2400)
    parser.add_argument('--skip-parallel',action='store_true',help='只运行单任务覆盖，不会开启并行资格')
    parser.add_argument('--worker',type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.worker:
        worker(args.worker);return 0
    inputs={'short':args.short,'medium':args.medium,'long':args.long}
    if any(path is None for path in inputs.values()):parser.error('请为短、中、长测试各提供一条音频，例如 --short short.wav --medium medium.wav --long long.wav')
    for label,path in inputs.items():
        if not path.is_file():parser.error(f'{label} 音频不存在：{path}')
    import soundfile as sf
    expected={'short':(5,60),'medium':(60,180),'long':(180,600)}
    for label,path in inputs.items():
        duration=float(sf.info(path).duration);low,high=expected[label]
        if not low<=duration<=high:parser.error(f'{label} 音频时长为 {duration:.1f} 秒，应在 {low}–{high} 秒内')
    stamp=time.strftime('%Y%m%d-%H%M%S');root=args.output_dir.resolve()/stamp;root.mkdir(parents=True,exist_ok=True)
    identity=gpu_sample() or {}
    if not identity:
        print('nvidia-smi 不可用；单任务仍会执行，但并行显存余量无法通过资格检查。',file=sys.stderr)
    singles={}
    for engine in ('mug','v32'):
        for length,audio in inputs.items():
            key=f'{engine}:{length}';print(f'[{len(singles)+1}/6] {engine.upper()} · {length}: {audio}')
            singles[key]=run_case(audio,engine,root/'single'/engine/length,args.single_timeout)
            if identity.get('driver_version') is None and singles[key].get('driver_version'):identity['driver_version']=singles[key]['driver_version']
    parallel_tests={}
    if not args.skip_parallel:
        for key,engines in [('mug+mug',('mug','mug')),('mug+v32',('mug','v32')),('v32+v32',('v32','v32'))]:
            print(f'双任务压力测试：{key}')
            pair=run_pair(engines,inputs['short'],root/'parallel'/key,args.pair_timeout)
            serial_baseline=sum((singles.get(f'{engine}:short',{}).get('elapsed_seconds') or 0) for engine in engines)
            pair['serial_baseline_seconds']=round(serial_baseline,2)
            pair['throughput_speedup']=round(serial_baseline/max(.01,pair['elapsed_seconds']),3)
            pair['faster_than_serial']=pair['throughput_speedup']>=1.05
            parallel_tests[key]=pair
            if identity.get('driver_version') is None and pair.get('driver_version'):identity['driver_version']=pair['driver_version']
    all_singles=all(item['success'] and item['artifact_valid'] and item['difficulty_count']==6 for item in singles.values())
    all_pairs=bool(parallel_tests) and all(item['success'] and item['artifacts_valid'] and item['min_free_vram_bytes']>=2*GIB for item in parallel_tests.values())
    result={'created':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'device_name':identity.get('name'),
        'driver_version':identity.get('driver_version'),'gpu_memory_total_bytes':identity.get('total_bytes'),
        'v32_inference_policy':V32_INFERENCE_POLICY,
        'single_tests':singles,'parallel_tests':parallel_tests,'single_tests_passed':all_singles,
        'parallel_ready':all_singles and all_pairs,'parallel_gate_min_free_vram_bytes':min((x['min_free_vram_bytes'] for x in parallel_tests.values()),default=0),
        'model_versions':{'mug':'MuG Diffusion v1.0.0','v32':'Mapperatorinator V32 mania'},
        'scope':'本机实际生成，含六档曲包校验；并行选项仅在所有同模型/跨模型组合通过且显存余量不少于 2 GiB 时开放。'}
    atomic_json(root/'benchmark.json',result)
    atomic_json(args.output_dir.resolve()/'latest.json',result)
    print(f"基准报告已保存：{root/'benchmark.json'}")
    print('并行资格：' + ('通过' if result['parallel_ready'] else '未通过或未完成；队列保持串行默认'))
    return 0 if all_singles else 2

if __name__=='__main__':
    raise SystemExit(main())
