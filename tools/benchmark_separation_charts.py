"""Fixed V32 sampling on stem-owned evaluation projects via the deployed queue.

Each project treats one full stem as its immutable source so the role-derived
seed policy does not confound this experiment. Original BPM references are copied,
not re-estimated from the isolated stem. Existing user charts are untouched.
"""
import argparse
import copy
import json
from pathlib import Path
import sys
import time

import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from malody_studio.advanced import ProjectStore, atomic, read, variants
from malody_studio.advanced_generation import stable_seed
from evaluate_separation import selected_manifests

CASES=[('55f4cb5164af47a5bb42bd3b567a6577',14,18,'balanced'),
       ('038bf82ac8a3486fa532fa7a89d5a1d9',28,44,'speed')]


def api(base,path,body=None):
    response=requests.get(base+path,timeout=90) if body is None else requests.post(base+path,json=body,timeout=90)
    response.raise_for_status();return response.json()


def run(base,output):
    store=ProjectStore();report=read(output) if output.is_file() else {'seed':20261003,'difficulty':'hard','engine':'v32',
       'dynamic_enabled':False,'strategy':'independent','protocol':'Same actual seed, difficulty, pattern and original BPM reference; dedicated full-stem source projects; no density layering', 'runs':[]}
    for pid,start,end,pattern in CASES:
        parent=store.load(pid);manifests=selected_manifests(parent)
        for model in ('Original','Demucs-standard','Kim-cover-4'):
            manifest=manifests[model] if model!='Original' else {'id':'original','stems':[
                {'role':'original','path':str(store.directory(pid)/'source.wav'),'pcm_sha':parent['source_pcm_sha256']}]}
            for role in (('original',) if model=='Original' else ('vocals','accompaniment')):
                case_id=pid+':'+model+':'+role
                existing=next((r for r in report['runs'] if r['case_id']==case_id),None)
                if existing and existing.get('status')=='completed':continue
                queue=api(base,'/api/queue')
                if queue['running'] or queue['waiting']:raise RuntimeError('队列繁忙，停止基准提交')
                stem=next(row for row in manifest['stems'] if row['role']==role)
                if existing:p=store.load(existing['evaluation_project_id'])
                else:
                    p=store.create(stem['path'], '分离制谱对照 · '+model+' · '+role+' · '+('DNA' if start==14 else 'MIKU'))
                    settings={**p['settings'],'fixed_seed':True,'seed':report['seed'],'dynamic_enabled':False,'engine':'v32','strategy':'independent'}
                    p=store.update(p['id'],{'settings':settings,'patterns':[pattern],'difficulties':['hard'],'tempo':copy.deepcopy(parent['tempo'])})
                    # Importing a reference must not turn its uncertain anchors
                    # into user-confirmed evidence in an evaluation project.
                    p['tempo']=copy.deepcopy(parent['tempo'])
                    p['evaluation']={'parent_project_id':pid,'parent_source_pcm_sha256':parent['source_pcm_sha256'],'stem_set_id':manifest['id'],'role':role,'purpose':'fixed seed separation comparison'}
                    store.save(p)
                    p=store.add_segment(p['id'],{'start_sample':round(start*44100),'end_sample':round(end*44100),'name':f'{start}–{end}s'})
                if p['source_pcm_sha256']!=stem['pcm_sha']:raise RuntimeError('评估项目没有保留同一声部 PCM')
                row=existing or {'case_id':case_id,'parent_project_id':pid,'model':model,'role':role,'evaluation_project_id':p['id'],
                   'range':[start,end],'source_pcm_sha256':stem['pcm_sha'],'stem_set_id':manifest['id'],'actual_model_seed':stable_seed(report['seed'],pattern,'hard')}
                if not existing:report['runs'].append(row)
                atomic(output,report)
                seg=p['segments'][0];began=time.monotonic()
                result=api(base,'/api/advanced/projects/'+p['id']+'/segments/'+seg['id']+'/generate',
                     {'source_id':'original','variants':[pattern+'--hard'],'expected_revision':p['revision']})
                row['job_id']=result['id'];atomic(output,report);print('Queued '+case_id+' job='+result['id'],flush=True)
                last=None
                while True:
                    job=api(base,'/api/jobs/'+result['id']);message=(job['status'],job.get('message'),job.get('progress'))
                    if last!=message:print(str(message),flush=True);last=message
                    if job['status'] in ('completed','failed','interrupted','cancelled'):break
                    time.sleep(3)
                row.update(status=job['status'],wall_seconds=round(time.monotonic()-began,3),error=job.get('error'),revisions=job.get('advanced_revisions',[]))
                if row['status']=='completed':
                    revision=store.revision(p['id'],row['revisions'][0]);row['events']=revision['events'];row['provenance']=revision['provenance']
                    row['heads_per_second']=[sum(start+i<=e['start_ms']/1000<start+i+1 for e in row['events']) for i in range(end-start)]
                atomic(output,report)
                if row['status']!='completed':print('Failure recorded; proceeding to the next stem',flush=True)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--service',default='http://127.0.0.1:8766');parser.add_argument('--output',type=Path,default=ROOT/'outputs'/'separation-evaluation'/'20261004'/'charts.json')
    args=parser.parse_args();run(args.service,args.output)
