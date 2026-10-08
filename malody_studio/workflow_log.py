"""Append-only, per-job execution trace for diagnosing generation failures.

Telemetry is best-effort: a logging failure must never fail chart generation.
Each JSONL line is self-contained so partial traces remain useful after a crash.
"""
from __future__ import annotations

import contextlib
import contextvars
import ctypes
import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

_CURRENT = contextvars.ContextVar('malody_workflow_trace', default=None)
_GPU_CACHE = {'time': 0.0, 'value': None}
_GPU_LOCK = threading.Lock()
_HOST_CPU_LAST = None


def _windows_snapshot():
    global _HOST_CPU_LAST
    if os.name != 'nt':
        raise OSError('Windows telemetry unavailable')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)

    class FILETIME(ctypes.Structure):
        _fields_ = [('low', ctypes.c_ulong), ('high', ctypes.c_ulong)]

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                    ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                    ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]

    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong),
                    ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                    ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t),
                    ('PrivateUsage', ctypes.c_size_t)]

    class SYSTEM_INFO(ctypes.Structure):
        _fields_ = [('idle', FILETIME), ('kernel', FILETIME), ('user', FILETIME)]

    def ft(value):
        return (int(value.high) << 32) | int(value.low)

    memory = MEMORYSTATUSEX(); memory.dwLength = ctypes.sizeof(memory)
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise ctypes.WinError(ctypes.get_last_error())
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.GetProcessTimes.argtypes = [ctypes.c_void_p, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
                                       ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), ctypes.c_ulong]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    handle = kernel.GetCurrentProcess()
    counters = PROCESS_MEMORY_COUNTERS_EX(); counters.cb = ctypes.sizeof(counters)
    process_row = {'pid': os.getpid()}
    if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        process_row.update(rss_bytes=int(counters.WorkingSetSize), private_bytes=int(counters.PrivateUsage),
                           peak_rss_bytes=int(counters.PeakWorkingSetSize), page_faults=int(counters.PageFaultCount))
    creation, exit_time, kernel_time, user_time = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    if kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                              ctypes.byref(kernel_time), ctypes.byref(user_time)):
        process_row['cpu_seconds'] = (ft(kernel_time) + ft(user_time)) / 10_000_000
    idle, kernel_time, user_time = FILETIME(), FILETIME(), FILETIME()
    host_cpu_pct = None
    if kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel_time), ctypes.byref(user_time)):
        current = (ft(idle), ft(kernel_time), ft(user_time))
        if _HOST_CPU_LAST is not None:
            old = _HOST_CPU_LAST
            total = sum(current[i] - old[i] for i in range(3))
            active = (current[1] - old[1]) + (current[2] - old[2]) - (current[0] - old[0])
            if total > 0:
                host_cpu_pct = max(0., min(100., 100 * active / total))
        _HOST_CPU_LAST = current
    result = {'process_tree': {'members': [process_row], 'cpu_seconds': process_row.get('cpu_seconds'),
                               'rss_bytes': process_row.get('rss_bytes'), 'private_bytes': process_row.get('private_bytes'),
                               'fallback': 'Win32 GetProcessTimes/GetProcessMemoryInfo; child process CPU in os.times'},
              'host': {'cpu_percent': host_cpu_pct, 'memory_total_bytes': int(memory.ullTotalPhys),
                       'memory_available_bytes': int(memory.ullAvailPhys),
                       'memory_used_bytes': int(memory.ullTotalPhys - memory.ullAvailPhys),
                       'memory_percent': float(memory.dwMemoryLoad)}}
    return result


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _safe(value, depth=0):
    """Make log payloads JSON-safe and avoid leaking credentials."""
    if depth > 8:
        return '<max-depth>'
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float('inf') else str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = str(key)
            if any(word in name.lower() for word in ('password', 'secret', 'token', 'api_key', 'authorization')):
                result[name] = '<redacted>'
            else:
                result[name] = _safe(item, depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_safe(item, depth + 1) for item in list(value)[:1000]]
    try:
        import numpy as np
        if isinstance(value, np.ndarray):
            return {'type': 'ndarray', 'shape': list(value.shape), 'dtype': str(value.dtype),
                    'size': int(value.size)}
        if isinstance(value, np.generic):
            return _safe(value.item(), depth + 1)
    except Exception:
        pass
    return repr(value)[:2000]


def _gpu_snapshot():
    now = time.monotonic()
    with _GPU_LOCK:
        if now - _GPU_CACHE['time'] < 2:
            return _GPU_CACHE['value']
        try:
            query = ('index,name,utilization.gpu,utilization.memory,memory.total,memory.used,'
                     'memory.free,temperature.gpu,power.draw,clocks.gr,clocks.mem')
            proc = subprocess.run(['nvidia-smi', f'--query-gpu={query}',
                                   '--format=csv,noheader,nounits'], capture_output=True,
                                  text=True, timeout=1.5, check=True)
            rows = []
            for line in proc.stdout.splitlines():
                fields = [part.strip() for part in line.split(',')]
                if len(fields) != 11:
                    continue
                def number(value):
                    try:
                        return float(value)
                    except ValueError:
                        return None
                rows.append({'index': fields[0], 'name': fields[1], 'utilization_gpu_pct': number(fields[2]),
                             'utilization_memory_pct': number(fields[3]), 'memory_total_mib': number(fields[4]),
                             'memory_used_mib': number(fields[5]), 'memory_free_mib': number(fields[6]),
                             'temperature_c': number(fields[7]), 'power_w': number(fields[8]),
                             'clock_graphics_mhz': number(fields[9]), 'clock_memory_mhz': number(fields[10])})
            apps = subprocess.run(['nvidia-smi', '--query-compute-apps=pid,process_name,used_memory',
                                   '--format=csv,noheader,nounits'], capture_output=True,
                                  text=True, timeout=1.5, check=True)
            app_rows = []
            for line in apps.stdout.splitlines():
                fields = [part.strip() for part in line.split(',', 2)]
                if len(fields) == 3:
                    try:
                        pid = int(fields[0]); memory = float(fields[2])
                    except ValueError:
                        continue
                    app_rows.append({'pid': pid, 'process': fields[1], 'memory_mib': memory})
            value = {'available': True, 'devices': rows, 'compute_processes': app_rows}
        except Exception as exc:
            value = {'available': False, 'error': f'{type(exc).__name__}: {exc}'}
        _GPU_CACHE.update(time=now, value=value)
        return value


def resource_snapshot():
    """Capture process, host and NVIDIA device resources without new dependencies."""
    result = {'pid': os.getpid(), 'thread_id': threading.get_ident(), 'platform': platform.platform()}
    try:
        import psutil
        process = psutil.Process(os.getpid())
        members = [process, *process.children(recursive=True)]
        cpu_seconds = 0.0; rss = 0; vms = 0; rows = []
        for item in members:
            try:
                times = item.cpu_times(); memory = item.memory_info()
                cpu = float(times.user + times.system); cpu_seconds += cpu
                rss += int(memory.rss); vms += int(memory.vms)
                rows.append({'pid': item.pid, 'name': item.name(), 'cpu_seconds': round(cpu, 4),
                             'rss_bytes': int(memory.rss), 'vms_bytes': int(memory.vms)})
            except (psutil.Error, OSError):
                continue
        vm = psutil.virtual_memory()
        result.update(process_tree={'members': rows, 'cpu_seconds': round(cpu_seconds, 4),
                                    'rss_bytes': rss, 'vms_bytes': vms},
                      host={'cpu_percent': psutil.cpu_percent(interval=None),
                            'memory_total_bytes': int(vm.total), 'memory_available_bytes': int(vm.available),
                            'memory_used_bytes': int(vm.used), 'memory_percent': float(vm.percent)})
    except Exception as exc:
        times = os.times()
        try:
            native = _windows_snapshot()
        except Exception as native_exc:
            native = {'process_tree': {}, 'host': {},
                      'error': f'{type(native_exc).__name__}: {native_exc}'}
        if native.get('process_tree'):
            native['process_tree'].update(children_cpu_seconds=round(times.children_user + times.children_system, 4),
                                          os_times_fallback=True)
            result.update(native)
        else:
            result['process_tree'] = {'cpu_seconds': round(times.user + times.system, 4),
                                      'children_cpu_seconds': round(times.children_user + times.children_system, 4),
                                      'fallback': 'os.times',
                                      'error': f'{type(exc).__name__}: {exc}'}
            result['host'] = {'fallback': 'psutil and Win32 telemetry unavailable',
                              'error': native.get('error')}
    result['gpu'] = _gpu_snapshot()
    return result


def _peak_values(snapshot):
    process=snapshot.get('process_tree',{});host=snapshot.get('host',{});gpus=snapshot.get('gpu',{}).get('devices',[])
    return {'process_rss_bytes':process.get('rss_bytes'),'process_vms_bytes':process.get('vms_bytes'),
            'process_private_bytes':process.get('private_bytes'),'host_memory_used_bytes':host.get('memory_used_bytes'),
            'host_cpu_percent':host.get('cpu_percent'),
            'gpu_memory_used_mib':max((row.get('memory_used_mib') or 0 for row in gpus),default=None),
            'gpu_utilization_pct':max((row.get('utilization_gpu_pct') or 0 for row in gpus),default=None),
            'gpu_temperature_c':max((row.get('temperature_c') or 0 for row in gpus),default=None),
            'gpu_power_w':max((row.get('power_w') or 0 for row in gpus),default=None)}


def _merge_peaks(*sets):
    keys={key for row in sets if row for key in row}
    return {key:max((row[key] for row in sets if row and isinstance(row.get(key),(int,float))),default=None)
            for key in keys}


def _cpu_total(snapshot):
    values=snapshot.get('process_tree',{})
    total=values.get('cpu_seconds') or 0
    total+=values.get('children_cpu_seconds') or 0
    return float(total)


def file_identity(path, hash_file=False):
    path = Path(path)
    record = {'path': str(path.resolve())}
    try:
        stat = path.stat()
        record.update(exists=True, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)
        if hash_file and path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            record['sha256'] = digest.hexdigest()
    except OSError as exc:
        record.update(exists=False, error=f'{type(exc).__name__}: {exc}')
    return record


class WorkflowTrace:
    def __init__(self, directory, task_id=None, identity=None, sample_seconds=5.0):
        self.directory = Path(directory)
        self.log_dir = self.directory / 'debug-log'
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / 'workflow.jsonl'
        self.task_id = task_id or self.directory.name
        self.run_id = uuid.uuid4().hex
        self.role = 'queue_worker'
        self.started_monotonic = time.monotonic()
        self.started_utc = _utc()
        self.sample_seconds = sample_seconds
        self._lock = threading.RLock()
        self._active = {}
        self._stop = threading.Event()
        self._monitor = None
        self._nodes = []
        self._record('run_start', 'workflow', identity=identity or {}, resources=resource_snapshot(),
                     runtime={'python': platform.python_version(), 'executable': os.path.abspath(sys.executable),
                              'cwd': os.getcwd(), 'pid': os.getpid()})
        self._monitor = threading.Thread(target=self._sample_loop, name='workflow-telemetry', daemon=True)
        self._monitor.start()

    def _record(self, event, node, **fields):
        row = {'schema': 'malody-workflow-trace-v1', 'event': event, 'node': node,
               'task_id': self.task_id, 'run_id': self.run_id, 'timestamp_utc': _utc(),
               'monotonic_seconds': round(time.monotonic() - self.started_monotonic, 6),
               'pid': os.getpid(), 'process_role': getattr(self,'role',_ROLE.get()), **_safe(fields)}
        try:
            data = (json.dumps(row, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
            # One append write per event keeps each JSONL record independent and crash-readable.
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        except Exception:
            # Diagnostics are deliberately non-fatal to generation.
            return False
        return True

    def event(self, event, node, **fields):
        return self._record(event, node, **fields)

    @contextlib.contextmanager
    def stage(self, name, **details):
        node_id = uuid.uuid4().hex
        parent_id = _CURRENT.get()
        started = time.monotonic()
        before = resource_snapshot()
        with self._lock:
            self._active[node_id] = {'node': name, 'parent_id': parent_id, 'thread_id': threading.get_ident(),
                                     'started_monotonic': started, 'peak_resources': _peak_values(before)}
            self._nodes.append({'node_id': node_id, 'node': name, 'parent_id': parent_id})
        token = _CURRENT.set(node_id)
        self._record('node_start', name, node_id=node_id, parent_id=parent_id, details=details, resources=before)
        status = 'completed'; error = None
        try:
            yield node_id
        except BaseException as exc:
            status = 'failed'
            error = {'type': type(exc).__name__, 'message': str(exc),
                     'traceback': traceback.format_exc()}
            raise
        finally:
            after = resource_snapshot()
            elapsed = time.monotonic() - started
            with self._lock:
                active_state=self._active.pop(node_id, {})
            cpu_seconds=max(0.,_cpu_total(after)-_cpu_total(before))
            self._record('node_end' if status == 'completed' else 'node_error', name,
                         node_id=node_id, parent_id=parent_id, status=status,
                         elapsed_seconds=round(elapsed, 6), cpu_seconds=round(cpu_seconds,6),
                         cpu_percent_one_core=round(cpu_seconds/max(elapsed,1e-9)*100,2),
                         cpu_percent_of_host_capacity=round(cpu_seconds/max(elapsed*max(1,os.cpu_count() or 1),1e-9)*100,2),
                         resources_before=before,resources_after=after,
                         resource_peaks=_merge_peaks(active_state.get('peak_resources'),_peak_values(after)),error=error)
            _CURRENT.reset(token)

    def _sample_loop(self):
        while not self._stop.wait(self.sample_seconds):
            try:
                with self._lock:
                    active = [{'node_id': key, **{k:v for k,v in value.items() if k!='peak_resources'}}
                              for key, value in self._active.items()]
                if active:
                    sample=resource_snapshot();peaks=_peak_values(sample)
                    with self._lock:
                        for state in self._active.values():
                            state['peak_resources']=_merge_peaks(state.get('peak_resources'),peaks)
                    self._record('resource_sample', active[-1]['node'], active_nodes=active,
                                 resources=sample,resource_peaks=peaks)
            except Exception as exc:
                self._record('telemetry_error', 'resource_sampler', error=f'{type(exc).__name__}: {exc}')

    def close(self, status='completed', **details):
        self._stop.set()
        if self._monitor and self._monitor is not threading.current_thread():
            self._monitor.join(timeout=2)
        end_resources = resource_snapshot()
        self._record('run_end', 'workflow', status=status,
                     elapsed_seconds=round(time.monotonic() - self.started_monotonic, 6),
                     started_utc=self.started_utc, resources=end_resources,
                     node_count=len(self._nodes), nodes=self._nodes, details=details)


class _NullTrace:
    path = ''
    def __init__(self, task_id):
        self.task_id=task_id;self.run_id='logging-unavailable'
    @contextlib.contextmanager
    def stage(self,name,**details):
        yield None
    def event(self,event,node,**fields):
        return False
    def close(self,status='completed',**details):
        return None


@contextlib.contextmanager
def trace_job(directory, task_id=None, identity=None, sample_seconds=5.0):
    try:
        trace = WorkflowTrace(directory, task_id, identity, sample_seconds)
    except Exception as exc:
        print(f'workflow debug telemetry unavailable: {type(exc).__name__}: {exc}',file=sys.stderr,flush=True)
        trace = _NullTrace(task_id or Path(directory).name)
    token = _TRACE.set(trace)
    status = 'completed'; details = {}
    try:
        yield trace
    except BaseException as exc:
        status = 'failed'; details = {'error_type': type(exc).__name__, 'error': str(exc),
                                      'traceback': traceback.format_exc()}
        raise
    finally:
        trace.close(status, **details)
        _TRACE.reset(token)


_TRACE = contextvars.ContextVar('malody_workflow_trace_instance', default=None)
_ROLE = contextvars.ContextVar('malody_workflow_process_role', default='queue_worker')


@contextlib.contextmanager
def stage(name, **details):
    trace = _TRACE.get()
    if trace is None:
        yield None
    else:
        with trace.stage(name, **details) as node_id:
            yield node_id


def event(event_name, node, **details):
    trace = _TRACE.get()
    if trace is not None:
        trace.event(event_name, node, parent_id=_CURRENT.get(), details=details)


def current_path():
    trace = _TRACE.get()
    return str(trace.path) if trace is not None else None


def current_context():
    trace = _TRACE.get()
    if trace is None:
        return None
    return {'path': str(trace.path), 'task_id': trace.task_id, 'run_id': trace.run_id}


def append_event(path, task_id, event_name, node, **details):
    """Append a lifecycle event from the service process after worker exit."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    run_id = None
    try:
        with path.open('r', encoding='utf-8') as stream:
            for line in stream:
                first = json.loads(line)
                run_id = first.get('run_id')
                if run_id:
                    break
    except (OSError, ValueError):
        pass
    row = {'schema': 'malody-workflow-trace-v1', 'event': event_name, 'node': node,
           'task_id': task_id, 'run_id': run_id, 'timestamp_utc': _utc(),
           'monotonic_seconds': None, 'pid': os.getpid(), 'process_role': 'studio_service',
           'details': _safe(details)}
    try:
        data = (json.dumps(row, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except Exception:
        return False
    return True


def context_for_file(path, task_id):
    path=Path(path)
    try:
        with path.open('r',encoding='utf-8') as stream:
            for line in stream:
                row=json.loads(line)
                if row.get('run_id'):
                    return {'path':str(path),'task_id':task_id,'run_id':row['run_id']}
    except (OSError,ValueError):
        return None
    return None


@contextlib.contextmanager
def attach_external(context, role):
    """Append node records from a resident/model process to the owning job log."""
    if not isinstance(context, dict) or not context.get('path'):
        yield None
        return
    trace = object.__new__(WorkflowTrace)
    trace.path = Path(context['path']); trace.task_id = context.get('task_id', 'unknown')
    trace.run_id = context.get('run_id', 'unknown'); trace.started_monotonic = time.monotonic()
    trace.role = role
    trace.started_utc = _utc(); trace.sample_seconds = 0; trace._lock = threading.RLock()
    trace._active = {}; trace._stop = threading.Event(); trace._monitor = None; trace._nodes = []
    token = _TRACE.set(trace); role_token = _ROLE.set(role)
    trace.sample_seconds=5.0
    trace._monitor=threading.Thread(target=trace._sample_loop,name='external-workflow-telemetry',daemon=True)
    trace._monitor.start()
    try:
        yield trace
    finally:
        trace._stop.set()
        trace._monitor.join(timeout=2)
        _ROLE.reset(role_token); _TRACE.reset(token)
