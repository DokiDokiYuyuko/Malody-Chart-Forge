"""Local file RPC for one serialized, persistent GPU owner across job processes."""
import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from .paths import ROOT

HOME = ROOT / 'cache' / 'resident-gpu'
IDLE_SECONDS = 900


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    for attempt in range(20):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 19: raise
            time.sleep(.05)


def read(path):
    try: return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError): return {}


def alive(pid):
    if not isinstance(pid, int) or pid <= 0: return False
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle: return False
        code = ctypes.c_ulong()
        try: return bool(kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))) and code.value == 259
        finally: kernel.CloseHandle(ctypes.c_void_p(handle))
    try: os.kill(pid, 0); return True
    except OSError: return False


@contextlib.contextmanager
def lease(timeout=3600, progress=None):
    HOME.mkdir(parents=True, exist_ok=True)
    stream = (HOME/'owner.lock').open('a+b')
    if (HOME/'owner.lock').stat().st_size==0:stream.write(b'0');stream.flush()
    stream.seek(0)
    start = time.monotonic(); notified = False;locked=False
    try:
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked=True
                break
            except OSError:
                if time.monotonic()-start >= timeout: raise RuntimeError('GPU 正在处理请求，请稍后再试')
                if progress and not notified: progress('等待常驻 GPU 工作进程', 10); notified = True
                time.sleep(.1)
        yield
    finally:
        try:
            if not locked:raise OSError('No lease held')
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
        except OSError: pass
        stream.close()


def code_version():
    paths=['tools/resident_worker.py','tools/mapperatorinator_worker.py','malody_studio/engine.py','malody_studio/resident.py','malody_studio/resident_mug.py']
    code=b''.join((ROOT/p).read_bytes() for p in paths)
    for name in ('models/mapperatorinator/v32-mania/model.safetensors','models/mapperatorinator/v32-timing/model.safetensors','models/mug-diffusion/v1.0.0/model.ckpt','models/mug-diffusion/v1.0.0/model.yaml'):
        try:
            stat=(ROOT/name).stat();code+=str((name,stat.st_size,stat.st_mtime_ns)).encode()
        except OSError:code+=name.encode()
    return hashlib.sha256(code).hexdigest()


def status():
    info=read(HOME/'state.json')
    return {**info, 'alive':alive(info.get('pid')), 'idle_timeout_seconds':IDLE_SECONDS}


def _stop(info):
    if not alive(info.get('pid')): return
    atomic(HOME/(info['session']+'.stop'), {})
    start=time.monotonic()
    while alive(info['pid']):
        if time.monotonic()-start>20: raise RuntimeError('常驻进程释放超时，请查看 resident-gpu 日志')
        time.sleep(.1)


def release(timeout=0):
    with lease(timeout): _stop(read(HOME/'state.json'))
    return status()


def call(engine, payload, progress=None, timeout=3600):
    if engine not in ('v32','mug'): raise ValueError('未知常驻模型')
    with lease(timeout, progress):
        version=code_version();info=read(HOME/'state.json')
        if not alive(info.get('pid')) or info.get('engine')!=engine or info.get('version')!=version:
            _stop(info)
            session=uuid.uuid4().hex
            python=ROOT/'runtime/mapperatorinator-venv/Scripts/python.exe' if engine=='v32' else Path(sys.executable)
            env=os.environ.copy();env.update(PYTHONUTF8='1',STARTRAIL_GPU_RESIDENT='1',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
            with (ROOT/'logs'/'resident-gpu.log').open('a',encoding='utf-8') as log:
                process=subprocess.Popen([str(python),'-u',str(ROOT/'tools/resident_worker.py'),engine,session,version],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            start=time.monotonic()
            while True:
                info=read(HOME/'state.json')
                if info.get('session')==session and info.get('phase')=='idle': break
                if process.poll() is not None: raise RuntimeError('常驻 GPU 进程启动失败，请查看 logs/resident-gpu.log')
                if time.monotonic()-start>60: raise RuntimeError('常驻 GPU 进程启动超时')
                time.sleep(.1)
        request_id=uuid.uuid4().hex
        atomic(HOME/(info['session']+'.request'),{'id':request_id,'payload':payload})
        output=HOME/(request_id+'.result');start=time.monotonic();previous=None
        while not output.exists():
            state=read(HOME/'state.json')
            current=(state.get('message'),state.get('percent'))
            if progress and state.get('request_id')==request_id and current[0] and current!=previous: progress(current[0],current[1] or 10);previous=current
            if not alive(info['pid']): raise RuntimeError('常驻 GPU 进程已中断；已有结果保留，请重试当前任务')
            if time.monotonic()-start>timeout:
                atomic(HOME/(info['session']+'.stop'),{})
                raise RuntimeError('GPU 请求超时；等待当前进程退出后再重试')
            time.sleep(.15)
        result=read(output);output.unlink(missing_ok=True)
        if result.get('error'): raise RuntimeError(result['error'])
        return result['result']


@contextlib.contextmanager
def external_gpu(progress=None):
    with lease(7200,progress):
        _stop(read(HOME/'state.json'))
        yield
