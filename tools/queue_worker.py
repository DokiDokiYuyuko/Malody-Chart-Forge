import json
import os
import sys
import time
import uuid
from pathlib import Path

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
    if request['options'].get('_advanced'):
        if request['options']['_advanced'].get('task_type') == 'separation':
            from malody_studio.separation_tasks import run
        elif request['options']['_advanced'].get('task_type') == 'separation_trial':
            from malody_studio.separation_trials import run
        elif request['options']['_advanced'].get('input_sources') or request['options']['_advanced']['settings'].get('source_mode')=='vocals_accompaniment':
            from malody_studio.stem_generation import run
        else:
            from malody_studio.advanced_generation import run
        started=time.monotonic()
        result=run(Path(request['source']),directory,request['options'],progress)
        result['elapsed_seconds']=round(time.monotonic()-started,3)
        if request['options']['_advanced'].get('task_type','generation')=='generation':
            from malody_studio.advanced_execution import execution_summary
            result['execution']=execution_summary(directory,result)
    else:
        from malody_studio.pipeline import run
        report,_=run(Path(request['source']),directory,request['options'],progress)
        result={'report':report}
except Exception as error:
    result={'error':str(error)}
    quality_path=directory/'quality-failure.json'
    if quality_path.is_file():
        try:result['quality_failure']=json.loads(quality_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):pass
    failure_path=directory/'worker-failure.json'
    write_result(failure_path,result)
    raise
write_result(result_path,result)
