"""Normalize existing deliverables without importing the server or running models."""
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from malody_studio.library import publish, _write, is_deleted


def organize(root=ROOT):
    rows=[]
    sources=[]
    for directory in (root/'outputs').iterdir():
        state=directory/'job.json'
        if not state.is_file():continue
        try:
            job=json.loads(state.read_text(encoding='utf-8'))
            if job.get('status')!='completed' or job.get('options',{}).get('_advanced'):continue
            sources.append((job['id'],directory,job.get('created'),{'type':'song','job_id':job['id']}))
        except (OSError,ValueError,KeyError) as e:rows.append({'source':str(state),'status':'failed','error':str(e)})
    for project in (root/'outputs'/'advanced').glob('*/project.json'):
        try:
            p=json.loads(project.read_text(encoding='utf-8'))
            for a in p['assemblies']:sources.append(('advanced-'+a['id'],project.parent/'assemblies'/a['id'],a.get('created'),{'type':'advanced','project_id':p['id'],'assembly_id':a['id']}))
        except (OSError,ValueError,KeyError) as e:rows.append({'source':str(project),'status':'failed','error':str(e)})
    for record,directory,created,source in sources:
        if is_deleted(root,record):
            rows.append({'record_id':record,'status':'deleted'});continue
        try:
            report=json.loads((directory/'report.json').read_text(encoding='utf-8'))
            folder,manifest,target=publish(root,record,directory/'malody-4k.mcz',report,created,source)
            rows.append({'record_id':record,'status':'ready','directory':str(folder),'archive':str(target)})
        except (OSError,ValueError,KeyError,IndexError) as e:rows.append({'record_id':record,'status':'failed','error':str(e)})
    result={'schema':1,'ready':sum(r['status']=='ready' for r in rows),'failed':sum(r['status']=='failed' for r in rows),'records':rows}
    _write(root/'work'/'library-organization-report.json',result)
    return result


if __name__=='__main__':
    result=organize();print(json.dumps({k:result[k] for k in ('ready','failed')},ensure_ascii=False))
