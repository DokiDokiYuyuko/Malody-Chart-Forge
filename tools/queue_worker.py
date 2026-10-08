import json
import os
import sys
import time
import uuid
from pathlib import Path
from importlib import metadata as package_metadata

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
request_path=Path(sys.argv[1]).resolve()
request=json.loads(request_path.read_text(encoding='utf-8'))
directory=Path(request['directory']).resolve()
os.environ['STARTRAIL_JOB_DIRECTORY']=str(directory)
progress_path=directory/'worker-progress.json'
result_path=directory/'worker-result.json'

def write_result(path, data, optional=False):
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    try:
        for attempt in range(10):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt==9:
                    if optional:return
                    raise
                time.sleep(.025*(attempt+1))
    finally:
        temporary.unlink(missing_ok=True)

def progress(message,percent):
    # Windows readers may briefly hold the old file. Telemetry must not abort inference.
    write_result(progress_path,{'message':message,'progress':round(float(percent),1)},optional=True)

try:
    from malody_studio.workflow_log import trace_job, stage, file_identity
    advanced = request['options'].get('_advanced') or {}
    task_type = advanced.get('task_type', 'generation')
    code_files=('tools/queue_worker.py','malody_studio/workflow_log.py','malody_studio/pipeline.py',
                'malody_studio/direct_v32.py','malody_studio/advanced_generation.py','malody_studio/stem_generation.py',
                'malody_studio/music_timing.py','malody_studio/beat_analysis.py','malody_studio/mapperatorinator.py',
                'malody_studio/v32_event_serialization.py','malody_studio/v32_rhythm.py',
                'malody_studio/resident.py','tools/resident_worker.py','tools/mapperatorinator_worker.py',
                'tools/beat_analysis_worker.py','malody_studio/separation.py','tools/separation_worker.py','tools/roformer_worker.py')
    package_versions={}
    for package in ('numpy','torch','librosa','fastapi','soundfile'):
        try:package_versions[package]=package_metadata.version(package)
        except package_metadata.PackageNotFoundError:package_versions[package]=None
    with trace_job(directory, task_id=directory.name,
                   identity={'task_type':task_type,'source':file_identity(request['source'],hash_file=True),
                             'options':request['options'],'request_file':file_identity(request_path,hash_file=True),
                             'environment':{'python':sys.version,'platform':sys.platform,
                                            'python_executable':sys.executable,'package_versions':package_versions,
                                            'cpu_count':os.cpu_count(),
                                            'dependency_lock':file_identity(ROOT/'requirements-lock.txt',hash_file=True),
                                            'code_files':{name:file_identity(ROOT/name,hash_file=True) for name in code_files},
                                            'workflow_log':'debug-log/workflow.jsonl'}}) as trace:
        os.environ['STARTRAIL_WORKFLOW_LOG']=str(trace.path)
        with stage('queue_worker.resolve_task', task_type=task_type, source=request['source']):
            if request['options'].get('_advanced'):
                if task_type == 'density_trial':
                    from malody_studio.density_trials import run
                elif task_type == 'music_analysis':
                    from malody_studio.music_workflow import run_analysis as run
                elif task_type == 'separation':
                    from malody_studio.separation_tasks import run
                elif task_type == 'separation_trial':
                    from malody_studio.separation_trials import run
                elif advanced.get('input_sources') or advanced['settings'].get('source_mode')=='vocals_accompaniment':
                    from malody_studio.stem_generation import run
                else:
                    from malody_studio.advanced_generation import run
            else:
                from malody_studio.pipeline import run
        started=time.monotonic()
        with stage('queue_worker.execute_task', task_type=task_type, source=request['source']):
            result=run(Path(request['source']),directory,request['options'],progress)
        if request['options'].get('_advanced') and task_type == 'generation':
            from malody_studio.quality_workflow import finalize_generated
            with stage('quality.finalize_generated', result_keys=list(result)):
                result=finalize_generated(Path(request['source']),directory,request['options'],result)
        result['elapsed_seconds']=round(time.monotonic()-started,3)
        if request['options'].get('_advanced') and task_type=='generation':
            from malody_studio.advanced_execution import execution_summary
            with stage('advanced.execution_summary'):
                result['execution']=execution_summary(directory,result)
        with stage('queue_worker.write_result', result_keys=list(result)):
            write_result(result_path,result)
except Exception as error:
    result={'error':str(error)}
    quality_path=directory/'quality-failure.json'
    if quality_path.is_file():
        try:result['quality_failure']=json.loads(quality_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):pass
    failure_path=directory/'worker-failure.json'
    write_result(failure_path,result)
    raise
